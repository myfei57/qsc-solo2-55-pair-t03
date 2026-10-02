"""多路辐照计表决演示脚本。

三台辐照计（irr-a / irr-b / irr-c）同步采集，脚本依次注入现场最常见的
三类部分失败：

- irr-a 在第 8 个周期起出现 +8% 偏置漂移，连续 3 周期被升级为故障，
  之后恢复；
- irr-b 在第 24 个周期起卡死在固定读数（同伴仍在随太阳缓变），连续 3
  个周期卡死标记后升级为故障，第 36 个周期恢复；
- irr-c 在第 39 个周期通信中断（null）3 个周期后恢复。

工况按 startup → normal_sun → cloud_transient → normal_sun → shutdown
切换，用来展示"可信均值还是最保守值"是随工况定死的。每个周期打印一行，
故障/恢复即时提示，末尾做三路哈希链 + 落盘快照 + 审计的一致性核对。
"""

from __future__ import annotations

from .clock import ManualClock
from .field.voting import (
    MODE_CLOUD,
    MODE_NORMAL,
    MODE_SHUTDOWN,
    MODE_STARTUP,
)

PERIOD_S = 30.0
_BASE = 820.0
_SLOPE = 3.0
_STUCK_VALUE = _BASE + _SLOPE * 23 - 1.0


def _truth(cycle: int) -> float:
    """真实 DNI：随时间缓慢变化的基线（云团过境段叠加一次下跌）。"""

    base = _BASE + _SLOPE * cycle
    if 19 <= cycle <= 22:
        base -= 180.0
    return base


def _mode_for(cycle: int) -> str:
    if cycle <= 4:
        return MODE_STARTUP
    if cycle <= 16:
        return MODE_NORMAL
    if cycle <= 22:
        return MODE_CLOUD
    if cycle <= 43:
        return MODE_NORMAL
    return MODE_SHUTDOWN


def _scripted_readings(cycle: int, truth: float) -> dict[str, float | None]:
    a = truth
    b = truth - 1.0
    c = truth + 1.0
    # irr-a：第 8 周期起 +8% 偏置，第 14 周期恢复。
    if 8 <= cycle <= 13:
        a = truth * 1.08
    # irr-b：第 24 周期起卡死在第 23 周期的值，第 36 周期恢复。
    if 24 <= cycle <= 35:
        b = _STUCK_VALUE
    # irr-c：第 39 周期起通信中断 3 个周期。
    if 39 <= cycle <= 41:
        c = None
    return {"irr-a": a, "irr-b": b, "irr-c": c}


def run_irradiance_demo(platform, clock: ManualClock, cycles: int = 46) -> dict:
    """推进演示并收集每个周期的融合结论。"""

    platform.start()
    rows: list[dict] = []
    events: list[dict] = []
    for cycle in range(1, cycles + 1):
        clock.advance(PERIOD_S)
        truth = _truth(cycle)
        mode = _mode_for(cycle)
        readings = _scripted_readings(cycle, truth)
        result = platform.fuse_irradiance(readings, mode)

        # 镜场按"已落盘"那份判定模式（中午太阳在地平线上），观察保守姿态。
        field_report = platform.field.track(platform.sun_at(720.0), clock.now())
        field_mode = field_report["mode"]
        aimed = field_report.get("aimed", 0)

        rows.append(
            {
                "cycle": cycle,
                "mode": mode,
                "truth": round(truth, 1),
                "readings": result["channels"],
                "fused": result["dni_w_m2"],
                "rule": result["fusion_rule"],
                "committed": result["committed"],
                "reason": result["reason"],
                "level": result["confidence"]["level"],
                "score": result["confidence"]["score"],
                "field_mode": field_mode,
                "aimed": aimed,
            }
        )
        for incident in result["incidents"]:
            events.append(incident)

    verification = platform.verify_irradiance()
    policy = platform.field.fusion_status()["policy"]
    return {
        "policy": policy,
        "cycles": rows,
        "events": events,
        "faulty_now": platform.field.voter.faulty(),
        "held_now": platform.field.fusion_held(),
        "durable_dni_w_m2": round(platform.field.durable_irradiance(), 3),
        "verification": verification,
    }
