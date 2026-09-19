"""The four-way outcome, and the order the checks run in.

The order *is* the design:

1. A replayed tap is `not_attempted`, decided **before anything else is even
   read**. The person walked past hours ago; recording it as `no_face` would
   claim we looked.
2. No template for the badge holder is also `not_attempted` -- we could not have
   compared, whatever the camera saw.
3. No usable face in the verification window is `no_face`. That is a camera
   problem.
4. Only then does a score decide `true` or `false`. That is a person problem,
   and the interesting one.

`no_face` must never collapse into `false` (ARCHITECTURE.md §7.2): not seeing a
face and seeing a *different* face are the only distinction this pipeline
exists to make.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import assert_never

from argus.pipelines.gate.taps import TapChannel


class VerifyOutcome(StrEnum):
    """Values match the gate_event.face_verified check constraint."""

    TRUE = "true"
    FALSE = "false"
    NO_FACE = "no_face"
    NOT_ATTEMPTED = "not_attempted"


@dataclass(frozen=True, slots=True)
class VerifyInput:
    channel: TapChannel
    template_present: bool
    face_found: bool
    score: float | None
    threshold: float | None


@dataclass(frozen=True, slots=True)
class Verdict:
    outcome: VerifyOutcome
    score: float | None
    reason: str

    @property
    def is_mismatch(self) -> bool:
        return self.outcome is VerifyOutcome.FALSE


def decide(state: VerifyInput) -> Verdict:
    if state.channel is TapChannel.REPLAY:
        return Verdict(
            VerifyOutcome.NOT_ATTEMPTED,
            None,
            "recovered from the reader's log; nothing was compared",
        )
    if not state.template_present:
        return Verdict(VerifyOutcome.NOT_ATTEMPTED, None, "no face template for the badge holder")
    if state.threshold is None:
        # No measured threshold means no comparison is possible (ADR-0010).
        # Saying "not attempted" is the truth; inventing a number is not.
        return Verdict(VerifyOutcome.NOT_ATTEMPTED, None, "no measured gate threshold configured")
    if not state.face_found or state.score is None:
        return Verdict(VerifyOutcome.NO_FACE, None, "no usable face in the window")
    if state.score >= state.threshold:
        return Verdict(VerifyOutcome.TRUE, state.score, "matched the badge holder")
    return Verdict(VerifyOutcome.FALSE, state.score, "a different face from the badge holder")


def describe(outcome: VerifyOutcome) -> str:
    """What each outcome means to whoever reads the gate page."""
    match outcome:
        case VerifyOutcome.TRUE:
            return "the badge holder tapped their own badge"
        case VerifyOutcome.FALSE:
            return "a different face: possible buddy-punching, for review"
        case VerifyOutcome.NO_FACE:
            return "no usable face: a camera or capture problem, not a person problem"
        case VerifyOutcome.NOT_ATTEMPTED:
            return "no comparison was made"
        case _:  # pragma: no cover - exhaustive over the enum
            assert_never(outcome)
