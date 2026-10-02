"""多路读数、落盘快照与审计的一致性核对。

现场最担心的是"盘上那份是错的"。这里提供离线核对：

1. **哈希链完整性**——原始读数链、决策链、事件台账链逐行重算 SHA256，
   任何一行被改、被删都会在断链处报错；
2. **重放复算**——按原始读数链重新跑一遍表决器，逐周期比对可信度、
   融合值、提交/挂起与逐路判定，保证"多路读数 → 决策"可复现；
3. **快照互认**——决策链记录的 ``snapshot_path`` 与盘上融合快照的
   ``decision_hash`` 必须互相指认，且最新快照必须等于最后一条决策；
4. **审计三方核对**——审计流水（audit-*.jsonl）里每个融合周期都要有
   一条对应的 ``irradiance_fused`` 记录，周期、提交状态、值、可信度
   分档必须一致；每起故障／恢复也要有对应的台账与审计事件。
"""

from __future__ import annotations

import os
from typing import Any

from .chain import ChainBroken
from .persist import FUSION_DOC
from .voting import IrradianceVoter

AUDIT_GLOB_PREFIX = "audit-"


class ConsistencyReport:
    """逐步收集核对结果；任何一条失败都让整份报告不通过。"""

    def __init__(self) -> None:
        self.ok = True
        self.checks: list[dict] = []
        self.errors: list[str] = []
        self.cycles_checked = 0
        self.incidents_checked = 0

    def pass_(self, name: str, detail: str = "") -> None:
        self.checks.append({"name": name, "ok": True, "detail": detail})

    def fail(self, name: str, detail: str) -> None:
        self.ok = False
        self.errors.append(f"{name}: {detail}")
        self.checks.append({"name": name, "ok": False, "detail": detail})

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "cycles_checked": self.cycles_checked,
            "incidents_checked": self.incidents_checked,
            "checks": self.checks,
            "errors": self.errors,
        }


def _load_audit(root: str) -> list[dict[str, Any]]:
    """读取审计流水（与 JsonlJournal 同目录约定）。"""

    import json

    records: list[dict[str, Any]] = []
    for name in sorted(os.listdir(root)):
        if not (name.startswith(AUDIT_GLOB_PREFIX) and name.endswith(".jsonl")):
            continue
        with open(os.path.join(root, name), "r", encoding="utf-8") as handle:
            records.extend(json.loads(line) for line in handle if line.strip())
    return records


def verify_field_irradiance(
    persistence,
    *,
    audit_root: str | None = None,
    voter: IrradianceVoter | None = None,
) -> dict[str, Any]:
    """核对镜场多路辐照的全部落盘材料，返回结构化报告。"""

    report = ConsistencyReport()
    chains = persistence.fusion_chains()

    # 1. 三条哈希链的完整性。
    chain_rows: dict[str, list[dict]] = {}
    for label, chain in chains.items():
        try:
            result = chain.verify()
            chain_rows[label] = chain.read_all()
            report.pass_(f"chain_{label}", f"{result['count']} 行哈希链完整")
        except ChainBroken as error:
            report.fail(f"chain_{label}", error.message)
            chain_rows[label] = []

    raw_rows = chain_rows.get("raw", [])
    decision_rows = chain_rows.get("decisions", [])
    incident_rows = chain_rows.get("incidents", [])

    # 2. 决策行数必须与原始读数行数一致（一一对应）。
    if len(raw_rows) != len(decision_rows):
        report.fail(
            "raw_decision_count",
            f"原始读数 {len(raw_rows)} 行，决策 {len(decision_rows)} 行，周期数不一致",
        )

    # 3. 重放原始读数链，逐周期复算并与决策行比对。
    if raw_rows and voter is None:
        names = sorted(raw_rows[0].get("readings", {}).keys())
        voter = IrradianceVoter(names)
    if voter is not None:
        _replay_and_compare(voter, raw_rows, decision_rows, report)

    # 4. 事件台账：每条事件都必须能在某周期的 incidents 中找到。
    decision_incidents = {
        (item.get("cycle"), item.get("name"), item.get("kind"))
        for row in decision_rows
        for item in row.get("incidents", [])
    }
    for row in incident_rows:
        key = (row.get("cycle"), row.get("name"), row.get("kind"))
        if key not in decision_incidents:
            report.fail("incident_orphan", f"台账事件在任何决策周期里都找不到: {key}")
        report.incidents_checked += 1
    report.pass_("incidents", f"{len(incident_rows)} 起故障/恢复事件全部与决策对应")

    # 5. 落盘快照与最后一条决策互认。
    _verify_snapshot(persistence, decision_rows, report)

    # 6. 与审计流水三方核对。
    root = audit_root or persistence.store.root
    audit_rows = _load_audit(root)
    if audit_rows:
        _verify_audit(audit_rows, decision_rows, incident_rows, report)
    else:
        report.pass_("audit_present", "无审计流水，跳过三方核对（新存储目录）")

    return report.as_dict()


