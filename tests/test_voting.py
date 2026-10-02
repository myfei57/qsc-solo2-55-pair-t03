"""多路辐照计表决、可信度门控与密封融合策略。"""

from __future__ import annotations

import pytest

from cspplant.app import build_plant
from cspplant.config import default_config
from cspplant.errors import InterlockError
from cspplant.field.voting import (
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
    IrradianceVoter,
)

NAMES = ["irr-a", "irr-b", "irr-c"]


def _readings(a=820.0, b=821.0, c=819.0):
    return {"irr-a": a, "irr-b": b, "irr-c": c}


def test_sealed_policy_maps_mode_to_fixed_rule():
    """均值还是最保守值按工况定死，未知工况没有规则。"""

    assert SEALED_POLICY.fusion_for(MODE_NORMAL) == FUSION_TRUSTED_MEAN
    assert SEALED_POLICY.fusion_for(MODE_CLOUD) == FUSION_TRUSTED_MEAN
    assert SEALED_POLICY.fusion_for(MODE_STARTUP) == FUSION_CONSERVATIVE_MIN
    assert SEALED_POLICY.fusion_for(MODE_SHUTDOWN) == FUSION_CONSERVATIVE_MAX
    assert SEALED_POLICY.fusion_for("maintenance_override") is None
    # 策略指纹必须稳定：同一份策略在任何进程里算出来都一样。
    assert SEALED_POLICY.fingerprint() == SEALED_POLICY.fingerprint()
    assert len(SEALED_POLICY.fingerprint()) == 16


def test_policy_fingerprint_changes_when_rules_change():
    """规则随版本改动后指纹必须变，巡检才能核对现场用的是哪一份策略。"""

    from cspplant.field.voting import FusionPolicy

    changed = FusionPolicy(**{**SEALED_POLICY.canonical(), "drift_relative": 0.08})
    assert changed.fingerprint() != SEALED_POLICY.fingerprint()
    changed_mode = FusionPolicy(
        **{**SEALED_POLICY.canonical(), "mode_fusion": {**SEALED_POLICY.mode_fusion, MODE_NORMAL: FUSION_CONSERVATIVE_MIN}}
    )
    assert changed_mode.fingerprint() != SEALED_POLICY.fingerprint()


def test_unanimous_readings_commit_mean_with_high_confidence():
    voter = IrradianceVoter(NAMES)
    result = voter.cycle(_readings(800, 800, 800), 30.0, MODE_NORMAL)
    assert result.committed is True
    assert result.value == pytest.approx(800.0)
    assert result.fusion_rule == FUSION_TRUSTED_MEAN
    assert result.confidence.level == LEVEL_HIGH
    assert result.confidence.trusted_count == 3
    assert result.policy_fingerprint == SEALED_POLICY.fingerprint()


def test_startup_uses_conservative_min_and_shutdown_max():
    voter = IrradianceVoter(NAMES)
    voter.cycle(_readings(800, 804, 802), 30.0, MODE_NORMAL)
    startup = voter.cycle(_readings(800, 804, 802), 60.0, MODE_STARTUP)
    shutdown = voter.cycle(_readings(800, 804, 802), 90.0, MODE_SHUTDOWN)
    assert startup.value == pytest.approx(800.0)
    assert startup.fusion_rule == FUSION_CONSERVATIVE_MIN
    assert shutdown.value == pytest.approx(804.0)
    assert shutdown.fusion_rule == FUSION_CONSERVATIVE_MAX


def test_single_drifting_meter_is_excluded_not_averaged():
    """一台漂移是部分失败：可疑→故障升级，融合只用另外两台，绝不能按它写死值。"""

    voter = IrradianceVoter(NAMES)
    voter.cycle(_readings(800, 800, 800), 30.0, MODE_NORMAL)
    first = voter.cycle(_readings(864, 801, 799), 60.0, MODE_NORMAL)
    assert first.channels[0].state == "suspect"
    assert first.channels[0].mark == "peer_drift"
    # 可疑的一路不参与融合，结果仍是两台好表的均值。
    assert first.value == pytest.approx(800.0)

    second = voter.cycle(_readings(864, 800, 800), 90.0, MODE_NORMAL)
    third = voter.cycle(_readings(864, 800, 800), 120.0, MODE_NORMAL)
    assert third.channels[0].state == "fault"
    assert voter.faulty() == ["irr-a"]
    kinds = [(item.name, item.kind, item.offense) for item in voter.incidents()]
    assert ("irr-a", "fault", "peer_drift") in kinds
    assert third.value == pytest.approx(800.0)


