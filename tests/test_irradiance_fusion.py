"""多路融合在平台层的门控、落盘、审计与跨重启一致性。"""

from __future__ import annotations

import json

import pytest

from cspplant.app import build_plant
from cspplant.config import default_config
from cspplant.field.chain import ChainBroken, HashChain
from cspplant.field.persist import RAW_CHAIN
from cspplant.field.voting import MODE_NORMAL, MODE_STARTUP

CHANNELS = ["irr-a", "irr-b", "irr-c"]


def _good(v=820.0):
    return {"irr-a": v, "irr-b": v + 1.0, "irr-c": v - 1.0}


def test_fusion_is_wired_by_default(platform):
    assert platform.field.fusion_active() is True
    assert platform.field.voter.names == CHANNELS
    assert platform.field.voter.cycle_count() == 0
    assert platform.field.voter.state_of("irr-a") == "good"
    assert platform.field.voter.states() == {"irr-a": "good", "irr-b": "good", "irr-c": "good"}
    assert platform.field.last_fusion() is None
    persistence = platform.field.meter.persistence
    assert persistence.fusion_chains()["raw"].topic == "field/irradiance-raw"
    assert persistence.fusion_chains()["decisions"].topic == "field/irradiance-decisions"
    assert persistence.fusion_chains()["incidents"].topic == "field/irradiance-incidents"


def test_cycle_count_and_last_fusion_advance(platform):
    platform.start()
    platform.fuse_irradiance(_good(820.0), MODE_NORMAL)
    platform.fuse_irradiance(_good(825.0), MODE_NORMAL)
    assert platform.field.voter.cycle_count() == 2
    assert platform.field.last_fusion().cycle == 2
    assert platform.field.last_fusion().value == pytest.approx(825.0)


def test_committed_fusion_persists_and_field_focuses(platform):
    platform.start()
    result = platform.fuse_irradiance(_good(820.0), MODE_NORMAL)
    assert result["committed"] is True
    assert platform.field.durable_irradiance() == pytest.approx(result["dni_w_m2"])

    report = platform.field.track(platform.sun_at(720.0), platform.clock.now())
    assert report["mode"] == "focus"
    assert report["aimed"] == len(platform.heliostats.names())


def test_partial_failure_keeps_last_good_durable_value(platform, clock):
    """两台故障、可信度不够时不覆盖盘上那份，镜场退回保守待机。"""

    platform.start()
    platform.fuse_irradiance(_good(820.0), MODE_NORMAL)
    assert platform.field.durable_ready(clock.now()) is True

    held = platform.fuse_irradiance({"irr-a": None, "irr-b": None, "irr-c": 820.0}, MODE_NORMAL)
    assert held["committed"] is False
    assert platform.field.fusion_held() is True
    # 盘上保留最后一次可信快照，不被坏值覆盖。
    assert platform.field.durable_irradiance() == pytest.approx(820.0)
    # 但快照已挂起，镜场不能对焦，只能保守待机。
    assert platform.field.durable_ready(clock.now()) is False
    report = platform.field.track(platform.sun_at(720.0), clock.now())
    assert report["mode"] == "standby"
    assert report["aimed"] == 0
    assert report["reason"].startswith("irradiance_held:")


def test_recovery_resumes_focus_and_audit_points_at_meter(platform, clock):
    platform.start()
    platform.fuse_irradiance(_good(820.0), MODE_NORMAL)
    for _ in range(3):
        clock.advance(30.0)
        platform.fuse_irradiance({"irr-a": 886.0, "irr-b": 821.0, "irr-c": 819.0}, MODE_NORMAL)
    faulty = platform.field.voter.faulty()
    assert faulty == ["irr-a"]

    audit_faults = [e for e in platform.audit.entries(200) if e["action"] == "irradiance_meter_fault"]
    assert audit_faults and audit_faults[0]["asset"] == "irr-a"
    assert audit_faults[0]["payload"]["offense"] == "peer_drift"

    for _ in range(3):
        clock.advance(30.0)
        platform.fuse_irradiance(_good(820.0), MODE_NORMAL)
    assert platform.field.voter.faulty() == []
    recovered = [e for e in platform.audit.entries(200) if e["action"] == "irradiance_meter_recovered"]
    assert recovered and recovered[0]["asset"] == "irr-a"
    assert platform.field.durable_ready(clock.now()) is True
    report = platform.field.track(platform.sun_at(720.0), clock.now())
    assert report["mode"] == "focus"


def test_confidence_is_published_to_audit(platform):
    platform.start()
    platform.fuse_irradiance(_good(820.0), MODE_STARTUP)
    entry = next(e for e in platform.audit.entries(50) if e["action"] == "irradiance_fused")
    payload = entry["payload"]
    assert payload["committed"] is True
    assert payload["confidence_level"] == "HIGH"
    assert 0.0 <= payload["confidence_score"] <= 1.0
    assert payload["trusted_count"] == 3
    assert payload["fusion_rule"] == "conservative_min"
    assert payload["policy_version"]
    assert payload["policy_fingerprint"]


