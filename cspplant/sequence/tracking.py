"""追日编排：多路采集 → 落盘 → 判定 → 下发。

落盘不是可选项：重启后控制链只能从盘上恢复辐照，如果追日在落盘之前就用了
内存里的当前值，重启后镜场会按旧快照对焦。这里把顺序写死，落盘失败、摘要不一致
或最近一轮投票可信度不足都不允许进入聚焦；即使拒绝写入，也仍会执行策略求值，
让镜场按待机/收起的保守姿态停留。
"""

from __future__ import annotations

from typing import Mapping

from ..field.field import FieldController
from ..heliostat.sun import SunPosition


def acquire_and_track(
    field: FieldController,
    dni_w_m2: float | Mapping[str, float | None],
    sun: SunPosition,
    now: float,
    latched: bool = False,
) -> dict:
    """采集一轮多路辐照、落盘，再按落盘值追日。"""

    steps: list[dict] = []
    violations: list[str] = []

    sample = field.acquire(dni_w_m2, now)
    steps.append({"component": "field", "action": "acquire", "detail": sample.as_dict()})
    for event in sample.events:
        steps.append(
            {
                "component": "field",
                "action": "meter_recovered" if event["event"] == "meter_recovered" else "meter_rejected",
                "detail": dict(event),
            }
        )
    if not sample.accepted:
        report = field.track(sun, now, latched)
        steps.extend(report["steps"])
        violations.extend(report["violations"])
        violations.append("irradiance_confidence_low")
        return {
            "ok": False,
            "mode": report.get("mode", ""),
            "reason": sample.reason,
            "steps": steps,
            "violations": violations,
            "aimed": report.get("aimed", 0),
        }

    durable = field.persist_irradiance()
    verdict = field.meter.latest()
    steps.append(
        {
            "component": "field",
            "action": "persist_irradiance",
            "detail": {
                "dni_w_m2": round(durable, 2),
                "confidence": round(verdict.confidence, 4) if verdict is not None else 0.0,
                "confidence_level": "low" if verdict is None else verdict.confidence_level,
                "aggregation": "none" if verdict is None else verdict.aggregation,
                "digest": "" if verdict is None else verdict.digest,
                "matches_current": field.meter.durable_matches_current(),
                "persisted": field.meter.latest_accepted(),
            },
        }
    )
    if not field.meter.latest_accepted() or durable <= 0.0 or not field.meter.durable_matches_current():
        report = field.track(sun, now, latched)
        steps.extend(report["steps"])
        violations.extend(report["violations"])
        violations.append("irradiance_confidence_low")
        return {
            "ok": False,
            "mode": report.get("mode", ""),
            "reason": "irradiance_confidence_low",
            "steps": steps,
            "violations": violations,
            "aimed": report.get("aimed", 0),
        }

    report = field.track(sun, now, latched)
    steps.extend(report["steps"])
    violations.extend(report["violations"])
    return {
        "ok": report["ok"] and not violations,
        "mode": report.get("mode", ""),
        "reason": report.get("reason", ""),
        "steps": steps,
        "violations": violations,
        "aimed": report.get("aimed", 0),
    }