def test_faulty_meter_recovers_after_clean_streak_and_is_pointed_out():
    """恢复需连续干净读数；恢复事件必须指出是哪台、此前是什么故障。"""

    voter = IrradianceVoter(NAMES)
    voter.cycle(_readings(800, 800, 800), 30.0, MODE_NORMAL)
    for ts in (60.0, 90.0, 120.0):
        voter.cycle(_readings(864, 800, 800), ts, MODE_NORMAL)
    assert voter.faulty() == ["irr-a"]

    for ts in (150.0, 180.0):
        recovering = voter.cycle(_readings(800, 800, 800), ts, MODE_NORMAL)
        assert recovering.channels[0].state == "fault"  # 未满干净周期，不恢复
    recovered = voter.cycle(_readings(800, 800, 800), 210.0, MODE_NORMAL)
    assert recovered.channels[0].state == "good"
    incident = recovered.incidents[-1]
    assert incident.kind == "recovered"
    assert incident.name == "irr-a"
    assert incident.offense == "peer_drift"
    assert voter.faulty() == []


def test_two_dead_channels_below_quorum_hold_without_value():
    """可信路不足多数：不提交融合值，返回挂起原因。"""

    voter = IrradianceVoter(NAMES)
    voter.cycle(_readings(800, 800, 800), 30.0, MODE_NORMAL)
    held = voter.cycle({"irr-a": None, "irr-b": None, "irr-c": 800.0}, 60.0, MODE_NORMAL)
    assert held.committed is False
    assert held.value is None
    assert held.reason == "trusted_below_quorum"
    assert held.confidence.level == LEVEL_LOW
    assert set(held.trusted()) == {"irr-c"}


def test_unknown_mode_holds_even_when_all_readings_good():
    """工况未知时规则定不了，宁可挂起也不能猜着写死值。"""

    voter = IrradianceVoter(NAMES)
    held = voter.cycle(_readings(800, 800, 800), 30.0, "manual_test")
    assert held.committed is False
    assert held.value is None
    assert held.reason == "unknown_mode_hold"


def test_out_of_range_reading_is_hard_fault():
    voter = IrradianceVoter(NAMES)
    result = voter.cycle(_readings(5000, 800, 800), 30.0, MODE_NORMAL)
    assert result.channels[0].state == "fault"
    assert result.channels[0].mark == "out_of_range"
    assert result.committed is True
    assert result.value == pytest.approx(800.0)


def test_cloud_transient_moving_together_is_not_a_fault():
    """云团是真实辐照变化：三路一起掉，谁也不离群，均值照常用。"""

    voter = IrradianceVoter(NAMES)
    voter.cycle(_readings(800, 801, 799), 30.0, MODE_CLOUD)
    dip = voter.cycle(_readings(500, 502, 498), 60.0, MODE_CLOUD)
    assert all(channel.state == "good" for channel in dip.channels)
    assert dip.committed is True
    assert dip.value == pytest.approx(500.0)


def test_rate_spike_flags_only_the_jumping_meter():
    """单台一秒内突跳 60 W/m² 而同伴平稳，应判尖峰而不是把云团当故障。"""

    voter = IrradianceVoter(NAMES)
    voter.cycle(_readings(800, 800, 800), 0.0, MODE_NORMAL)
    spiked = voter.cycle(_readings(860, 801, 800), 1.0, MODE_NORMAL)
    assert spiked.channels[0].mark == "rate_spike"
    assert spiked.channels[1].mark == "ok"


def test_stuck_meter_faults_when_peers_move():
    """读数长时间不动而同伴在缓变：连续卡死后升级故障，恢复后清掉。"""

    voter = IrradianceVoter(NAMES)
    ts = 0.0
    for cycle in range(1, 24):
        ts += 30.0
        truth = 820.0 + 3.0 * cycle
        voter.cycle(_readings(truth, truth - 1.0, truth + 1.0), ts, MODE_NORMAL)
    stuck_value = truth - 1.0
    for cycle in range(24, 36):
        ts += 30.0
        truth = 820.0 + 3.0 * cycle
        result = voter.cycle(_readings(truth, stuck_value, truth + 1.0), ts, MODE_NORMAL)
    assert result.channels[1].state == "fault"
    assert any(item.name == "irr-b" and item.offense == "stuck" for item in voter.incidents())
