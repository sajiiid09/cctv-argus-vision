"""One test per row of the ARCHITECTURE.md §7.3 case table.

The file is named for the table so the two can be diffed by eye. Adding a row
there means adding a test here, in the same commit (AGENTS.md §6).

Each test asserts four things -- state, flags, overage and the attributed local
day -- because a test that checks only the overage passes just as happily on a
right number reached for the wrong reason.
"""

from __future__ import annotations

from argus.payroll import Flag, PairingState, run_pairing
from builders import DAY, NEXT_DAY, at, clock_after, ev, gap, policy


def _run(events, gaps=(), pol=None, clock=None):
    return run_pairing(list(events), list(gaps), pol or policy(), clock or clock_after())


class TestCaseTable:
    def test_clean_enter_exit_resolved(self) -> None:
        result = _run([ev("a", "13:00", "enter"), ev("b", "13:30", "exit")])
        (interval,) = result.intervals
        assert interval.state is PairingState.RESOLVED
        assert interval.flags == ()
        assert interval.duration_s == 1800
        assert interval.local_day == DAY
        (day,) = result.days
        assert day.day_state == "clean"
        assert day.total_dwell_s == 1800
        assert day.overage_s == 1800 - 600

    def test_enter_then_enter_is_ambiguous_double_enter(self) -> None:
        result = _run([ev("a", "13:00", "enter"), ev("b", "13:10", "enter")])
        first = result.intervals[0]
        assert first.state is PairingState.AMBIGUOUS
        assert Flag.DOUBLE_ENTER in first.flags
        # the FIRST enter stays the start candidate; no exit is invented
        assert first.enter_event_id == ev("a", "13:00", "enter").event_id
        assert first.exit_event_id is None
        assert all(d.overage_s == 0 for d in result.days)
        assert all(d.day_state == "flagged" for d in result.days)

    def test_exit_then_exit_is_ambiguous_double_exit(self) -> None:
        result = _run([ev("a", "13:00", "exit"), ev("b", "13:10", "exit")])
        first, second = result.intervals
        assert first.state is PairingState.UNPAIRED_EXIT
        assert Flag.MISSING_ENTER in first.flags
        assert second.state is PairingState.AMBIGUOUS
        assert Flag.DOUBLE_EXIT in second.flags
        (day,) = result.days
        assert day.overage_s == 0
        assert day.local_day == DAY

    def test_unpaired_enter_missing_exit(self) -> None:
        result = _run([ev("a", "13:00", "enter")])
        (interval,) = result.intervals
        assert interval.state is PairingState.UNPAIRED_ENTER
        assert Flag.MISSING_EXIT in interval.flags
        assert interval.exit_event_id is None
        assert interval.duration_s is None
        (day,) = result.days
        assert day.overage_s == 0 and day.day_state == "flagged"
        assert day.local_day == DAY

    def test_unpaired_exit_missing_enter(self) -> None:
        result = _run([ev("a", "13:30", "exit")])
        (interval,) = result.intervals
        assert interval.state is PairingState.UNPAIRED_EXIT
        assert Flag.MISSING_ENTER in interval.flags
        assert interval.start_utc is None
        assert result.days[0].overage_s == 0

    def test_implausible_short_below_floor(self) -> None:
        result = _run([ev("a", "13:00:00", "enter"), ev("b", "13:00:30", "exit")])
        (interval,) = result.intervals
        assert interval.state is PairingState.IMPLAUSIBLE
        assert Flag.TOO_SHORT in interval.flags
        assert interval.duration_s == 30
        assert result.days[0].overage_s == 0

    def test_implausible_long_above_ceiling(self) -> None:
        result = _run(
            [ev("a", "09:00", "enter"), ev("b", "13:00", "exit")],
            pol=policy(max_dwell_s=10800),
        )
        (interval,) = result.intervals
        assert interval.state is PairingState.IMPLAUSIBLE
        assert Flag.TOO_LONG in interval.flags
        assert result.days[0].overage_s == 0

    def test_stream_gap_overlap_is_gap_affected(self) -> None:
        result = _run(
            [ev("a", "13:00", "enter"), ev("b", "13:30", "exit")],
            gaps=[gap("13:10", "13:12")],
        )
        (interval,) = result.intervals
        assert interval.state is PairingState.GAP_AFFECTED
        assert Flag.STREAM_GAP in interval.flags
        assert result.days[0].overage_s == 0

    def test_gap_in_another_space_does_not_flag(self) -> None:
        """Pairing is per space; a gap elsewhere is not evidence about this one."""
        result = _run(
            [ev("a", "13:00", "enter"), ev("b", "13:30", "exit")],
            gaps=[gap("13:10", "13:12", space="mess_hall", camera="floor_view")],
        )
        assert result.intervals[0].state is PairingState.RESOLVED

    def test_clock_anomaly(self) -> None:
        """Camera-reported time disagreeing beyond tolerance is an anomaly.
        The reported time is never used in arithmetic -- only to notice this."""
        result = _run(
            [
                ev("a", "13:00", "enter", reported="12:00"),
                ev("b", "13:30", "exit"),
            ]
        )
        (interval,) = result.intervals
        assert interval.state is PairingState.AMBIGUOUS
        assert Flag.CLOCK_ANOMALY in interval.flags
        assert interval.duration_s == 1800  # still measured from the server clock
        assert result.days[0].overage_s == 0

    def test_unknown_face_produces_no_interval(self) -> None:
        result = _run([ev("a", "13:00", "enter", person=None)])
        assert result.intervals == ()
        assert result.days == ()
        assert [u.flag for u in result.unattributable] == [Flag.UNKNOWN_PERSON]

    def test_low_confidence_treated_as_unknown_never_a_best_guess(self) -> None:
        result = _run(
            [ev("a", "13:00", "enter", confidence=0.20)],
            pol=policy(identity_threshold=0.38),
        )
        assert result.intervals == ()
        assert [u.flag for u in result.unattributable] == [Flag.LOW_CONFIDENCE]

    def test_multiple_doors_one_space_resolves(self) -> None:
        """Enter by one door, leave by another. Normal behaviour, not a fault."""
        result = _run(
            [
                ev("a", "13:00", "enter", door="c1", camera="canteen_door_01"),
                ev("b", "13:30", "exit", door="c2", camera="canteen_door_02"),
            ]
        )
        (interval,) = result.intervals
        assert interval.state is PairingState.RESOLVED
        assert interval.flags == ()
        assert result.days[0].overage_s == 1800 - 600

    def test_crossing_midnight_attributed_to_enter_day(self) -> None:
        result = _run(
            [
                ev("a", "23:30", "enter"),
                ev("b", "00:30", "exit", day=NEXT_DAY),
            ]
        )
        (interval,) = result.intervals
        assert interval.state is PairingState.RESOLVED
        assert Flag.SPANS_BOUNDARY in interval.flags
        assert interval.local_day == DAY, "attributed to the day of the ENTER"
        assert interval.duration_s == 3600
        (day,) = result.days
        assert day.local_day == DAY
        # spans_boundary is a flag for review, and the interval still resolved,
        # so the day is clean and the overage is real
        assert day.day_state == "clean"
        assert day.overage_s == 3600 - 600

    def test_duplicate_event_deduplicated_within_window(self) -> None:
        result = _run(
            [
                ev("a", "13:00:00", "enter"),
                ev("a_dup", "13:00:02", "enter"),
                ev("b", "13:30", "exit"),
            ]
        )
        (interval,) = result.intervals
        assert interval.state is PairingState.RESOLVED
        assert interval.enter_event_id == ev("a", "13:00:00", "enter").event_id
        assert [u.flag for u in result.unattributable] == [Flag.DEDUPLICATED]

    def test_duplicate_marked_by_ingest_is_ignored(self) -> None:
        """A row already carrying duplicate_of never enters the machine, however
        far apart the two timestamps are."""
        original = ev("a", "13:00:00", "enter")
        result = _run(
            [
                original,
                ev("a_dup", "13:00:20", "enter", duplicate_of=original.event_id),
                ev("b", "13:30", "exit"),
            ]
        )
        (interval,) = result.intervals
        assert interval.state is PairingState.RESOLVED
        assert interval.enter_event_id == original.event_id

    def test_missing_clip_flags_the_day(self) -> None:
        """ADR-0008: no clip, no deduction. The interval still resolved -- the
        measurement happened -- but an unauditable day contributes zero."""
        result = _run([ev("a", "13:00", "enter", clip=None), ev("b", "13:30", "exit")])
        (interval,) = result.intervals
        assert interval.state is PairingState.RESOLVED
        assert Flag.MISSING_CLIP in interval.flags
        (day,) = result.days
        assert day.day_state == "flagged"
        assert day.overage_s == 0
        assert day.total_dwell_s == 1800, "the dwell is still reported, just not charged"


