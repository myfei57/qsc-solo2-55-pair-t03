"""镜场状态快照。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FieldSnapshot:
    """镜场某一时刻的汇总状态。"""

    mode: str
    reason: str
    dni_w_m2: float
    durable_dni_w_m2: float
    durable_ok: bool
    confidence: float
    confidence_level: str
    aggregation: str
    trusted_meters: tuple[str, ...]
    rejected_meters: tuple[str, ...]
    mirrors: int
    focusing: int
    excluded: int
    manual: int
    dispatch_pct: float
    dispatch_target_pct: float

    def as_dict(self) -> dict:
        return {
            "mode": self.mode,
            "reason": self.reason,
            "dni_w_m2": round(self.dni_w_m2, 2),
            "durable_dni_w_m2": round(self.durable_dni_w_m2, 2),
            "durable_ok": self.durable_ok,
            "confidence": round(self.confidence, 4),
            "confidence_level": self.confidence_level,
            "aggregation": self.aggregation,
            "trusted_meters": list(self.trusted_meters),
            "rejected_meters": list(self.rejected_meters),
            "mirrors": self.mirrors,
            "focusing": self.focusing,
            "excluded": self.excluded,
            "manual": self.manual,
            "dispatch_pct": round(self.dispatch_pct, 3),
            "dispatch_target_pct": round(self.dispatch_target_pct, 3),
        }