def test_held_cycle_writes_warning_audit_without_value(platform):
    platform.start()
    platform.fuse_irradiance(_good(820.0), MODE_NORMAL)
    platform.fuse_irradiance({"irr-a": None, "irr-b": None, "irr-c": 820.0}, MODE_NORMAL)
    entry = next(
        e
        for e in platform.audit.entries(50)
        if e["action"] == "irradiance_fused" and e["payload"]["cycle"] == 2
    )
    assert entry["severity"] == "warning"
    assert entry["payload"]["committed"] is False
    assert entry["payload"]["dni_w_m2"] is None


def test_state_and_snapshot_expose_fusion(platform):
    platform.start()
    platform.fuse_irradiance(_good(820.0), MODE_NORMAL)
    status = platform.status()["field"]["fusion"]
    assert status["active"] is True
    assert status["held"] is False
    assert status["last"]["committed"] is True
    assert status["policy"]["fingerprint"]
    snapshot = platform.field.snapshot().as_dict()
    assert snapshot["fusion_held"] is False
    assert snapshot["fusion_level"] == "HIGH"


def test_chains_and_snapshot_survive_restart(platform, clock):
    platform.start()
    for cycle in range(4):
        clock.advance(30.0)
        platform.fuse_irradiance(_good(820.0 + cycle), MODE_NORMAL)

    root = platform.store.root
    restarted = build_plant(default_config(), clock=clock, storage_root=root)
    persistence = restarted.field.meter.persistence
    assert len(persistence.fusion_chains()["raw"].read_all()) == 4
    assert persistence.fusion_chains()["raw"].verify()["ok"] is True
    snapshot = persistence.read_fusion()
    assert snapshot["status"] == "committed"
    assert snapshot["cycle"] == 4
    assert restarted.field.durable_irradiance() == pytest.approx(823.0)


def test_consistency_report_passes_on_clean_history(platform, clock):
    platform.start()
    for cycle in range(6):
        clock.advance(30.0)
        mode = MODE_STARTUP if cycle < 2 else MODE_NORMAL
        platform.fuse_irradiance(_good(820.0 + cycle), mode)
    report = platform.verify_irradiance()
    assert report["ok"] is True, report["errors"]
    assert report["cycles_checked"] == 6
    names = {check["name"] for check in report["checks"]}
    assert {"chain_raw", "chain_decisions", "chain_incidents", "replay", "snapshot", "audit_cycles"} <= names


def test_tampered_chain_is_detected(platform, clock, tmp_path):
    platform.start()
    for cycle in range(5):
        clock.advance(30.0)
        platform.fuse_irradiance(_good(820.0 + cycle), MODE_NORMAL)

    root = platform.store.root
    raw_path = platform.field.meter.persistence.fusion_chains()["raw"].path
    lines = open(raw_path, encoding="utf-8").read().splitlines()
    record = json.loads(lines[2])
    record["readings"]["irr-a"] = 999.0
    lines[2] = json.dumps(record, ensure_ascii=False)
    with open(raw_path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")

    # 构造时重读链头或显式校验，二者任一都必须拒绝被篡改的行。
    with pytest.raises(ChainBroken):
        chain = HashChain(root, RAW_CHAIN)
        chain.verify()


def test_replay_mismatch_is_detected(platform, clock):
    """原始读数被改但哈希没重算时，重放复算会和决策链对不上（先破链重算）。"""

    platform.start()
    for cycle in range(4):
        clock.advance(30.0)
        platform.fuse_irradiance(_good(820.0 + cycle), MODE_NORMAL)

    persistence = platform.field.meter.persistence
    chain = persistence.fusion_chains()["raw"]
    raw_path = chain.path
    lines = open(raw_path, encoding="utf-8").read().splitlines()
    # 改读数并把链式哈希重算成自洽的：哈希链完整，但业务复算对不上。
    rebuilt = []
    prev = "0" * 64
    for index, line in enumerate(lines):
        record = json.loads(line)
        if index == 2:
            record["readings"]["irr-a"] = 500.0
        record.pop("hash", None)
        record["prev_hash"] = prev
        from cspplant.field.chain import canonical_hash

        record["hash"] = canonical_hash(record)
        prev = record["hash"]
        rebuilt.append(json.dumps(record, ensure_ascii=False))
    with open(raw_path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(rebuilt) + "\n")

    # 重新打开平台以重读链头，再跑一致性核对。
    restarted = build_plant(default_config(), clock=clock, storage_root=platform.store.root)
    report = restarted.verify_irradiance()
    assert report["ok"] is False
    assert any("replay_mismatch" in error for error in report["errors"])


def test_snapshot_without_matching_decision_fails(platform, clock):
    platform.start()
    platform.fuse_irradiance(_good(820.0), MODE_NORMAL)
    persistence = platform.field.meter.persistence
    persistence.write_fusion({**persistence.read_fusion(), "decision_hash": "deadbeef"})
    report = platform.verify_irradiance()
    assert report["ok"] is False
    assert any("snapshot_decision_hash" in error for error in report["errors"])
