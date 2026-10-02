"""多路辐照计表决与总辐照度融合。

现场只有一台辐照计时，读数一漂镜场就跟着动，落盘那份也变成错的。这里把
N 台辐照计同时接入，每个采集周期做三层判断：

1. **逐路故障判定**——死值（无读数／越界）、相对同伴的一致性离群（漂移／
   偏置）、相对同伴的变化率尖峰、长时间卡死（读数不动而同伴在变）。硬故障
   立即判可疑，软故障连续若干周期才升级为故障，恢复需连续若干周期干净读数，
   避免云团遮挡这类真实瞬态被误判。
2. **可信度评分**——只取状态为 GOOD 的路作为可信集合，按可信票数、读数
   一致性、近期稳定性打分，映射成 HIGH / MEDIUM / LOW 三档。
3. **融合与门控**——按**工况**从密封策略里取融合规则（可信均值或最保守
   值）。可信度不足（LOW、可信路不足半数、工况未知）一律不落写死值，
   返回 ``committed=False``，镜场保持保守姿态（待机／收起）。

融合规则按工况"定死"在代码里（:data:`SEALED_POLICY`），运行期不可改；
策略带版本号与 SHA256 指纹，每条融合结果都记录指纹，巡检时可核对现场
用的是不是同一份规则。
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field

# ---- 工况与融合规则（定死，不允许运行期切换） -------------------------

MODE_STARTUP = "startup"  # 启动／预热序列：低估辐照最安全
MODE_NORMAL = "normal_sun"  # 正常追日：按可信均值
MODE_CLOUD = "cloud_transient"  # 云团过境：按可信均值（云是真实变化，不保护）
MODE_SHUTDOWN = "shutdown"  # 停机／夜间：高估辐照最安全，防止提前对焦

KNOWN_MODES: tuple[str, ...] = (MODE_STARTUP, MODE_NORMAL, MODE_CLOUD, MODE_SHUTDOWN)

FUSION_TRUSTED_MEAN = "trusted_mean"  # 取可信几路的均值
FUSION_CONSERVATIVE_MIN = "conservative_min"  # 取可信几路的最小值（低估）
FUSION_CONSERVATIVE_MAX = "conservative_max"  # 取可信几路的最大值（高估）

# 每个工况用哪种融合规则，是现场安全决定，写死后只能随版本发布修改：
# - 启动／预热序列低估辐照，镜场不会提前把光打到尚未建立循环的吸热器上；
# - 正常追日按可信均值；
# - 云团过境按可信均值（云团是真实辐照变化，不能用低值把镜场收起）；
# - 停机／夜间高估辐照，镜场不会因为低读数提前进入对焦。
_MODE_FUSION: dict[str, str] = {
    MODE_STARTUP: FUSION_CONSERVATIVE_MIN,
    MODE_NORMAL: FUSION_TRUSTED_MEAN,
    MODE_CLOUD: FUSION_TRUSTED_MEAN,
    MODE_SHUTDOWN: FUSION_CONSERVATIVE_MAX,
}

POLICY_VERSION = "v1.0.0"


@dataclass(frozen=True)
class FusionPolicy:
    """一份密封的融合策略：工况 → 融合规则，外加判定门限。"""

    version: str
    mode_fusion: dict[str, str]
    physical_min_w_m2: float
    physical_max_w_m2: float
    drift_relative: float  # 相对同伴中位数偏离比例即判一致性离群
    drift_streak_fault: int  # 离群连续多少周期升级为故障
    rate_spike_ratio: float  # 变化率是同伴中位数的多少倍即判尖峰
    rate_spike_min_w_m2_s: float  # 尖峰的最小绝对变化幅度，避免晴天噪声误判
    stuck_window: int  # 卡死判定窗口（周期数）
    stuck_self_tol_w_m2: float  # 窗口内自身读数展宽小于该值视为不动
    stuck_peer_move_relative: float  # 同期同伴中位数移动比例超过该值才判卡死
    recover_clean_cycles: int  # 故障后连续多少周期干净才恢复
    weight_vote: float
    weight_agreement: float
    weight_stability: float
    high_score: float
    medium_score: float
    agreement_spread_scale: float  # 一致性分 = exp(-展宽/该尺度)，展宽为相对极差

    def fusion_for(self, mode: str) -> str | None:
        """返回某工况的融合规则；工况未知返回 ``None``（调用方必须挂起）。"""

        return self.mode_fusion.get(mode)

    def canonical(self) -> dict:
        """策略的规范形式（排序键、固定顺序），用于计算指纹与展示。"""

        return {
            "version": self.version,
            "mode_fusion": {key: self.mode_fusion[key] for key in sorted(self.mode_fusion)},
            "physical_min_w_m2": round(self.physical_min_w_m2, 3),
            "physical_max_w_m2": round(self.physical_max_w_m2, 3),
            "drift_relative": round(self.drift_relative, 4),
            "drift_streak_fault": self.drift_streak_fault,
            "rate_spike_ratio": round(self.rate_spike_ratio, 3),
            "rate_spike_min_w_m2_s": round(self.rate_spike_min_w_m2_s, 3),
            "stuck_window": self.stuck_window,
            "stuck_self_tol_w_m2": round(self.stuck_self_tol_w_m2, 3),
            "stuck_peer_move_relative": round(self.stuck_peer_move_relative, 4),
            "recover_clean_cycles": self.recover_clean_cycles,
            "weight_vote": round(self.weight_vote, 3),
            "weight_agreement": round(self.weight_agreement, 3),
            "weight_stability": round(self.weight_stability, 3),
            "high_score": round(self.high_score, 3),
            "medium_score": round(self.medium_score, 3),
            "agreement_spread_scale": round(self.agreement_spread_scale, 4),
        }

    def fingerprint(self) -> str:
        """策略指纹：规范 JSON 的 SHA256，前 16 位。"""

        body = json.dumps(self.canonical(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]

    def describe(self) -> dict:
        """供控制台／审计展示的策略说明。"""

        return {"version": self.version, "fingerprint": self.fingerprint(), **self.canonical()}


SEALED_POLICY = FusionPolicy(
    version=POLICY_VERSION,
    mode_fusion=dict(_MODE_FUSION),
    physical_min_w_m2=0.0,
    physical_max_w_m2=1500.0,
    drift_relative=0.05,
    drift_streak_fault=3,
    rate_spike_ratio=8.0,
    rate_spike_min_w_m2_s=40.0,
    stuck_window=8,
    stuck_self_tol_w_m2=1.0,
    stuck_peer_move_relative=0.015,
    recover_clean_cycles=3,
    weight_vote=0.45,
    weight_agreement=0.40,
    weight_stability=0.15,
    high_score=0.72,
    medium_score=0.45,
    agreement_spread_scale=0.10,
)

# ---- 通道状态与故障类型 -----------------------------------------------

GOOD = "good"  # 可信，参与融合
SUSPECT = "suspect"  # 可疑（部分失败），不参与融合，尚未升级
FAULT = "fault"  # 故障（部分失败），不参与融合，等待连续干净读数恢复

HARD_DEAD = "dead"  # 无读数／非数值
HARD_RANGE = "out_of_range"  # 越界
SOFT_DRIFT = "peer_drift"  # 相对同伴离群（漂移／偏置）
SOFT_SPIKE = "rate_spike"  # 变化率尖峰
SOFT_STUCK = "stuck"  # 卡死
SOFT_OK = "ok"  # 本周期干净

LEVEL_HIGH = "HIGH"
LEVEL_MEDIUM = "MEDIUM"
LEVEL_LOW = "LOW"


@dataclass
class _ChannelHealth:
    """单台辐照计的滚动健康状态（可变，由表决器每个周期推进）。"""

    name: str
    state: str = GOOD
    offense: str = ""  # 最近一次故障类型
    offense_streak: int = 0  # 软故障连续周期数
    clean_streak: int = 0  # 故障后连续干净周期数
    history: list[tuple[float, float]] = field(default_factory=list)  # (ts, 值)
    last_value: float | None = None
    last_ts: float | None = None

    def push_history(self, ts: float, value: float, window: int) -> None:
        self.history.append((float(ts), float(value)))
        if len(self.history) > window:
            del self.history[: len(self.history) - window]


@dataclass(frozen=True)
class ChannelVerdict:
    """一台辐照计在某个周期的判定结果。"""

    name: str
    raw: float | None
    in_range: bool
    mark: str  # 本周期的故障标记：SOFT_OK / HARD_* / SOFT_*
    state: str  # 更新后的通道状态：GOOD / SUSPECT / FAULT
    offense: str  # 最近一次故障类型（GOOD 时为空串）

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "raw": None if self.raw is None else round(self.raw, 3),
            "in_range": self.in_range,
            "mark": self.mark,
            "state": self.state,
            "offense": self.offense,
        }


@dataclass(frozen=True)
class MeterIncident:
    """一次通道故障升级或恢复事件（部分失败必须能指到具体哪一台）。"""

    name: str
    kind: str  # fault / recovered
    offense: str
    state: str
    cycle: int
    ts: float
    raw: float | None

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "kind": self.kind,
            "offense": self.offense,
            "state": self.state,
            "cycle": self.cycle,
            "ts": round(self.ts, 3),
            "raw": None if self.raw is None else round(self.raw, 3),
        }


@dataclass(frozen=True)
class Confidence:
    """总辐照度的可信度评分与分档。"""

    score: float
    level: str
    trusted_count: int
    total_count: int
    vote_fraction: float
    spread_relative: float
    stable_fraction: float
    factors: dict

    def as_dict(self) -> dict:
        return {
            "score": round(self.score, 4),
            "level": self.level,
            "trusted_count": self.trusted_count,
            "total_count": self.total_count,
            "vote_fraction": round(self.vote_fraction, 4),
            "spread_relative": round(self.spread_relative, 4),
            "stable_fraction": round(self.stable_fraction, 4),
            "factors": dict(self.factors),
        }


@dataclass(frozen=True)
class FusedIrradiance:
    """一个采集周期的完整融合结论。

    ``committed`` 为 ``False`` 时 :attr:`value` 为 ``None``：可信度不够或工况
    未知，调用方**不得**往盘上写死值，镜场必须保持保守姿态。
    """

    cycle: int
    ts: float
    mode: str
    fusion_rule: str  # 实际使用的融合规则；未知工况时为 "hold"
    value: float | None
    committed: bool
    reason: str
    confidence: Confidence
    channels: tuple[ChannelVerdict, ...]
    incidents: tuple[MeterIncident, ...]
    policy_version: str
    policy_fingerprint: str

    def trusted(self) -> list[str]:
        """返回本周期参与融合的可信通道名。"""

        return [item.name for item in self.channels if item.state == GOOD and item.in_range]

    def as_dict(self) -> dict:
        return {
            "cycle": self.cycle,
            "ts": round(self.ts, 3),
            "mode": self.mode,
            "fusion_rule": self.fusion_rule,
            "dni_w_m2": None if self.value is None else round(self.value, 3),
            "committed": self.committed,
            "reason": self.reason,
            "confidence": self.confidence.as_dict(),
            "channels": [item.as_dict() for item in self.channels],
            "incidents": [item.as_dict() for item in self.incidents],
            "policy_version": self.policy_version,
            "policy_fingerprint": self.policy_fingerprint,
        }


# ---- 统计辅助 ---------------------------------------------------------


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return 0.5 * (ordered[mid - 1] + ordered[mid])


# ---- 表决器 -----------------------------------------------------------


class IrradianceVoter:
    """多路辐照计的逐周期表决、融合与可信度门控。"""

    def __init__(self, channel_names: list[str], policy: FusionPolicy = SEALED_POLICY) -> None:
        names = [str(name) for name in channel_names]
        if len(names) < 1 or len(set(names)) != len(names):
            raise ValueError("辐照计通道必须非空且名字唯一")
        self._names = names
        self._policy = policy
        self._health = {name: _ChannelHealth(name) for name in names}
        self._cycle = 0
        self._incidents: list[MeterIncident] = []

    @property
    def names(self) -> list[str]:
        return list(self._names)

    @property
    def policy(self) -> FusionPolicy:
        return self._policy

    def cycle_count(self) -> int:
        return self._cycle

    def incidents(self) -> list[MeterIncident]:
        """返回累计的故障升级／恢复事件（按时间升序）。"""

        return list(self._incidents)

    def state_of(self, name: str) -> str:
        return self._health[name].state

    def states(self) -> dict[str, str]:
        """返回每台辐照计的当前状态。"""

        return {name: self._health[name].state for name in self._names}

    def faulty(self) -> list[str]:
        """返回当前处于 FAULT 的通道（恢复后据此指出是哪台出过问题）。"""

        return [name for name in self._names if self._health[name].state == FAULT]

    def cycle(self, readings: dict[str, float | None], ts: float, mode: str) -> FusedIrradiance:
        """推进一个采集周期。

        ``readings`` 为通道名到读数的映射；缺通道或给 ``None`` 视为死值。
        """

        self._cycle += 1
        current = self._cycle
        previous_good = {name for name in self._names if self._health[name].state == GOOD}
        verdicts: list[ChannelVerdict] = []
        incidents: list[MeterIncident] = []

        # 只让在物理量程内的读数参与同伴统计。
        in_range_values = [
            float(value)
            for name in self._names
            if (value := readings.get(name)) is not None
            and isinstance(value, (int, float))
            and not isinstance(value, bool)
            and self._policy.physical_min_w_m2 <= float(value) <= self._policy.physical_max_w_m2
        ]
        peer_median = _median(in_range_values) if in_range_values else None

        for name in self._names:
            verdict = self._evaluate_channel(
                name,
                readings.get(name),
                float(ts),
                peer_median,
                in_range_values,
                readings,
                current,
                incidents,
            )
            verdicts.append(verdict)
        self._incidents.extend(incidents)

        trusted_values = [
            float(readings[name])
            for name in self._names
            if self._health[name].state == GOOD
            and (raw := readings.get(name)) is not None
            and isinstance(raw, (int, float))
            and not isinstance(raw, bool)
            and self._policy.physical_min_w_m2 <= float(raw) <= self._policy.physical_max_w_m2
        ]
        confidence = self._score(trusted_values, previous_good)
        value, rule, committed, reason = self._fuse(trusted_values, mode, confidence)
        return FusedIrradiance(
            cycle=current,
            ts=float(ts),
            mode=str(mode),
            fusion_rule=rule,
            value=value,
            committed=committed,
            reason=reason,
            confidence=confidence,
            channels=tuple(verdicts),
            incidents=tuple(incidents),
            policy_version=self._policy.version,
            policy_fingerprint=self._policy.fingerprint(),
        )

    # ---- 单通道判定 ---------------------------------------------------

    def _evaluate_channel(
        self,
        name: str,
        raw: object,
        ts: float,
        peer_median: float | None,
        in_range_values: list[float],
        readings: dict[str, float | None],
        current: int,
        incidents: list[MeterIncident],
    ) -> ChannelVerdict:
        health = self._health[name]
        value = raw if isinstance(raw, (int, float)) and not isinstance(raw, bool) else None

        # 硬故障：死值或越界，立即判可疑并升级（连续缺读数同样视为故障）。
        if value is None:
            return self._hard_fault(name, health, None, ts, HARD_DEAD, current, incidents)
        numeric = float(value)
        if not (self._policy.physical_min_w_m2 <= numeric <= self._policy.physical_max_w_m2):
            return self._hard_fault(name, health, numeric, ts, HARD_RANGE, current, incidents)

        # 软故障检测（需要至少两台在量程内，单台不做同伴比较）。尖峰先判：
        # 单周期突变若先按离群处理，故障类型会被记成漂移，恢复后指不到真实原因。
        mark = SOFT_OK
        if len(in_range_values) >= 2:
            if self._is_spike(name, numeric, ts, readings):
                mark = SOFT_SPIKE
            elif self._is_drift(numeric, peer_median):
                mark = SOFT_DRIFT
            elif self._is_stuck(health, numeric, ts):
                mark = SOFT_STUCK

        previous = health.state
        if mark != SOFT_OK:
            health.offense = mark
            health.offense_streak += 1
            health.clean_streak = 0
            if previous == FAULT:
                health.state = FAULT  # 恢复尝试中再犯，继续按故障处理
            elif health.offense_streak >= self._policy.drift_streak_fault:
                health.state = FAULT
            else:
                health.state = SUSPECT
        else:
            health.offense_streak = 0
            health.clean_streak += 1
            recovered_offense = None
            if previous == SUSPECT:
                health.state = GOOD
                health.offense = ""
            elif previous == FAULT and health.clean_streak >= self._policy.recover_clean_cycles:
                recovered_offense = health.offense
                health.state = GOOD
                health.offense = ""
            if previous == FAULT and health.state == GOOD:
                incidents.append(
                    MeterIncident(
                        name,
                        "recovered",
                        recovered_offense,
                        GOOD,
                        current,
                        ts,
                        numeric,
                    )
                )

        if health.state == FAULT and previous != FAULT:
            incidents.append(
                MeterIncident(name, "fault", health.offense, FAULT, current, ts, numeric)
            )
        health.push_history(ts, numeric, self._policy.stuck_window)
        health.last_value = numeric
        health.last_ts = ts
        return ChannelVerdict(name, numeric, True, mark, health.state, health.offense if health.state != GOOD else "")

    def _hard_fault(
        self,
        name: str,
        health: _ChannelHealth,
        numeric: float | None,
        ts: float,
        kind: str,
        current: int,
        incidents: list[MeterIncident],
    ) -> ChannelVerdict:
        previous = health.state
        health.offense = kind
        health.offense_streak += 1
        health.clean_streak = 0
        health.state = FAULT
        # 坏值不能进入滚动窗口，也不能作为下一周期变化率的基线。
        health.history.clear()
        health.last_value = None
        health.last_ts = None
        if previous != FAULT:
            incidents.append(MeterIncident(name, "fault", kind, FAULT, current, ts, numeric))
        return ChannelVerdict(name, numeric, False, kind, FAULT, kind)

    def _is_drift(self, value: float, peer_median: float | None) -> bool:
        if peer_median is None or peer_median <= 0.0:
            return False
        return abs(value - peer_median) / peer_median > self._policy.drift_relative

    def _is_spike(self, name: str, value: float, ts: float, readings: dict) -> bool:
        """本路变化率显著超过同周期同伴变化率时判尖峰。"""

        health = self._health[name]
        if health.last_value is None or health.last_ts is None:
            return False
        dt = ts - health.last_ts
        if dt <= 0.0:
            return False
        rate = abs(value - health.last_value) / dt
        if rate < self._policy.rate_spike_min_w_m2_s:
            return False
        peer_rates: list[float] = []
        for other_name in self._names:
            if other_name == name:
                continue
            other = self._health[other_name]
            other_raw = readings.get(other_name)
            if other.last_value is None or other.last_ts is None:
                continue
            odt = ts - other.last_ts
            if odt <= 0.0 or not isinstance(other_raw, (int, float)) or isinstance(other_raw, bool):
                continue
            if not (
                self._policy.physical_min_w_m2 <= float(other_raw) <= self._policy.physical_max_w_m2
            ):
                continue
            peer_rates.append(abs(float(other_raw) - other.last_value) / odt)
        if not peer_rates:
            return False
        return rate > self._policy.rate_spike_ratio * _median(peer_rates)

    def _is_stuck(self, health: _ChannelHealth, value: float, ts: float) -> bool:
        """自身读数在窗口内几乎不动，而同伴中位数明显移动时判卡死。"""

        window = self._policy.stuck_window
        if len(health.history) + 1 < window:
            return False
        recent = health.history[-(window - 1) :] + [(ts, value)]
        span = max(v for _, v in recent) - min(v for _, v in recent)
        if span > self._policy.stuck_self_tol_w_m2:
            return False
        # 比较同伴中位数在窗口两端的移动：同伴整体在动而自己不动才叫卡死。
        old_peer: list[float] = []
        new_peer: list[float] = []
        for other_name in self._names:
            if other_name == health.name:
                continue
            other = self._health[other_name]
            if len(other.history) < window:
                return False
            segment = other.history[-(window - 1) :]
            old_peer.append(segment[0][1])
            new_peer.append(segment[-1][1])
        base = _median(old_peer)
        if base <= 0.0:
            return False
        return abs(_median(new_peer) - base) / base > self._policy.stuck_peer_move_relative

    # ---- 可信度评分 ---------------------------------------------------

    def _score(self, trusted_values: list[float], previously_good: set[str]) -> Confidence:
        total = len(self._names)
        trusted = len(trusted_values)
        vote_fraction = trusted / total

        if trusted >= 2:
            spread = (max(trusted_values) - min(trusted_values)) / max(_median(trusted_values), 1e-9)
            agreement = math.exp(-spread / self._policy.agreement_spread_scale)
        elif trusted == 1:
            spread = 0.0
            agreement = 1.0
        else:
            spread = 0.0
            agreement = 0.0

        # 稳定性：本周期可信通道里上一周期也可信的比例；首周期无历史按满分。
        trusted_names = {name for name in self._names if self._health[name].state == GOOD}
        if trusted == 0:
            stable = 0.0
        elif self._cycle <= 1:
            stable = 1.0
        else:
            stable = len(previously_good & trusted_names) / trusted

        score = (
            self._policy.weight_vote * vote_fraction
            + self._policy.weight_agreement * agreement
            + self._policy.weight_stability * stable
        )
        quorum = total // 2 + 1
        if trusted >= total and score >= self._policy.high_score:
            level = LEVEL_HIGH
        elif trusted >= quorum and score >= self._policy.medium_score:
            level = LEVEL_MEDIUM
        else:
            level = LEVEL_LOW
        return Confidence(
            score=round(score, 6),
            level=level,
            trusted_count=trusted,
            total_count=total,
            vote_fraction=vote_fraction,
            spread_relative=spread,
            stable_fraction=stable,
            factors={
                "agreement": round(agreement, 6),
                "quorum": quorum,
                "unanimous": trusted == total,
            },
        )

    # ---- 融合与门控 ---------------------------------------------------

    def _fuse(
        self, trusted_values: list[float], mode: str, confidence: Confidence
    ) -> tuple[float | None, str, bool, str]:
        rule = self._policy.fusion_for(str(mode))
        if rule is None:
            return None, "hold", False, "unknown_mode_hold"
        if confidence.level == LEVEL_LOW:
            if confidence.trusted_count == 0:
                return None, "hold", False, "no_trusted_channel"
            quorum = len(self._names) // 2 + 1
            if confidence.trusted_count < quorum:
                return None, "hold", False, "trusted_below_quorum"
            return None, "hold", False, "confidence_low"
        if rule == FUSION_TRUSTED_MEAN:
            return round(sum(trusted_values) / len(trusted_values), 3), rule, True, "committed"
        if rule == FUSION_CONSERVATIVE_MIN:
            return round(min(trusted_values), 3), rule, True, "committed_conservative"
        if rule == FUSION_CONSERVATIVE_MAX:
            return round(max(trusted_values), 3), rule, True, "committed_conservative"
        return None, "hold", False, "unsupported_fusion_rule"
