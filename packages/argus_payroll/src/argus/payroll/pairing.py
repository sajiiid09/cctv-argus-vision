"""The door-event pairing state machine (DATA_MODEL.md §4).

This is the only file in the repository that can cost someone money. Every case
in the §4 table has a branch here and a test named after it in
``tests/payroll/test_case_table.py``.

    IDLE --enter--> INSIDE --exit--> RESOLVED (dwell computed)
      |                |
      |                +--enter again--> AMBIGUOUS
      +--exit, no prior enter--> UNPAIRED_EXIT

The walk is per (person, canteen space) and **not** per day, which looks like a
contradiction of §4's "one local day" scoping and is not. §4 also requires a
midnight-crossing interval to be RESOLVED and attributed to the day of its
enter; that is only possible if INSIDE survives the boundary. It is well defined
because ``max_dwell_s`` is under 24 hours, so an interval crosses at most one
local midnight -- the plausibility ceiling is what makes day attribution total,
which is why the policy refuses a ceiling of a day or more.

Precedence, when several conditions apply at once:

    clock anomaly -> stream gap -> structural failure -> plausibility -> clip

The property that makes this cheap to review: every one of those branches yields
a non-RESOLVED state, and a non-RESOLVED state yields zero overage. Precedence
decides the label a reviewer sees. It cannot change the money.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from uuid import NAMESPACE_URL, UUID, uuid5

from argus.common.clock import local_day
from argus.payroll.dwell import duration_s
from argus.payroll.policy import PairingPolicy
from argus.payroll.types import (
    DoorEvent,
    Flag,
    Interval,
    MachineState,
    PairingState,
)
from argus.payroll.types import Gap as StreamGap
from argus.payroll.version import LOGIC_VERSION

# A fixed namespace so interval ids are reproducible across processes and runs.
INTERVAL_NAMESPACE = uuid5(NAMESPACE_URL, "sparrow-vision/payroll/interval")


def interval_id(
    *,
    policy_fingerprint: str,
    person_id: str,
    space_id: str,
    day: date,
    enter_event_id: UUID | None,
    exit_event_id: UUID | None,
) -> UUID:
    """Deterministic id for one interval.

    uuid5, not uuid4: TESTING.md §2 property 4 requires recomputation over the
    same inputs to be identical, and a random id would force the comparison to
    exclude the id -- an exclusion that would then hide real nondeterminism.
    """
    key = "|".join(
        [
            LOGIC_VERSION,
            policy_fingerprint,
            person_id,
            space_id,
            day.isoformat(),
            str(enter_event_id),
            str(exit_event_id),
        ]
    )
    return uuid5(INTERVAL_NAMESPACE, key)


def _skewed(event: DoorEvent, policy: PairingPolicy) -> bool:
    if event.ts_camera_reported is None:
        return False
    drift = abs((event.ts_camera_reported - event.ts_utc).total_seconds())
    return drift > policy.clock_skew_tolerance_s


def overlaps_gap(
    start_utc: datetime,
    end_utc: datetime,
    gaps: Sequence[StreamGap],
    space_id: str,
    now_utc: datetime,
) -> bool:
    """Does any measurement gap in this space touch [start, end]?

    An open gap (``to_utc is None``) extends to the evaluation instant: we have
    not seen it close, so we cannot claim we were watching.
    """
    for gap in gaps:
        if gap.space_id != space_id:
            continue
        gap_end = gap.to_utc if gap.to_utc is not None else now_utc
        if gap.from_utc <= end_utc and gap_end >= start_utc:
            return True
    return False


def walk(
    events: Sequence[DoorEvent],
    gaps: Sequence[StreamGap],
    policy: PairingPolicy,
    *,
    person_id: str,
    space_id: str,
    now_utc: datetime,
    policy_fingerprint: str,
) -> tuple[list[Interval], list[UUID]]:
    """Walk one person's events in one space. Returns (intervals, open enters).

    ``events`` must already be deduplicated, attributable and sorted.
    """
    zone = policy.zone()
    intervals: list[Interval] = []
    open_enters: list[UUID] = []

    state = MachineState.IDLE
    open_enter: DoorEvent | None = None
    previous: DoorEvent | None = None

    def emit(
        enter: DoorEvent | None,
        exit_: DoorEvent | None,
        state_: PairingState,
        flags: tuple[Flag, ...],
        *,
        start: datetime | None,
        end: datetime | None,
    ) -> None:
        anchor = start or end
        assert anchor is not None
        day = local_day(anchor, zone)
        seconds = duration_s(start, end) if start is not None and end is not None else None
        intervals.append(
            Interval(
                interval_id=interval_id(
                    policy_fingerprint=policy_fingerprint,
                    person_id=person_id,
                    space_id=space_id,
                    day=day,
                    enter_event_id=enter.event_id if enter else None,
                    exit_event_id=exit_.event_id if exit_ else None,
                ),
                person_id=person_id,
                space_id=space_id,
                local_day=day,
                enter_event_id=enter.event_id if enter else None,
                exit_event_id=exit_.event_id if exit_ else None,
                start_utc=start,
                end_utc=end,
                duration_s=seconds,
                state=state_,
                flags=flags,
            )
        )

    for event in events:
        if state is MachineState.IDLE:
            if event.direction == "enter":
                open_enter = event
                state = MachineState.INSIDE
            else:
                # An exit with nothing open. Whether that is a repeat exit or a
                # first exit with no enter changes the flag a reviewer sees, and
                # changes neither the state's chargeability nor the money.
                double = previous is not None and previous.direction == "exit"
                flags: tuple[Flag, ...] = (Flag.DOUBLE_EXIT,) if double else (Flag.MISSING_ENTER,)
                if _skewed(event, policy):
                    flags = (*flags, Flag.CLOCK_ANOMALY)
                if event.clip_ref is None:
                    flags = (*flags, Flag.MISSING_CLIP)
                emit(
                    None,
                    event,
                    PairingState.AMBIGUOUS if double else PairingState.UNPAIRED_EXIT,
                    flags,
                    start=None,
                    end=event.ts_utc,
                )
        else:
            assert open_enter is not None
            if event.direction == "enter":
                # Second enter while inside. The FIRST enter stays the interval's
                # start candidate; §4 forbids picking the more likely exit, so no
                # exit is chosen at all. The second enter becomes the new open
                # one -- the day is flagged either way, so this cannot quietly
                # become a charge.
                flags = (Flag.DOUBLE_ENTER,)
                if _skewed(open_enter, policy) or _skewed(event, policy):
                    flags = (*flags, Flag.CLOCK_ANOMALY)
                if open_enter.clip_ref is None or event.clip_ref is None:
                    flags = (*flags, Flag.MISSING_CLIP)
                emit(
                    open_enter,
                    None,
                    PairingState.AMBIGUOUS,
                    flags,
                    start=open_enter.ts_utc,
                    end=None,
                )
                open_enter = event
            else:
                intervals.append(
                    _close(
                        open_enter,
                        event,
                        gaps,
                        policy,
                        person_id=person_id,
                        space_id=space_id,
                        now_utc=now_utc,
                        policy_fingerprint=policy_fingerprint,
                    )
                )
                open_enter = None
                state = MachineState.IDLE
        previous = event

    if open_enter is not None:
        # Still inside at the end of the evidence. Only force it closed once the
        # plausibility ceiling has passed -- before that the person is simply in
        # the canteen, and inventing an exit is the punitive default SOUL.md
        # forbids. This is also what makes property 5 hold: a day that is not yet
        # closed has no number to increase.
        deadline = open_enter.ts_utc.timestamp() + policy.max_dwell_s
        if now_utc.timestamp() >= deadline:
            flags = (Flag.MISSING_EXIT,)
            if _skewed(open_enter, policy):
                flags = (*flags, Flag.CLOCK_ANOMALY)
            if open_enter.clip_ref is None:
                flags = (*flags, Flag.MISSING_CLIP)
            if overlaps_gap(open_enter.ts_utc, now_utc, gaps, space_id, now_utc):
                flags = (*flags, Flag.STREAM_GAP)
            emit(
                open_enter,
                None,
                PairingState.UNPAIRED_ENTER,
                flags,
                start=open_enter.ts_utc,
                end=None,
            )
        else:
            open_enters.append(open_enter.event_id)

    return intervals, open_enters


def _close(
    enter: DoorEvent,
    exit_: DoorEvent,
    gaps: Sequence[StreamGap],
    policy: PairingPolicy,
    *,
    person_id: str,
    space_id: str,
    now_utc: datetime,
    policy_fingerprint: str,
) -> Interval:
    """Resolve a matched enter/exit pair. Precedence is documented at the top."""
    zone = policy.zone()
    start, end = enter.ts_utc, exit_.ts_utc
    day = local_day(start, zone)
    seconds = duration_s(start, end)
    flags: tuple[Flag, ...] = ()

    if local_day(end, zone) != day:
        flags = (*flags, Flag.SPANS_BOUNDARY)
    if enter.clip_ref is None or exit_.clip_ref is None:
        flags = (*flags, Flag.MISSING_CLIP)

    if end < start or _skewed(enter, policy) or _skewed(exit_, policy):
        state = PairingState.AMBIGUOUS
        flags = (*flags, Flag.CLOCK_ANOMALY)
    elif overlaps_gap(start, end, gaps, space_id, now_utc):
        state = PairingState.GAP_AFFECTED
        flags = (*flags, Flag.STREAM_GAP)
    elif seconds < policy.min_dwell_s:
        state = PairingState.IMPLAUSIBLE
        flags = (*flags, Flag.TOO_SHORT)
    elif seconds > policy.max_dwell_s:
        state = PairingState.IMPLAUSIBLE
        flags = (*flags, Flag.TOO_LONG)
    else:
        state = PairingState.RESOLVED

    return Interval(
        interval_id=interval_id(
            policy_fingerprint=policy_fingerprint,
            person_id=person_id,
            space_id=space_id,
            day=day,
            enter_event_id=enter.event_id,
            exit_event_id=exit_.event_id,
        ),
        person_id=person_id,
        space_id=space_id,
        local_day=day,
        enter_event_id=enter.event_id,
        exit_event_id=exit_.event_id,
        start_utc=start,
        end_utc=end,
        duration_s=seconds,
        state=state,
        flags=flags,
    )
