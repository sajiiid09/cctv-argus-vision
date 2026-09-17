"""Stable identifiers and fingerprints.

A disputed line from months ago has to be reproducible exactly. That needs two
things to be stable across processes and releases: the id of an interval, and
the fingerprint of the policy that produced it.
"""

from __future__ import annotations

from datetime import date

from argus.payroll import LOGIC_VERSION, interval_id, run_pairing
from builders import clock_after, ev, policy


class TestIntervalId:
    def test_is_stable_across_processes(self) -> None:
        """A literal, not a recomputation: if this value ever changes, stored
        ids stop matching recomputed ones, and that must be a visible decision
        rather than a surprise."""
        got = interval_id(
            policy_fingerprint="0123456789abcdef",
            person_id="p_alice",
            space_id="canteen",
            day=date(2026, 9, 17),
            enter_event_id=None,
            exit_event_id=None,
        )
        assert str(got) == "c4358cce-5e18-5e33-a262-a4887a8c29e5"

    def test_differs_when_any_component_differs(self) -> None:
        base = dict(
            policy_fingerprint="0123456789abcdef",
            person_id="p_alice",
            space_id="canteen",
            day=date(2026, 9, 17),
            enter_event_id=None,
            exit_event_id=None,
        )
        ids = {interval_id(**base)}
        ids.add(interval_id(**{**base, "person_id": "p_bob"}))
        ids.add(interval_id(**{**base, "space_id": "mess_hall"}))
        ids.add(interval_id(**{**base, "day": date(2026, 9, 18)}))
        ids.add(interval_id(**{**base, "policy_fingerprint": "ffffffffffffffff"}))
        assert len(ids) == 5

    def test_changes_with_the_logic_version(self) -> None:
        """Ids are namespaced by LOGIC_VERSION, so a change to the arithmetic
        cannot silently reuse an old id for a differently-computed interval."""
        assert LOGIC_VERSION in ("pairing-1.0.0",), (
            "bumping LOGIC_VERSION is intended; update this test in the same "
            "commit so the change is deliberate"
        )


class TestPolicyFingerprint:
    def test_is_stable_for_the_same_policy(self) -> None:
        assert policy().fingerprint() == policy().fingerprint()

    def test_changes_when_any_field_changes(self) -> None:
        assert policy().fingerprint() != policy(allowance_s=120).fingerprint()
        assert policy().fingerprint() != policy(min_dwell_s=30).fingerprint()

    def test_is_recorded_on_the_result(self) -> None:
        pol = policy(allowance_s=120)
        result = run_pairing(
            [ev("a", "13:00", "enter"), ev("b", "13:30", "exit")], [], pol, clock_after()
        )
        assert result.policy_fingerprint == pol.fingerprint()
        assert result.logic_version == LOGIC_VERSION