def _replay_and_compare(
    voter: IrradianceVoter,
    raw_rows: list[dict],
    decision_rows: list[dict],
    report: ConsistencyReport,
) -> None:
    by_cycle = {int(row["cycle"]): row for row in decision_rows}
    for raw in raw_rows:
        cycle = int(raw["cycle"])
        replay = voter.cycle(dict(raw["readings"]), float(raw["ts"]), str(raw["mode"]))
        stored = by_cycle.get(cycle)
        if stored is None:
            report.fail("replay_decision_missing", f"周期 {cycle} 没有决策记录")
            continue
        mismatches: list[str] = []
        if bool(stored.get("committed")) != replay.committed:
            mismatches.append("committed")
        stored_value = stored.get("dni_w_m2")
        replay_value = replay.value
        if replay_value is None:
            if stored_value is not None:
                mismatches.append("dni_w_m2")
        elif stored_value is None or abs(float(stored_value) - replay_value) > 1e-6:
            mismatches.append("dni_w_m2")
        if stored.get("confidence", {}).get("level") != replay.confidence.level:
            mismatches.append("confidence.level")
        if stored.get("fusion_rule") != replay.fusion_rule:
            mismatches.append("fusion_rule")
        stored_states = {item["name"]: item["state"] for item in stored.get("channels", [])}
        replay_states = {item.name: item.state for item in replay.channels}
        if stored_states != replay_states:
            mismatches.append(f"channel_states {stored_states} != {replay_states}")
        if mismatches:
            report.fail("replay_mismatch", f"周期 {cycle} 复算不一致: {', '.join(mismatches)}")
        report.cycles_checked += 1
    if report.cycles_checked:
        report.pass_("replay", f"{report.cycles_checked} 个周期重放复算全部一致")


def _verify_snapshot(persistence, decision_rows: list[dict], report: ConsistencyReport) -> None:
    snapshot = persistence.read_fusion()
    if not decision_rows:
        if snapshot is not None:
            report.fail("snapshot_without_decision", "盘上有融合快照但决策链为空")
        else:
            report.pass_("snapshot", "无快照、无决策（尚未融合）")
        return
    if snapshot is None:
        report.fail("snapshot_missing", "决策链非空但盘上没有融合快照")
        return
    last = decision_rows[-1]
    decision_hash = snapshot.get("decision_hash")
    if decision_hash != last.get("hash"):
        report.fail(
            "snapshot_decision_hash",
            f"快照指向 {str(decision_hash)[:12]}…，链尾为 {str(last.get('hash'))[:12]}…",
        )
    if snapshot.get("cycle") != last.get("cycle"):
        report.fail("snapshot_cycle", "盘上快照不是最后一条决策")
    else:
        report.pass_("snapshot", f"快照与链尾决策（周期 {last.get('cycle')}）互认一致")
    expected_path = persistence.store.path_of(FUSION_DOC)
    if last.get("snapshot_path") != expected_path:
        report.fail("snapshot_path", "决策记录的快照路径与实际文档路径不一致")


def _verify_audit(
    audit_rows: list[dict],
    decision_rows: list[dict],
    incident_rows: list[dict],
    report: ConsistencyReport,
) -> None:
    fused = [
        row
        for row in audit_rows
        if row.get("action") == "irradiance_fused"
    ]
    if len(fused) != len(decision_rows):
        report.fail(
            "audit_cycle_count",
            f"审计融合记录 {len(fused)} 条，决策链 {len(decision_rows)} 行",
        )
        return
    by_cycle = {int(row["cycle"]): row for row in decision_rows}
    for entry in fused:
        payload = entry.get("payload", {})
        cycle = payload.get("cycle")
        stored = by_cycle.get(int(cycle)) if cycle is not None else None
        if stored is None:
            report.fail("audit_cycle_missing", f"审计周期 {cycle} 在决策链中不存在")
            continue
        if bool(payload.get("committed")) != bool(stored.get("committed")):
            report.fail("audit_commit_mismatch", f"周期 {cycle} 提交状态与决策不一致")
        if payload.get("confidence_level") != stored.get("confidence", {}).get("level"):
            report.fail("audit_level_mismatch", f"周期 {cycle} 可信度分档与决策不一致")
        audit_value = payload.get("dni_w_m2")
        stored_value = stored.get("dni_w_m2")
        if audit_value != stored_value:
            report.fail("audit_value_mismatch", f"周期 {cycle} 融合值与决策不一致")
    report.pass_("audit_cycles", f"{len(fused)} 条融合审计与决策链逐周期一致")

    audit_events = {
        (
            item.get("payload", {}).get("cycle"),
            item.get("asset"),
            "recovered" if item.get("action") == "irradiance_meter_recovered" else "fault",
        )
        for item in audit_rows
        if item.get("action") in ("irradiance_meter_fault", "irradiance_meter_recovered")
    }
    for row in incident_rows:
        key = (row.get("cycle"), row.get("name"), row.get("kind"))
        if key not in audit_events:
            report.fail("audit_incident_missing", f"台账事件缺少对应审计: {key}")
    report.pass_(
        "audit_incidents",
        f"{len(incident_rows)} 起事件台账与审计流水一致",
    )
