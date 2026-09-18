"""Config to policy: one mapping, one timezone, and a flag that cannot be set."""

from __future__ import annotations

import pytest
from argus.common.config import AppConfig, ConfigError, PayrollConfig
from argus.pairing.policy import lead_in_seconds, policy_from_config


def test_the_policy_timezone_has_exactly_one_source() -> None:
    """Two places to set it is two places to disagree, and the disagreement
    shows up as every pay-period boundary moving by six hours in one of them."""
    config = AppConfig(timezone="Asia/Dhaka")
    assert policy_from_config(config).timezone == "Asia/Dhaka"
    assert not hasattr(PayrollConfig(), "timezone")


def test_shadow_mode_cannot_be_turned_off_in_config() -> None:
    with pytest.raises(ConfigError, match="human sign-off"):
        PayrollConfig(shadow_mode=False)


def test_thresholds_carry_through() -> None:
    config = AppConfig(
        payroll=PayrollConfig(
            allowance_s=120, min_dwell_s=30, max_dwell_s=7200, duplicate_window_s=2.5
        )
    )
    policy = policy_from_config(config)
    assert (policy.allowance_s, policy.min_dwell_s, policy.max_dwell_s) == (120, 30, 7200)
    assert policy.duplicate_window_s == 2.5


def test_an_impossible_policy_is_refused_before_any_run() -> None:
    with pytest.raises(ConfigError, match="must exceed min_dwell_s"):
        PayrollConfig(min_dwell_s=600, max_dwell_s=600)


def test_the_lead_in_defaults_to_the_longest_legitimate_interval() -> None:
    """Truncating a window would turn a resolved pair into an UNPAIRED_ENTER,
    which is a window boundary deciding whether somebody is charged."""
    config = AppConfig(payroll=PayrollConfig(max_dwell_s=7200))
    policy = policy_from_config(config)
    assert lead_in_seconds(config, policy) == 7200
    explicit = AppConfig(payroll=PayrollConfig(max_dwell_s=7200, lead_in_s=60))
    assert lead_in_seconds(explicit, policy_from_config(explicit)) == 60


def test_the_policy_fingerprint_changes_when_the_policy_does() -> None:
    first = policy_from_config(AppConfig(payroll=PayrollConfig(allowance_s=3600)))
    second = policy_from_config(AppConfig(payroll=PayrollConfig(allowance_s=120)))
    assert first.fingerprint() != second.fingerprint()
    assert "2 minutes" in second.describe()
