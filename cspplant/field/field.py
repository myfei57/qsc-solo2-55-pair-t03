"""镜场控制器。

把"辐照度 → 策略 → 定日镜动作 → 调度指令"这条链串起来，并守住两条顺序约束：

1. 追日只用**已落盘且未过期**的辐照度；内存里更新的当前值不能直接拿来对焦。
2. 清洗／检修窗口变化后必须重新求值避让区；批次换了而避让范围没跟着换，
   退出聚焦的镜子就会停在朝着工位的姿态上。

控制器只调用各组件公开的方法，不碰它们的私有字段。
"""

from __future__ import annotations

import threading

from ..config import FieldLimits, HeliostatLimits, IrradianceLimits
from ..errors import AlignmentError, InterlockError
from ..heliostat.aim import AimAngles, AimPoint, aim_angles_for, stow_angles
from ..heliostat.drive import DEFOCUS, STOWED, TRACKING, HeliostatDrive
from ..heliostat.registry import HeliostatRegistry
from ..heliostat.sun import SunPosition
from ..heliostat.zone import ZoneGuard
from ..ns.zones import ZoneRegistry
from .dispatch import DispatchController, DispatchOutcome
from .irradiance import IrradianceMeter, IrradianceSample
from .manual import ManualLease, ManualOverride
from .persist import FieldPersistence
from .snapshot import FieldSnapshot
from .strategy import FOCUS, NOT_EVALUATED, STANDBY, STOW, FieldStrategy, StrategyDecision
from .sweep import SweepPlanner, SweepWindow
from .voting import (
    SEALED_POLICY,
    FusedIrradiance,
    IrradianceVoter,
)

MAX_STEPS = 720

# 从镜面反射到吸热器窗口的光学效率（余弦损失、遮挡、溢出合计）。
OPTICAL_EFFICIENCY = 0.62


