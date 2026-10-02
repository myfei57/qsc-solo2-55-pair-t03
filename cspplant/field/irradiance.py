"""多路辐照采集、投票与落盘。

现场至少接入三路辐照计。每轮先做单表量程检查，再在有效读数里寻找互相一致的
多数：没有多数就不能形成可信总辐照；全部一致时按固定规则取均值；只要有表被
排除，即使多数仍可信，也属于部分失败，总辐照固定取可信路的最小值。可信度不足
的结果只保留在内存状态和审计里，绝不覆盖盘上快照。

追日用的是**已落盘**的投票快照：现场重启后控制链只能从盘上恢复，如果追日读的
是内存里那份还没落盘的值，重启后就会按旧快照对焦，吸热器局部超温。快照带
SHA-256 摘要，读回时重新计算，用来核对多路读数、投票结论、总辐照和审计口径。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from itertools import combinations

from ..config import IrradianceLimits
from .persist import FieldPersistence, digest_snapshot

HEALTHY = "healthy"
DEGRADED = "degraded"
UNAVAILABLE = "unavailable"
MEAN = "mean"
MINIMUM = "minimum"
NONE = "none"


@dataclass(frozen=True)
class MeterReading:
    """一台辐照计的一轮读数。"""

    meter_id: str
    dni_w_m2: float | None
    ts: float
    valid: bool
    reason: str = ""

    def as_dict(self) -> dict:
        value = None if self.dni_w_m2 is None else round(self.dni_w_m2, 3)
        return {
            "meter_id": self.meter_id,
            "dni_w_m2": value,
            "ts": round(self.ts, 3),
            "valid": self.valid,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class IrradianceVerdict:
    """一轮多路辐照投票结论。"""

    accepted: bool
    dni_w_m2: float
    ts: float
    confidence: float
    confidence_level: str
    mode: str
    aggregation: str
    trusted_meters: tuple[str, ...]
    rejected_meters: tuple[str, ...]
    readings: tuple[MeterReading, ...]
    reason: str
    events: tuple[dict, ...] = field(default_factory=tuple)
    digest: str = ""

    def as_dict(self) -> dict:
        return {
            "accepted": self.accepted,
            "dni_w_m2": round(self.dni_w_m2, 3),
            "ts": round(self.ts, 3),
            "confidence": round(self.confidence, 4),
            "confidence_level": self.confidence_level,
            "mode": self.mode,
            "aggregation": self.aggregation,
            "trusted_meters": list(self.trusted_meters),
            "rejected_meters": list(self.rejected_meters),
            "readings": [item.as_dict() for item in self.readings],
            "reason": self.reason,
            "events": list(self.events),
            "digest": self.digest,
        }


class IrradianceMeter:
    """多路辐照计、投票结论与它们的落盘副本。"""

    def __init__(
        self,
        limits: IrradianceLimits,
        persistence: FieldPersistence,
        meter_ids: tuple[str, ...] | list[str] | None = None,
    ) -> None:
        self._limits = limits
        self._persistence = persistence
        if meter_ids is None:
            meter_ids = tuple(f"irradiance-{index:03d}" for index in range(1, limits.meter_count + 1))
        self._meter_ids = tuple(str(item) for item in meter_ids)
        if len(self._meter_ids) != limits.meter_count:
            raise ValueError("辐照计数量必须与配置一致")
        self._current = 0.0
        self._sampled_at = 0.0
        self._current_verdict: IrradianceVerdict | None = None
        self._latest_verdict: IrradianceVerdict | None = None
        self._health = {meter_id: "unknown" for meter_id in self._meter_ids}
        self._samples = 0
        self._rejected = 0
        self._persists = 0
        self._meter_rejections = {meter_id: 0 for meter_id in self._meter_ids}
        self._health_events: list[dict] = []
        self._restore()

    @property
    def limits(self) -> IrradianceLimits:
        return self._limits

    @property
    def persistence(self) -> FieldPersistence:
        return self._persistence

    @property
    def meter_ids(self) -> tuple[str, ...]:
        return self._meter_ids

    def _restore(self) -> None:
        """重启后从盘上恢复最近一次可信投票，避免内存与盘上状态错位。"""

        stored = self._persistence.read_irradiance()
        if stored is None:
            return
        aggregate = stored["aggregate"]
        self._current = float(aggregate["dni_w_m2"])
        self._sampled_at = float(aggregate["ts"])
        if stored["schema_version"] != 3:
            return
        verdict_data = stored["verdict"]
        readings = tuple(
            MeterReading(
                meter_id=str(item["meter_id"]),
                dni_w_m2=item.get("dni_w_m2"),
                ts=float(item.get("ts", aggregate["ts"])),
                valid=bool(item.get("valid", False)),
                reason=str(item.get("reason", "")),
            )
            for item in stored["readings"]
        )
        self._current_verdict = IrradianceVerdict(
            accepted=bool(verdict_data.get("accepted", False)),
            dni_w_m2=self._current,
            ts=self._sampled_at,
            confidence=float(verdict_data.get("confidence", 0.0)),
            confidence_level=str(verdict_data.get("confidence_level", "low")),
            mode=str(verdict_data.get("mode", UNAVAILABLE)),
            aggregation=str(verdict_data.get("aggregation", NONE)),
            trusted_meters=tuple(str(item) for item in verdict_data.get("trusted_meters", [])),
            rejected_meters=tuple(str(item) for item in verdict_data.get("rejected_meters", [])),
            readings=readings,
            reason=str(verdict_data.get("reason", "")),
            digest=str(stored.get("digest", "")),
        )
        self._latest_verdict = self._current_verdict
        for meter_id in self._current_verdict.trusted_meters:
            self._health[meter_id] = "healthy"
        for meter_id in self._current_verdict.rejected_meters:
            self._health[meter_id] = "faulty"

    def sample(self, dni_w_m2: float, ts: float) -> IrradianceVerdict:
        """兼容单表接口：把同一个读数复制到所有辐照计后再投票。"""

        return self.sample_many({meter_id: dni_w_m2 for meter_id in self._meter_ids}, ts)

    def sample_many(self, readings: dict[str, float | None], ts: float) -> IrradianceVerdict:
        """采集一轮多路 DNI，按多数互检结果形成固定口径的总辐照。"""

        moment = float(ts)
        unknown = sorted(set(readings) - set(self._meter_ids))
        if unknown:
            raise ValueError(f"未知辐照计: {', '.join(unknown)}")
        meter_readings = tuple(self._read_meter(meter_id, readings.get(meter_id), moment) for meter_id in self._meter_ids)
        valid = {item.meter_id: float(item.dni_w_m2) for item in meter_readings if item.valid}
        trusted, trust_reason = self._trusted_majority(valid)
        rejected = tuple(meter_id for meter_id in self._meter_ids if meter_id not in trusted)

        if trusted:
            values = [valid[meter_id] for meter_id in trusted]
            partial_failure = len(trusted) < len(self._meter_ids)
            mode = DEGRADED if partial_failure else HEALTHY
            aggregation = MINIMUM if partial_failure else MEAN
            aggregate = min(values) if partial_failure else sum(values) / len(values)
            spread = (max(values) - min(values)) / max(1.0, max(abs(item) for item in values))
            agreement = max(0.0, 1.0 - spread / self._limits.agreement_tolerance)
            confidence = len(trusted) / len(self._meter_ids) * (0.90 + 0.10 * agreement)
            accepted = confidence >= self._limits.min_focus_confidence
            reason = "partial_meter_failure" if partial_failure else "all_meters_agree"
            if not accepted:
                reason = "confidence_below_threshold"
        else:
            aggregate = self._current
            mode = UNAVAILABLE
            aggregation = NONE
            confidence = 0.0
            accepted = False
            reasons = [item.reason for item in meter_readings if not item.valid]
            if reasons and all(item == "out_of_range" for item in reasons):
                reason = "out_of_range"
            elif reasons and all(item == "missing" for item in reasons):
                reason = "missing_readings"
            else:
                reason = trust_reason

        level = self._confidence_level(confidence) if accepted else "low"
        events = self._transition_health(trusted, rejected, valid, moment)
        verdict = IrradianceVerdict(
            accepted=accepted,
            dni_w_m2=float(aggregate),
            ts=moment,
            confidence=float(confidence),
            confidence_level=level,
            mode=mode,
            aggregation=aggregation,
            trusted_meters=trusted,
            rejected_meters=rejected,
            readings=meter_readings,
            reason=reason,
            events=tuple(events),
        )
        verdict = self._with_digest(verdict)
        self._latest_verdict = verdict
        if accepted:
            self._current = verdict.dni_w_m2
            self._sampled_at = moment
            self._current_verdict = verdict
            self._samples += 1
        else:
            self._rejected += 1
        for meter_id in rejected:
            self._meter_rejections[meter_id] += 1
        return verdict

    def _read_meter(self, meter_id: str, raw: object, ts: float) -> MeterReading:
        if raw is None:
            return MeterReading(meter_id, None, ts, False, "missing")
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return MeterReading(meter_id, None, ts, False, "not_a_number")
        if not math.isfinite(value):
            return MeterReading(meter_id, None, ts, False, "not_finite")
        if not self._limits.in_range(value):
            return MeterReading(meter_id, value, ts, False, "out_of_range")
        return MeterReading(meter_id, value, ts, True)

    def _trusted_majority(self, valid: dict[str, float]) -> tuple[tuple[str, ...], str]:
        minimum_count = len(self._meter_ids) // 2 + 1
        if not valid:
            return tuple(), "no_valid_readings"
        if len(valid) < minimum_count:
            return tuple(), "no_trusted_majority"

        meter_ids = tuple(sorted(valid))
        best: tuple[str, ...] | None = None
        ambiguous = False
        for size in range(len(meter_ids), minimum_count - 1, -1):
            candidates = []
            for candidate in combinations(meter_ids, size):
                if all(
                    self._limits.readings_agree(valid[left], valid[right])
                    for left, right in combinations(candidate, 2)
                ):
                    candidates.append(candidate)
            if candidates:
                if len(candidates) == 1:
                    best = tuple(candidates[0])
                else:
                    ambiguous = True
                break
        if best is None:
            return tuple(), "ambiguous_majority" if ambiguous else "no_trusted_majority"
        return best, ""

    def _confidence_level(self, confidence: float) -> str:
        if confidence >= 0.85:
            return "high"
        if confidence >= self._limits.min_focus_confidence:
            return "medium"
        return "low"

    def _transition_health(
        self,
        trusted: tuple[str, ...],
        rejected: tuple[str, ...],
        valid: dict[str, float],
        ts: float,
    ) -> list[dict]:
        events: list[dict] = []
        for meter_id in rejected:
            previous = self._health.get(meter_id, "unknown")
            value = valid.get(meter_id)
            reason = "out_of_consensus" if value is not None else "invalid_reading"
            if trusted and previous == "healthy":
                events.append(
                    {
                        "meter_id": meter_id,
                        "event": "meter_rejected",
                        "dni_w_m2": None if value is None else round(value, 3),
                        "reason": reason,
                        "ts": round(ts, 3),
                    }
                )
            self._health[meter_id] = "faulty" if trusted else "suspect"
        for meter_id in trusted:
            previous = self._health.get(meter_id, "unknown")
            if previous in ("faulty", "suspect"):
                events.append(
                    {
                        "meter_id": meter_id,
                        "event": "meter_recovered",
                        "dni_w_m2": round(valid[meter_id], 3),
                        "reason": "back_in_consensus",
                        "ts": round(ts, 3),
                    }
                )
            self._health[meter_id] = "healthy"
        self._health_events.extend(events)
        return events

    def _snapshot(self, verdict: IrradianceVerdict) -> dict:
        return {
            "schema_version": 3,
            "aggregate": {
                "dni_w_m2": round(verdict.dni_w_m2, 3),
                "ts": round(verdict.ts, 3),
            },
            "readings": [item.as_dict() for item in sorted(verdict.readings, key=lambda item: item.meter_id)],
            "verdict": {
                "accepted": verdict.accepted,
                "confidence": round(verdict.confidence, 4),
                "confidence_level": verdict.confidence_level,
                "mode": verdict.mode,
                "aggregation": verdict.aggregation,
                "trusted_meters": list(verdict.trusted_meters),
                "rejected_meters": list(verdict.rejected_meters),
                "reason": verdict.reason,
                "meter_count": len(self._meter_ids),
            },
        }

    def _with_digest(self, verdict: IrradianceVerdict) -> IrradianceVerdict:
        snapshot = self._snapshot(verdict)
        digest = digest_snapshot(snapshot)
        object.__setattr__(verdict, "digest", digest)
        return verdict

    def current(self) -> float:
        """返回最近一次可信总 DNI；不可信轮次不覆盖它。"""

        return self._current

    def sampled_at(self) -> float:
        """返回最近一次可信总辐照的采集时刻。"""

        return self._sampled_at

    def latest(self) -> IrradianceVerdict | None:
        """返回最近一轮投票（包括未达可信度的部分失败）。"""

        return self._latest_verdict

    def current_verdict(self) -> IrradianceVerdict | None:
        """返回当前仍可作为控制基线的投票。"""

        return self._current_verdict

    def latest_accepted(self) -> bool:
        """最近一轮读数是否形成了可信总辐照。"""

        return self._latest_verdict is not None and self._latest_verdict.accepted

    def persist(self) -> float:
        """把当前可信投票落盘；可信度不足时不覆盖盘上值。"""

        verdict = self._current_verdict
        if verdict is None or not verdict.accepted:
            return self._current
        self._persistence.write_irradiance(self._snapshot(verdict))
        self._persists += 1
        return self._current

    def durable(self) -> float:
        """读回**已落盘**的总 DNI；盘上没有就返回 0。"""

        stored = self._persistence.read_irradiance()
        return 0.0 if stored is None else stored["aggregate"]["dni_w_m2"]

    def durable_at(self) -> float:
        """返回已落盘值的采集时刻（没有落盘时返回 0）。"""

        stored = self._persistence.read_irradiance()
        return 0.0 if stored is None else stored["aggregate"]["ts"]

    def durable_verdict(self) -> IrradianceVerdict | None:
        """读回已落盘投票；旧版单表文档不还原成投票对象。"""

        stored = self._persistence.read_irradiance()
        if stored is None or stored["schema_version"] != 3:
            return None
        data = stored["verdict"]
        readings = tuple(
            MeterReading(
                meter_id=str(item["meter_id"]),
                dni_w_m2=item.get("dni_w_m2"),
                ts=float(item.get("ts", stored["aggregate"]["ts"])),
                valid=bool(item.get("valid", False)),
                reason=str(item.get("reason", "")),
            )
            for item in stored["readings"]
        )
        return IrradianceVerdict(
            accepted=bool(data.get("accepted", False)),
            dni_w_m2=stored["aggregate"]["dni_w_m2"],
            ts=stored["aggregate"]["ts"],
            confidence=float(data.get("confidence", 0.0)),
            confidence_level=str(data.get("confidence_level", "low")),
            mode=str(data.get("mode", UNAVAILABLE)),
            aggregation=str(data.get("aggregation", NONE)),
            trusted_meters=tuple(str(item) for item in data.get("trusted_meters", [])),
            rejected_meters=tuple(str(item) for item in data.get("rejected_meters", [])),
            readings=readings,
            reason=str(data.get("reason", "")),
            digest=str(stored.get("digest", "")),
        )

    def durable_matches_current(self) -> bool:
        """盘上快照是否就是当前可信投票。"""

        stored = self._persistence.read_irradiance()
        if stored is None:
            return False
        verdict = self._current_verdict
        if verdict is None:
            return stored["schema_version"] == 1 and abs(
                stored["aggregate"]["dni_w_m2"] - self._current
            ) < 1e-9
        return stored["schema_version"] == 3 and stored.get("digest") == verdict.digest

    def stale(self, now: float) -> bool:
        """已落盘快照是否已经过期。"""

        stored = self._persistence.read_irradiance()
        if stored is None:
            return True
        return (float(now) - stored["aggregate"]["ts"]) > self._limits.stale_after_s

    def consistency(self, now: float | None = None) -> dict:
        """核对内存投票、落盘快照与摘要是否一致。"""

        stored = self._persistence.read_irradiance()
        verdict = self.current_verdict()
        durable = self.durable_verdict()
        latest = self._latest_verdict
        digest_valid = durable is not None and bool(durable.digest)
        matches = (
            stored is not None
            and stored["schema_version"] == 3
            and verdict is not None
            and durable is not None
            and verdict.digest == durable.digest
        )
        expected = set(self._meter_ids)
        actual = {item.meter_id for item in verdict.readings} if verdict is not None else set()
        checks = {
            "durable_exists": stored is not None,
            "digest_valid": digest_valid,
            "current_matches_durable": matches,
            "expected_meters_present": actual == expected,
            "not_stale": stored is not None and not self.stale(now if now is not None else self.durable_at()),
        }
        return {
            "ok": all(checks.values()),
            "checks": checks,
            "latest_digest": latest.digest if latest is not None else "",
            "durable_digest": durable.digest if durable is not None else "",
        }

    def health(self) -> dict:
        """返回每台辐照计当前健康状态与累计拒绝次数。"""

        return {
            meter_id: {
                "state": self._health[meter_id],
                "rejections": self._meter_rejections[meter_id],
            }
            for meter_id in self._meter_ids
        }

    def health_events(self) -> list[dict]:
        """返回辐照计被剔除和恢复事件。"""

        return list(self._health_events)

    def sample_count(self) -> int:
        """返回形成可信总辐照的采样轮数。"""

        return self._samples

    def rejected(self) -> int:
        """返回未形成可信总辐照的轮数。"""

        return self._rejected

    def persist_count(self) -> int:
        """返回落盘次数。"""

        return self._persists
