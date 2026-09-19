"""The config sections added with the services, and the values they refuse.

Configuration is where a decision quietly becomes a knob. Each refusal here
corresponds to a decision that is not supposed to be one.
"""

from __future__ import annotations

import pytest
from argus.common.config import (
    CanteenConfig,
    ConfigError,
    FaceConfig,
    OccupancyConfig,
    PayrollConfig,
    PipelinesConfig,
    load_config,
)


def test_shadow_mode_is_not_a_yaml_key(tmp_path) -> None:
    """AGENTS.md §2.1: leaving shadow mode is a human sign-off, an ADR and a
    code change -- and there is no export to enable, so `false` would claim
    something untrue."""
    path = tmp_path / "bad.yaml"
    path.write_text("payroll:\n  shadow_mode: false\n")
    with pytest.raises(ConfigError, match="human sign-off"):
        load_config(path, secrets_path=tmp_path / "none.env", env={})


def test_face_thresholds_have_no_defaults() -> None:
    """ADR-0010: a threshold comes from measured data. With none, identity is
    off and every crossing is unknown, which is the fail-open outcome."""
    assert FaceConfig().canteen_match_threshold is None
    assert FaceConfig().gate_verify_threshold is None
    with pytest.raises(ConfigError, match="unset"):
        FaceConfig(enabled=True)
    with pytest.raises(ConfigError, match="unset"):
        FaceConfig(enabled=True, canteen_match_threshold=0.4)
    assert FaceConfig(enabled=True, canteen_match_threshold=0.38, gate_verify_threshold=0.28)


def test_the_two_face_thresholds_may_not_be_equal() -> None:
    """1:N identification and 1:1 verification are different problems. Equal
    values are almost always a copy-paste, and the mistake is invisible in
    review."""
    with pytest.raises(ConfigError, match="different problems"):
        FaceConfig(enabled=True, canteen_match_threshold=0.3, gate_verify_threshold=0.3)


def test_analysis_is_off_unless_asked_for() -> None:
    """A box with no model artefacts must run ingest exactly as before."""
    assert PipelinesConfig().enabled is False
    assert OccupancyConfig().enabled is False
    assert PipelinesConfig().violence.enabled is False


def test_a_zero_width_hysteresis_band_is_refused() -> None:
    """One jittery detection either side of a line would be a crossing, and a
    crossing is a deduction."""
    with pytest.raises(ConfigError, match=r"band must be in"):
        CanteenConfig(band=0.0)


def test_the_dedup_window_is_one_number_shared_with_payroll() -> None:
    """The pipeline dedupes in process and payroll dedupes again on read; two
    different windows is a divergence nobody would notice."""
    assert CanteenConfig().duplicate_window_s == PayrollConfig().duplicate_window_s


def test_the_lead_in_defaults_to_the_plausibility_ceiling() -> None:
    assert PayrollConfig().lead_in_s is None  # meaning max_dwell_s
    assert PayrollConfig().max_dwell_s == 10800


def test_env_overrides_still_reach_the_new_sections(tmp_path) -> None:
    path = tmp_path / "dev.yaml"
    path.write_text("pipelines:\n  enabled: false\n")
    import os

    # apply_env_overrides reads os.environ, so the override path is exercised
    # through the real thing rather than an injected dict.
    os.environ["ARGUS_PIPELINES__ENABLED"] = "true"
    os.environ["ARGUS_PAYROLL__ALLOWANCE_S"] = "120"
    try:
        config = load_config(path, secrets_path=tmp_path / "none.env", env=None)
        assert config.pipelines.enabled is True
        assert config.payroll.allowance_s == 120
    finally:
        del os.environ["ARGUS_PIPELINES__ENABLED"]
        del os.environ["ARGUS_PAYROLL__ALLOWANCE_S"]


def test_the_shipped_configs_all_load() -> None:
    """A config file nobody loads is a config file that has drifted."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    for name in ("dev.yaml", "staging.yaml", "rig_canteen.yaml", "rig_floor.yaml"):
        config = load_config(root / "config" / name, secrets_path=root / "nothing.env", env={})
        assert config.timezone == "Asia/Dhaka"
        assert config.payroll.shadow_mode is True
        assert config.face.enabled is False, f"{name} must not claim identity works yet"