class FieldController:
    """定日镜场控制器。"""

    def __init__(
        self,
        registry: HeliostatRegistry,
        limits: FieldLimits,
        heliostat_limits: HeliostatLimits,
        irradiance_limits: IrradianceLimits,
        zones: ZoneRegistry,
        guard: ZoneGuard,
        persistence: FieldPersistence,
        aim_point: AimPoint,
        maintenance_reach_m: float = 24.0,
        manual_hold_s: float = 900.0,
        irradiance_channels: list[str] | None = None,
    ) -> None:
        self._registry = registry
        self._limits = limits
        self._heliostat_limits = heliostat_limits
        self._zones = zones
        self._guard = guard
        self._aim_point = aim_point
        self._maintenance_reach_m = float(maintenance_reach_m)
        self._meter = IrradianceMeter(irradiance_limits, persistence)
        self._strategy = FieldStrategy(limits)
        self._dispatch = DispatchController(limits)
        self._sweeps = SweepPlanner(limits)
        self._manual = ManualOverride(manual_hold_s)
        # 多路辐照计表决：装配时给了通道名才启用；不启用时保持单路老链路。
        self._voter: IrradianceVoter | None = (
            IrradianceVoter(list(irradiance_channels)) if irradiance_channels else None
        )
        self._last_fusion: FusedIrradiance | None = None
        self._fusion_held = False
        self._fusion_hold_reason = ""
        self._fusions = 0
        self._holds = 0
        self._drives: dict[str, HeliostatDrive] = {
            mirror.name: HeliostatDrive(mirror.name, heliostat_limits, azimuth_deg=180.0)
            for mirror in registry.all()
        }
        self._last_decision: StrategyDecision | None = None
        self._lock = threading.RLock()
        self._restore()

    def _restore(self) -> None:
        """重启后把仍在窗口里的镜子重新压进避让姿态。"""

        stored = self._meter.persistence.read_window()
        if stored is None or not stored["mirrors"]:
            return
        self._sweeps.begin(stored["mirrors"], stored["zone"], stored["opened_at"])
        self.refresh_zones(stored["opened_at"])

    @property
    def limits(self) -> FieldLimits:
        return self._limits

    @property
    def aim_point(self) -> AimPoint:
        return self._aim_point

    @property
    def meter(self) -> IrradianceMeter:
        return self._meter

    @property
    def dispatch(self) -> DispatchController:
        return self._dispatch

    @property
    def sweeps(self) -> SweepPlanner:
        return self._sweeps

    @property
    def manual(self) -> ManualOverride:
        return self._manual

    @property
    def guard(self) -> ZoneGuard:
        return self._guard

    @property
    def zones(self) -> ZoneRegistry:
        return self._zones

    def drives(self) -> list[HeliostatDrive]:
        """返回全部定日镜驱动（按名字排序）。"""

        return [self._drives[name] for name in sorted(self._drives)]

    def drive(self, name: str) -> HeliostatDrive:
        """按名字取一台定日镜驱动。"""

        try:
            return self._drives[name]
        except KeyError as error:
            raise InterlockError(f"镜场里没有 {name}", component="field") from error

    # ---- 辐照度 -------------------------------------------------------

    def acquire(self, dni_w_m2: float, now: float) -> IrradianceSample:
        """采集一次 DNI。"""

        with self._lock:
            return self._meter.sample(dni_w_m2, now)

    def persist_irradiance(self) -> float:
        """把当前 DNI 落盘。"""

        with self._lock:
            return self._meter.persist()

    def durable_irradiance(self) -> float:
        """返回已落盘的 DNI。"""

        return self._meter.durable()

    def durable_ready(self, now: float) -> bool:
        """落盘快照是否可用（存在、与当前值一致、未过期）。

        多路表决启用时还要满足：最近一次融合是已提交状态，不是挂起。
        """

        if self._voter is not None and self._fusion_held:
            return False
        return (
            self._meter.durable() > 0.0
            and self._meter.durable_matches_current()
            and not self._meter.stale(now)
        )

    # ---- 多路辐照计表决 -----------------------------------------------

    @property
    def voter(self) -> IrradianceVoter | None:
        """多路表决器；装配时未给通道名则为 ``None``。"""

        return self._voter

    def fusion_active(self) -> bool:
        """多路融合链路是否已启用。"""

        return self._voter is not None

    def last_fusion(self) -> FusedIrradiance | None:
        """返回最近一次融合结论。"""

        return self._last_fusion

    def fusion_held(self) -> bool:
        """当前是否处于"不写死值、镜场保守等待"状态。"""

        return self._fusion_held

    def fusion_status(self) -> dict:
        """汇总多路融合状态（控制台展示与审计用）。"""

        last = self._last_fusion
        return {
            "active": self._voter is not None,
            "channels": [] if self._voter is None else self._voter.names,
            "channel_states": (
                {} if self._voter is None else {name: self._voter.state_of(name) for name in self._voter.names}
            ),
            "faulty": [] if self._voter is None else self._voter.faulty(),
            "held": self._fusion_held,
            "hold_reason": self._fusion_hold_reason,
            "cycles": 0 if self._voter is None else self._voter.cycle_count(),
            "fusions": self._fusions,
            "holds": self._holds,
            "policy": SEALED_POLICY.describe(),
            "last": None if last is None else last.as_dict(),
        }

    def fuse_irradiance(self, readings: dict[str, float | None], mode: str, now: float) -> FusedIrradiance:
        """多路采集一个周期：判定 → 融合 → 门控落盘。

        可信度不足或工况未知时**只更新挂起标记、不写死值**；提交时才把
        融合值喂给单路辐照计并原子落盘，让追日、热功率估算等下游链路
        仍然只认"已落盘且未过期"那一份。
        """

        if self._voter is None:
            raise InterlockError("镜场未装配多路辐照计通道", component="field")
        with self._lock:
            fused = self._voter.cycle(readings, now, mode)
            self._last_fusion = fused
            persistence = self._meter.persistence

            raw_record = persistence.append_raw_readings(
                {
                    "cycle": fused.cycle,
                    "ts": round(fused.ts, 3),
                    "mode": fused.mode,
                    "readings": {
                        verdict.name: verdict.as_dict()["raw"] for verdict in fused.channels
                    },
                }
            )
            for incident in fused.incidents:
                persistence.append_incident(
                    {**incident.as_dict(), "policy_version": fused.policy_version}
                )

            if fused.committed:
                assert fused.value is not None
                sample = self._meter.sample(fused.value, now)
                if not sample.accepted:
                    # 融合值本身落在物理量程外属于策略/配置错误：宁可不落盘。
                    self._fusion_held = True
                    self._fusion_hold_reason = "fused_value_out_of_range"
                    self._holds += 1
                else:
                    self._meter.persist()
                    self._fusion_held = False
                    self._fusion_hold_reason = ""
                    self._fusions += 1
            else:
                self._fusion_held = True
                self._fusion_hold_reason = fused.reason
                self._holds += 1

            snapshot = {
                "status": "committed" if fused.committed else "held",
                "committed": fused.committed,
                "cycle": fused.cycle,
                "ts": round(fused.ts, 3),
                "mode": fused.mode,
                "fusion_rule": fused.fusion_rule,
                "reason": fused.reason,
                "confidence": fused.confidence.as_dict(),
                "channels": [verdict.as_dict() for verdict in fused.channels],
                "incidents": [incident.as_dict() for incident in fused.incidents],
                "policy_version": fused.policy_version,
                "policy_fingerprint": fused.policy_fingerprint,
                "raw_hash": raw_record["hash"],
            }
            if fused.committed:
                snapshot["dni_w_m2"] = round(float(fused.value), 3)
            path = persistence.write_fusion(snapshot)
            decision_record = persistence.append_decision({**snapshot, "snapshot_path": path})
            # 决策行与快照互认：把决策哈希补进快照，形成快照 ↔ 决策链的交叉引用。
            snapshot["decision_hash"] = decision_record["hash"]
            persistence.write_fusion(snapshot)
            return fused

    # ---- 策略与追日 ---------------------------------------------------

    def decide(self, sun: SunPosition, now: float, latched: bool = False) -> StrategyDecision:
        """给出本轮镜场模式，并记住它供状态快照读取。"""

        with self._lock:
            decision = self._strategy.decide(
                dni_w_m2=self._meter.current(),
                sun_above_horizon=sun.above_horizon,
                latched=latched,
                durable_ok=self.durable_ready(now),
            )
            # 多路融合挂起时，镜场必须保守待机，并把挂起原因带到模式判定里。
            if decision.mode == STANDBY and self.fusion_active() and self._fusion_held:
                decision = StrategyDecision(
                    STANDBY, f"irradiance_held:{self._fusion_hold_reason}", decision.dni_w_m2
                )
            self._last_decision = decision
            return decision

    def track(self, sun: SunPosition, now: float, latched: bool = False) -> dict:
        """追日编排：判定模式 → 下发目标 → 按固定步长转到位。"""

        with self._lock:
            steps: list[dict] = []
            violations: list[str] = []
            decision = self.decide(sun, now, latched)
            steps.append(
                {
                    "component": "field",
                    "action": "decide",
                    "detail": {
                        **decision.as_dict(),
                        "durable_ready": self.durable_ready(now),
                        "fusion_cycle": 0 if self.last_fusion() is None else self.last_fusion().cycle,
                    },
                }
            )

            if decision.mode == STOW:
                moved = self._stow_all(clear_manual=latched)
                steps.append({"component": "field", "action": "stow", "detail": {"mirrors": moved}})
                return {
                    "ok": True,
                    "mode": decision.mode,
                    "reason": decision.reason,
                    "steps": steps,
                    "violations": violations,
                    "aimed": 0,
                }

            if not decision.focuses:
                parked = self._park_all()
                steps.append({"component": "field", "action": "standby", "detail": {"mirrors": parked}})
                return {
                    "ok": True,
                    "mode": STANDBY,
                    "reason": decision.reason,
                    "steps": steps,
                    "violations": violations,
                    "aimed": 0,
                }

            durable = self._meter.durable()
            if durable <= 0.0:
                violations.append("irradiance_not_durable")
                return {
                    "ok": False,
                    "mode": decision.mode,
                    "reason": "irradiance_not_durable",
                    "steps": steps,
                    "violations": violations,
                    "aimed": 0,
                }

            aimed = 0
            skipped = 0
            for mirror in self._registry.all():
                drive = self._drives[mirror.name]
                if drive.excluded or drive.manual:
                    skipped += 1
                    continue
                target = aim_angles_for(mirror.x_m, mirror.y_m, mirror.z_m, sun, self._aim_point)
                drive.aim(target)
                aimed += 1
            steps.append(
                {
                    "component": "field",
                    "action": "aim",
                    "detail": {"aimed": aimed, "skipped": skipped, "dni_w_m2": round(durable, 2)},
                }
            )
            aligned = self._settle()
            steps.append(
                {
                    "component": "field",
                    "action": "settle",
                    "detail": {"aligned": aligned, "aimed": aimed},
                }
            )
            if aligned < aimed:
                violations.append("aim_incomplete")
            return {
                "ok": not violations,
                "mode": decision.mode,
                "reason": decision.reason,
                "steps": steps,
                "violations": violations,
                "aimed": aligned,
            }

    def _settle(self) -> int:
        """推进所有驱动直到到位或步数用尽，返回到位台数。"""

        pending = [drive for drive in self._drives.values() if drive.state == TRACKING]
        for _ in range(MAX_STEPS):
            if not pending:
                break
            for drive in list(pending):
                drive.step(1.0)
            pending = [drive for drive in pending if not drive.aligned()]
        return sum(1 for drive in self._drives.values() if drive.aligned())

    def _park_all(self) -> int:
        """把非手动、非避让的镜子转到离焦姿态。"""

        moved = 0
        for drive in self._drives.values():
            if drive.manual or drive.excluded:
                continue
            drive.defocus()
            moved += 1
        for _ in range(MAX_STEPS):
            if all(
                drive.at_target() or drive.manual or drive.excluded for drive in self._drives.values()
            ):
                break
            for drive in self._drives.values():
                if not drive.manual and not drive.excluded:
                    drive.step(1.0)
        return moved

    def _stow_all(self, clear_manual: bool = False) -> int:
        """把所有镜面收起；故障闩锁时连手动接管一起清掉。"""

        if clear_manual:
            self._manual.clear()
        moved = 0
        for drive in self._drives.values():
            drive.stow()
            moved += 1
        for _ in range(MAX_STEPS):
            if all(drive.at_target() for drive in self._drives.values()):
                break
            for drive in self._drives.values():
                drive.step(1.0)
        return moved

    # ---- 避让区与清洗窗口 ---------------------------------------------

    def refresh_zones(self, now: float) -> list[dict]:
        """按当前清洗窗口重新求值避让区，并把镜子压进安全姿态。"""

        with self._lock:
            active = self._sweeps.active()
            in_window = set(active.mirrors) if active is not None else set()
            refreshed: list[dict] = []
            for name in sorted(self._drives):
                drive = self._drives[name]
                if name not in in_window:
                    drive.mark_excluded(False)
                    continue
                mirror = self._registry.get(name)
                verdict = self._guard.evaluate(
                    mirror.x_m, mirror.y_m, drive.azimuth_deg, self._maintenance_reach_m
                )
                drive.mark_excluded(True)
                if verdict.allowed:
                    drive.defocus()
                else:
                    drive.stow()
                refreshed.append({"mirror": name, "posture": DEFOCUS if verdict.allowed else STOWED, **verdict.as_dict()})
            self._settle_excluded()
            return refreshed

    def _settle_excluded(self) -> None:
        """把避让中的镜子推进到新姿态。"""

        targets = [drive for drive in self._drives.values() if drive.excluded]
        for _ in range(MAX_STEPS):
            if not targets or all(drive.at_target() for drive in targets):
                break
            for drive in targets:
                drive.step(1.0)

    def open_window(self, mirrors: list[str], zone: str, now: float) -> SweepWindow:
        """打开清洗窗口并立即刷新避让区。"""

        with self._lock:
            window = self._sweeps.begin(mirrors, zone, now)
            self._meter.persistence.write_window(list(window.mirrors), window.zone, window.opened_at)
            self.refresh_zones(now)
            return window

    def close_window(self, now: float) -> SweepWindow:
        """关闭清洗窗口，镜子放回自动追日。"""

        with self._lock:
            window = self._sweeps.finish(now)
            self._meter.persistence.clear_window()
            for drive in self._drives.values():
                drive.mark_excluded(False)
            return window

    # ---- 手动接管 -----------------------------------------------------

    def manual_aim(
        self,
        mirror: str,
        azimuth_deg: float,
        elevation_deg: float,
        operator: str,
        now: float,
        hold_s: float | None = None,
    ) -> ManualLease:
        """操作员手动接管一台镜子，自动追日随后不得覆盖它。"""

        with self._lock:
            drive = self.drive(mirror)
            lease = self._manual.request(mirror, azimuth_deg, elevation_deg, operator, now, hold_s)
            drive.set_manual(True)
            drive.aim(AimAngles(lease.azimuth_deg, lease.elevation_deg), auto=False)
            for _ in range(MAX_STEPS):
                if drive.aligned():
                    break
                drive.step(1.0)
            if not drive.aligned():
                raise AlignmentError(
                    f"{mirror} 在 {MAX_STEPS} 步内没有转到手动目标", component="heliostat"
                )
            return lease

    def release_manual(self, mirror: str, now: float) -> bool:
        """释放手动接管，把镜子交回自动追日。"""

        with self._lock:
            if not self._manual.active(mirror, now):
                return False
            released = self._manual.release(mirror)
            self._drives[mirror].set_manual(False)
            return released

    def expire_manual(self, now: float) -> list[str]:
        """清理过期租约，并把对应镜子交回自动追日。"""

        with self._lock:
            gone = self._manual.expire(now)
            for name in gone:
                self._drives[name].set_manual(False)
            return gone

    # ---- 调度与出力 ---------------------------------------------------

    def dispatch_load(self, pct: float, writer: str, ceiling_pct: float) -> DispatchOutcome:
        """登记调度负荷，并按本地保护上限求值。"""

        with self._lock:
            self._dispatch.request(pct, writer)
            return self._dispatch.resolve(ceiling_pct)

    def step_dispatch(self, dt: float) -> float:
        """按斜率推进执行负荷。"""

        return self._dispatch.step(dt)

    def focusing_count(self) -> int:
        """返回当前真正把光打进吸热器的镜子数量。"""

        return sum(
            1
            for drive in self._drives.values()
            if drive.aimed_at_receiver and not drive.excluded and not drive.manual and drive.aligned()
        )

    def thermal_power_kw(self) -> float:
        """按已落盘辐照度与对焦镜面积估算热功率。"""

        area = self._registry.total_area_m2()
        focusing = self.focusing_count()
        total = max(1, len(self._drives))
        fraction = focusing / total
        return (
            area
            * fraction
            * self._meter.durable()
            * self._heliostat_limits.reflectivity
            * OPTICAL_EFFICIENCY
            / 1000.0
        )

    def stow_all(self) -> int:
        """强制全场收起（保护动作）；收起原因由调用方写审计。"""

        with self._lock:
            return self._stow_all(clear_manual=False)

    def status(self) -> dict:
        """汇总镜场状态。"""

        decision = self._last_decision
        return {
            "mode": NOT_EVALUATED if decision is None else decision.mode,
            "reason": "not_evaluated" if decision is None else decision.reason,
            "dni_w_m2": round(self._meter.current(), 2),
            "durable_dni_w_m2": round(self._meter.durable(), 2),
            "samples": self._meter.sample_count(),
            "rejected": self._meter.rejected(),
            "persists": self._meter.persist_count(),
            "sampled_at": round(self._meter.sampled_at(), 3),
            "durable_at": round(self._meter.durable_at(), 3),
            "decisions": self._strategy.evaluations(),
            "mirrors": self._registry.describe(),
            "focusing": self.focusing_count(),
            "excluded": sum(1 for drive in self._drives.values() if drive.excluded),
            "manual": self._manual.count(),
            "manual_leases": [lease.as_dict() for lease in self._manual.held()],
            "windows": len(self._sweeps.windows()),
            "swept_mirrors": self._sweeps.swept_mirrors(),
            "zones_used": self._sweeps.zones_used(),
            "zone_vetoes": self._guard.vetoes(),
            "dispatch_pct": round(self._dispatch.current(), 3),
            "dispatch_target_pct": round(self._dispatch.target(), 3),
            "dispatch_requested_pct": round(self._dispatch.requested(), 3),
            "dispatch_clamps": self._dispatch.clamps(),
            "dispatch_resolves": self._dispatch.resolves(),
            "fusion": self.fusion_status(),
        }

    def snapshot(self) -> FieldSnapshot:
        """返回镜场状态快照对象。"""

        decision = self._last_decision
        fusion_level = ""
        if self._last_fusion is not None:
            fusion_level = self._last_fusion.confidence.level
        return FieldSnapshot(
            mode=NOT_EVALUATED if decision is None else decision.mode,
            reason="not_evaluated" if decision is None else decision.reason,
            dni_w_m2=self._meter.current(),
            durable_dni_w_m2=self._meter.durable(),
            durable_ok=decision is not None and decision.mode == FOCUS,
            mirrors=len(self._drives),
            focusing=self.focusing_count(),
            excluded=sum(1 for drive in self._drives.values() if drive.excluded),
            manual=self._manual.count(),
            dispatch_pct=self._dispatch.current(),
            dispatch_target_pct=self._dispatch.target(),
            fusion_held=self._fusion_held,
            fusion_level=fusion_level,
        )

    def stow_angles_for(self, mirror: str) -> AimAngles:
        """返回某台镜子的收起姿态角度（供控制台展示）。"""

        drive = self.drive(mirror)
        return stow_angles(0.0, drive.azimuth_deg)