class TestDayRollup:
    def test_allowance_is_per_day_not_per_visit(self) -> None:
        result = _run(
            [
                ev("a", "12:00", "enter"),
                ev("b", "12:08", "exit"),
                ev("c", "13:00", "enter"),
                ev("d", "13:08", "exit"),
            ]
        )
        (day,) = result.days
        assert day.total_dwell_s == 960
        assert day.overage_s == 960 - 600, "one allowance for the day, not one per visit"

    def test_one_bad_interval_forgives_the_whole_day(self) -> None:
        """ADR-0023 (a), the strictest reading of fail-open."""
        result = _run(
            [
                ev("a", "12:00", "enter"),
                ev("b", "12:40", "exit"),  # 40 min, well over the allowance
                ev("c", "13:00", "exit"),  # unpaired: flags the day
            ]
        )
        (day,) = result.days
        assert day.day_state == "flagged"
        assert day.overage_s == 0
        assert day.total_dwell_s == 2400

    def test_person_still_inside_yields_no_interval_yet(self) -> None:
        """Before the plausibility ceiling passes, they are simply in the
        canteen. Inventing an exit is the punitive default SOUL.md forbids."""
        from argus.common.clock import FixedClock

        result = _run([ev("a", "13:00", "enter")], clock=FixedClock(at("13:20")))
        assert result.intervals == ()
        assert result.days == ()
        assert len(result.open_enters) == 1
