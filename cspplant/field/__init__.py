"""镜场。

镜场把"辐照度 → 追日策略 → 定日镜动作 → 调度指令"这条链串起来。这里有两条
顺序约束：追日必须用**已落盘**的辐照度（重启后不能再拿旧快照对焦），检修或
清洗窗口打开后避让区必须重新求值（批次变了，避让范围也要跟着变）。
"""

from __future__ import annotations

from .dispatch import DispatchController, DispatchOutcome
from .field import FieldController
from .irradiance import IrradianceMeter, IrradianceSample
from .manual import ManualLease, ManualOverride
from .persist import FieldPersistence
from .snapshot import FieldSnapshot
from .strategy import FOCUS, NOT_EVALUATED, STANDBY, STOW, FieldStrategy, StrategyDecision
from .sweep import SweepPlanner, SweepWindow
from .voting import (
    FUSION_CONSERVATIVE_MAX,
    FUSION_CONSERVATIVE_MIN,
    FUSION_TRUSTED_MEAN,
    LEVEL_HIGH,
    LEVEL_LOW,
    LEVEL_MEDIUM,
    MODE_CLOUD,
    MODE_NORMAL,
    MODE_SHUTDOWN,
    MODE_STARTUP,
    SEALED_POLICY,
    ChannelVerdict,
    Confidence,
    FusedIrradiance,
    FusionPolicy,
    IrradianceVoter,
    MeterIncident,
)

__all__ = [
    "DispatchController",
    "DispatchOutcome",
    "FieldController",
    "IrradianceMeter",
    "IrradianceSample",
    "ManualLease",
    "ManualOverride",
    "FieldPersistence",
    "FieldSnapshot",
    "FieldStrategy",
    "StrategyDecision",
    "FOCUS",
    "STANDBY",
    "STOW",
    "NOT_EVALUATED",
    "SweepPlanner",
    "SweepWindow",
    "FusionPolicy",
    "SEALED_POLICY",
    "IrradianceVoter",
    "FusedIrradiance",
    "ChannelVerdict",
    "Confidence",
    "MeterIncident",
    "FUSION_TRUSTED_MEAN",
    "FUSION_CONSERVATIVE_MIN",
    "FUSION_CONSERVATIVE_MAX",
    "MODE_STARTUP",
    "MODE_NORMAL",
    "MODE_CLOUD",
    "MODE_SHUTDOWN",
    "LEVEL_HIGH",
    "LEVEL_MEDIUM",
    "LEVEL_LOW",
]
