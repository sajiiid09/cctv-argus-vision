"""The properties from AGENTS.md §7, plus two.

Examples prove the cases someone thought of. The fail-open guarantee is a claim
about the cases nobody thought of, which is what these are for.

Property 1 is that guarantee in executable form. If it ever fails, stop and fix
it before anything else -- it is the one promise SOUL.md makes.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, uuid5

from argus.common.clock import FixedClock, local_day
from argus.payroll import Flag, Gap, PairingState, run_pairing
from argus.payroll.types import DoorEvent
from builders import DHAKA, policy
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

EPOCH = datetime(2026, 9, 17, 0, 0, tzinfo=UTC)
POLICY = policy()
# Late enough that every generated day has certainly closed, so the walk never
# leaves an interval pending and the properties talk about final answers.
CLOCK = FixedClock(EPOCH + timedelta(days=6))

SETTINGS = settings(
    max_examples=200,
    deadline=None,
    derandomize=True,
    database=None,
    suppress_health_check=[HealthCheck.too_slow],
)


@st.composite
def door_events(draw: st.DrawFn, *, min_size: int = 0, max_size: int = 14) -> list[DoorEvent]:
    """Event sets spanning a few days, with the awkward values included:
    unknown people, confidences either side of the threshold, missing clips,
    both doors of one space, and ambiguous directions."""
    n = draw(st.integers(min_value=min_size, max_value=max_size))
    events = []
    for i in range(n):
        offset = draw(st.integers(min_value=0, max_value=3 * 24 * 3600))
        events.append(
            DoorEvent(
                event_id=uuid5(NAMESPACE_URL, f"prop-event-{i}"),
                ts_utc=EPOCH + timedelta(seconds=offset),
                space_id="canteen",
                camera_id=draw(st.sampled_from(["canteen_door_01", "canteen_door_02"])),
                direction=draw(st.sampled_from(["enter", "exit", "ambiguous"])),
                door_id=draw(st.sampled_from(["c1", "c2"])),
                person_id=draw(st.sampled_from(["p_alice", "p_bob", None])),
                match_confidence=draw(st.sampled_from([None, 0.1, 0.5, 0.9])),
                clip_ref=draw(st.sampled_from([None, "clips/x.mp4"])),
            )
        )
    return events


@st.composite
def gaps(draw: st.DrawFn, *, max_size: int = 3) -> list[Gap]:
    n = draw(st.integers(min_value=0, max_value=max_size))
    out = []
    for _ in range(n):
        start = draw(st.integers(min_value=0, max_value=3 * 24 * 3600))
        length = draw(st.integers(min_value=1, max_value=7200))
        out.append(
            Gap(
                camera_id="canteen_door_01",
                from_utc=EPOCH + timedelta(seconds=start),
                to_utc=EPOCH + timedelta(seconds=start + length),
                cause="offline",
                space_id="canteen",
            )
        )
    return out


@SETTINGS
@given(events=door_events(), gs=gaps())
def test_property_1_non_resolved_contributes_exactly_zero_overage(events, gs) -> None:
    """The fail-open guarantee. Any day holding a non-RESOLVED interval, or a
    resolved one without clips, contributes exactly zero -- not a reduced
    amount, not a confidence-weighted fraction. Zero."""
    result = run_pairing(events, gs, POLICY, CLOCK)
    by_day = {}
    for interval in result.intervals:
        by_day.setdefault((interval.person_id, interval.space_id, interval.local_day), []).append(
            interval
        )
    for day in result.days:
        group = by_day[(day.person_id, day.space_id, day.local_day)]
        impure = any(
            i.state is not PairingState.RESOLVED or Flag.MISSING_CLIP in i.flags for i in group
        )
        if impure:
            assert day.overage_s == 0
            assert day.day_state == "flagged"


@SETTINGS
@given(events=door_events(), gs=gaps())
def test_property_2_overage_never_negative_never_exceeds_measured_dwell(events, gs) -> None:
    result = run_pairing(events, gs, POLICY, CLOCK)
    for day in result.days:
        assert day.overage_s >= 0
        assert day.overage_s <= day.total_dwell_s
        assert day.total_dwell_s >= 0


@SETTINGS
@given(events=door_events())
def test_property_3_overlapping_gap_forces_zero_for_the_day(events) -> None:
    """A gap covering the whole window means we were not watching, so nothing
    in it can be charged."""
    covering = [
        Gap(
            camera_id="canteen_door_01",
            from_utc=EPOCH - timedelta(days=1),
            to_utc=EPOCH + timedelta(days=5),
            cause="offline",
            space_id="canteen",
        )
    ]
    result = run_pairing(events, covering, POLICY, CLOCK)
    assert all(day.overage_s == 0 for day in result.days)


@SETTINGS
@given(events=door_events(), gs=gaps())
def test_property_4_recompute_over_same_inputs_is_identical(events, gs) -> None:
    first = run_pairing(events, gs, POLICY, CLOCK)
    second = run_pairing(events, gs, POLICY, CLOCK)
    assert first == second


@SETTINGS
@given(events=door_events(min_size=1), gs=gaps())
def test_property_5_a_late_event_cannot_create_a_charge_on_an_old_day(events, gs) -> None:
    """A late arrival never increases an already-computed day.

    It holds because of the plausibility ceiling, not by accident: an event
    arriving days later can only pair with an old enter by producing an interval
    longer than max_dwell_s, which is IMPLAUSIBLE, which is zero.

    Note what this does NOT claim. A *normal* late event -- an exit minutes after
    its enter -- legitimately completes a day and changes its number. That is why
    every stored result carries its own run id: the number is allowed to change,
    but only through a recorded recomputation, never by editing a row.
    """
    before = run_pairing(events, gs, POLICY, CLOCK)
    latest = max(e.ts_utc for e in events)
    late = DoorEvent(
        event_id=uuid5(NAMESPACE_URL, "prop-event-late"),
        ts_utc=latest + timedelta(days=2),
        space_id="canteen",
        camera_id="canteen_door_01",
        direction="exit",
        door_id="c1",
        person_id="p_alice",
        clip_ref="clips/x.mp4",
    )
    after = run_pairing([*events, late], gs, POLICY, CLOCK)
    after_by_key = {(d.person_id, d.space_id, d.local_day): d for d in after.days}
    late_day = local_day(late.ts_utc, DHAKA)
    for day in before.days:
        if day.local_day >= late_day:
            continue
        updated = after_by_key.get((day.person_id, day.space_id, day.local_day))
        assert updated is not None, "a day cannot vanish when evidence is added"
        assert updated.overage_s <= day.overage_s


@SETTINGS
@given(events=door_events(), gs=gaps())
def test_property_6_no_ordering_produces_interval_with_end_before_start(events, gs) -> None:
    result = run_pairing(events, gs, POLICY, CLOCK)
    for interval in result.intervals:
        if interval.start_utc is not None and interval.end_utc is not None:
            assert interval.end_utc >= interval.start_utc
        if interval.duration_s is not None:
            assert interval.duration_s >= 0


@SETTINGS
@given(events=door_events(), gs=gaps(), seed=st.integers(min_value=0, max_value=10_000))
def test_property_7_output_is_invariant_under_input_permutation(events, gs, seed) -> None:
    """Out-of-order arrival is the real-world case: a clip finishes encoding
    late, a reconnect flushes a burst. Stronger than property 6 -- ordering must
    not matter at all, not merely avoid producing nonsense."""
    import random

    shuffled = list(events)
    random.Random(seed).shuffle(shuffled)
    assert run_pairing(events, gs, POLICY, CLOCK) == run_pairing(shuffled, gs, POLICY, CLOCK)


@SETTINGS
@given(events=door_events(), gs=gaps())
def test_property_8_every_charged_day_is_fully_auditable(events, gs) -> None:
    """ADR-0008 inside pairing: a day that contributes a charge has a clip on
    both ends of every interval that built it."""
    result = run_pairing(events, gs, POLICY, CLOCK)
    by_id = {i.interval_id: i for i in result.intervals}
    for day in result.days:
        if day.overage_s > 0:
            for interval_id in day.interval_ids:
                interval = by_id[interval_id]
                assert Flag.MISSING_CLIP not in interval.flags
                assert interval.state is PairingState.RESOLVED
