"""多路辐照计的控制台接口与配置装配。"""

from __future__ import annotations

import pytest

from cspplant.config import ConfigError, default_config, load_config
from cspplant.console.api import Api
from cspplant.console.pages import render_page


def test_api_fuse_and_status(platform):
    api = Api(platform)
    status, payload = api.handle(
        "POST",
        "/api/irradiance/fuse",
        {},
        {"readings": {"irr-a": 820, "irr-b": 821, "irr-c": 819}, "mode": "normal_sun"},
    )
    assert status == 200
    assert payload["committed"] is True
    assert payload["confidence"]["level"] == "HIGH"

    status, info = api.handle("GET", "/api/irradiance", {}, {})
    assert status == 200
    assert info["fusion"]["active"] is True
    assert info["fusion"]["channel_states"] == {"irr-a": "good", "irr-b": "good", "irr-c": "good"}


def test_api_fuse_holds_on_two_dead(platform):
    api = Api(platform)
    status, payload = api.handle(
        "POST",
        "/api/irradiance/fuse",
        {},
        {"readings": {"irr-a": None, "irr-b": None, "irr-c": 820}},
    )
    assert status == 200
    assert payload["committed"] is False
    assert payload["dni_w_m2"] is None


def test_api_fuse_rejects_non_mapping_readings(platform):
    api = Api(platform)
    status, payload = api.handle("POST", "/api/irradiance/fuse", {}, {"readings": [820, 821]})
    assert status == 409
    assert payload["component"] == "console"


def test_api_verify_endpoint(platform):
    api = Api(platform)
    api.handle("POST", "/api/irradiance/fuse", {}, {"readings": {"irr-a": 820, "irr-b": 821, "irr-c": 819}})
    status, payload = api.handle("GET", "/api/irradiance/verify", {}, {})
    assert status == 200
    assert payload["ok"] is True


def test_mirror_page_shows_fusion_panel(platform):
    api = Api(platform)
    api.handle("POST", "/api/irradiance/fuse", {}, {"readings": {"irr-a": 820, "irr-b": 821, "irr-c": 819}})
    html = render_page("/", platform)
    assert "多路辐照表决" in html
    assert "irr-a" in html
    assert "可信度" in html


def test_default_config_has_three_channels():
    channels = default_config().irradiance.meter_channels
    assert channels == ("irr-a", "irr-b", "irr-c")


def test_config_rejects_single_channel(tmp_path):
    import json

    path = tmp_path / "csp.json"
    mapping = {
        "platform": {"name": "x", "storage_root": str(tmp_path / "state")},
        "limits": {
            "heliostat": {
                "aim_tolerance_deg": 0.6, "azimuth_rate_deg_s": 1.2,
                "elevation_rate_deg_s": 0.9, "stow_elevation_deg": 12.0,
                "mirror_area_m2": 450.0, "reflectivity": 0.94,
            },
            "irradiance": {
                "dni_min_w_m2": 250.0, "dni_max_w_m2": 1100.0,
                "sample_period_s": 30.0, "stale_after_s": 180.0,
                "meter_channels": ["only-one"],
            },
            "field": {
                "focus_threshold_w_m2": 320.0, "dispatch_rate_pct_s": 6.0,
                "sweep_batch": 12, "safe_zone_margin_m": 6.0,
            },
            "receiver": {
                "temp_target_c": 600.0, "temp_derate_c": 620.0, "temp_max_c": 720.0,
                "flow_min_kg_s": 42.0, "flow_confirm_s": 20.0,
                "valve_travel_pct_s": 8.0, "ambient_c": 22.0,
            },
            "salt": {
                "cold_level_min_pct": 18.0, "hot_level_max_pct": 96.0,
                "pump_ramp_kg_s2": 1.4, "handoff_confirm_s": 15.0,
                "flow_baseline_kg_s": 58.0, "flow_tolerance_kg_s": 9.0,
                "header_pressure_target_mpa": 2.5, "header_pressure_max_mpa": 3.4,
            },
            "steam": {
                "pressure_target_mpa": 12.0, "pressure_band_mpa": 0.8,
                "bypass_lead_s": 6.0, "load_max_mw": 30.0, "load_rate_mw_s": 1.0,
            },
            "wash": {
                "wash_duration_s": 90.0, "return_flow_m3_h": 26.0,
                "drain_lead_s": 4.0, "truck_capacity_m3": 9.0,
            },
            "alarm": {"latch_reset_delay_s": 5.0, "escalation_window_s": 120.0},
        },
    }
    path.write_text(json.dumps(mapping), encoding="utf-8")
    with pytest.raises(ConfigError, match="至少需要两台"):
        load_config(str(path))
