"""Policy validation and description."""

from __future__ import annotations

import pytest
from argus.payroll import PairingPolicy, PolicyError
from builders import policy


class TestValidation:
    def test_rejects_ceiling_below_floor(self) -> None:
        with pytest.raises(PolicyError, match="must exceed min_dwell_s"):
            policy(min_dwell_s=600, max_dwell_s=60)

    def test_rejects_a_ceiling_of_a_day_or_more(self) -> None:
        """Day attribution assumes an interval crosses at most one local
        midnight. A ceiling of 24h or more quietly breaks that, so it is
        refused rather than left to produce wrong days."""
        with pytest.raises(PolicyError, match="crosses at most one local midnight"):
            policy(max_dwell_s=86400)

    def test_rejects_negative_allowance(self) -> None:
        with pytest.raises(PolicyError, match="allowance_s"):
            policy(allowance_s=-1)

    def test_rejects_unknown_timezone(self) -> None:
        with pytest.raises(PolicyError, match="unknown policy timezone"):
            policy(timezone="Mars/Olympus")

    def test_rejects_nonpositive_duplicate_window(self) -> None:
        with pytest.raises(PolicyError, match="duplicate_window_s"):
            policy(duplicate_window_s=0)

    def test_zero_allowance_is_allowed(self) -> None:
        """Not a mistake to guard against: a space with no free time is a
        legitimate policy, and refusing it would be us inventing rules."""
        assert policy(allowance_s=0).allowance_s == 0


class TestDescribe:
    def test_says_the_allowance_in_words(self) -> None:
        """This string goes at the top of every report page, which is how
        'the demo runs at two minutes' becomes a property of the artefact
        rather than of whoever is presenting it."""
        assert "2 minutes" in policy(allowance_s=120).describe()
        assert "1 hour" in policy(allowance_s=3600).describe()
        assert "Asia/Dhaka" in policy().describe()

    def test_defaults_are_the_production_values(self) -> None:
        default = PairingPolicy()
        assert default.allowance_s == 3600, "the demo shortens this in config, not in code"
        assert default.timezone == "Asia/Dhaka"
