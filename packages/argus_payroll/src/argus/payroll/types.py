"""Value types for pairing.

These are payroll's own types, not the store's. The store's row classes carry a
database dependency and are mutable; pairing has to be importable and testable
with no database at all, so the service layer maps between them. The mapping is
ten lines and it is worth them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

Direction = Literal["enter", "exit", "ambiguous"]


class MachineState(StrEnum):
    """Internal walk state. Not stored."""

    IDLE = "idle"
    INSIDE = "inside"


class PairingState(StrEnum):
    """Terminal state of one interval. Values match the SQL check constraint."""

    RESOLVED = "resolved"
    UNPAIRED_ENTER = "unpaired_enter"
    UNPAIRED_EXIT = "unpaired_exit"
    AMBIGUOUS = "ambiguous"
    IMPLAUSIBLE = "implausible"
    GAP_AFFECTED = "gap_affected"


class Flag(StrEnum):
    """Why a reviewer is being shown this. Flags accumulate; state does not."""

    DOUBLE_ENTER = "double_enter"
    DOUBLE_EXIT = "double_exit"
    MISSING_EXIT = "missing_exit"
    MISSING_ENTER = "missing_enter"
    TOO_SHORT = "too_short"
    TOO_LONG = "too_long"
    STREAM_GAP = "stream_gap"
    CLOCK_ANOMALY = "clock_anomaly"
    UNKNOWN_PERSON = "unknown_person"
    LOW_CONFIDENCE = "low_confidence"
    SPANS_BOUNDARY = "spans_boundary"
    DEDUPLICATED = "deduplicated"
    MISSING_CLIP = "missing_clip"


@dataclass(frozen=True, slots=True)
class DoorEvent:
    event_id: UUID
    ts_utc: datetime  # tz-aware UTC; server clock, authoritative
    space_id: str
    camera_id: str
    direction: Direction
    door_id: str | None = None
    # What the camera said the time was. Recorded for audit and NEVER used in
    # arithmetic (ARCHITECTURE.md §7.1) -- its only job here is to make the
    # clock-anomaly row of the §4 case table detectable, by disagreeing with
    # ts_utc by more than the policy tolerates.
    ts_camera_reported: datetime | None = None
    person_id: str | None = None
    match_confidence: float | None = None
    clip_ref: str | None = None
    # Set on the LATER of two detections of the same crossing, pointing at the
    # earlier one, which is the row that survives (ARCHITECTURE.md §7.3). Pairing
    # ignores any event that sets it.
    duplicate_of: UUID | None = None


@dataclass(frozen=True, slots=True)
class Gap:
    camera_id: str
    from_utc: datetime
    to_utc: datetime | None  # None = still open
    cause: str
    # Resolved by the caller from camera.space_id. Pairing is per space, so a gap
    # on ANY door camera of that space affects intervals in it -- the unseen exit
    # might have been at the other door. Attributing gaps per door would
    # under-flag exactly the multi-door case ARCHITECTURE.md §7.3 calls out.
    space_id: str | None = None


@dataclass(frozen=True, slots=True)
class Interval:
    interval_id: UUID
    person_id: str
    space_id: str
    local_day: date
    enter_event_id: UUID | None
    exit_event_id: UUID | None
    start_utc: datetime | None
    end_utc: datetime | None
    duration_s: int | None
    state: PairingState
    flags: tuple[Flag, ...]


@dataclass(frozen=True, slots=True)
class DayResult:
    person_id: str
    space_id: str
    local_day: date
    total_dwell_s: int
    allowance_s: int
    overage_s: int
    day_state: Literal["clean", "flagged"]
    flags: tuple[Flag, ...]
    interval_ids: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class Unattributable:
    """Evidence that belongs to nobody chargeable. Retained, never charged."""

    event_id: UUID
    flag: Flag


@dataclass(frozen=True, slots=True)
class PairingResult:
    logic_version: str
    policy_fingerprint: str
    computed_at: datetime
    intervals: tuple[Interval, ...]
    days: tuple[DayResult, ...]
    open_enters: tuple[UUID, ...]  # still inside; not yet an interval
    unattributable: tuple[Unattributable, ...]
