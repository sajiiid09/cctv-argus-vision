"""Pairing policy: every threshold that decides a number, in one frozen object.

Each PROVISIONAL constant in DATA_MODEL.md §4 is a named field here rather than
a literal inside a branch, so that changing one is visible in a diff and
recorded in the run that used it.

Two fields are deliberately absent.

There is no ``day_zeroing`` switch. Making the whole-day-zeroing rule
configurable would put "which cases resolve to a deduction" -- an AGENTS.md §2.2
sign-off item -- behind a YAML key. ADR-0023 decided it; the code encodes it.

There is no shadow-mode flag. Whether a result may be exported is a property of
an export that does not exist, and is none of pairing's business.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from zoneinfo import ZoneInfo


class PolicyError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class PairingPolicy:
    timezone: str = "Asia/Dhaka"
    # The canteen allowance, per local day, across all visits -- not per visit.
    # 3600 in production; the demo runs 120 and says so out loud.
    allowance_s: int = 3600
    # DATA_MODEL.md §4 plausibility bounds. PROVISIONAL.
    min_dwell_s: int = 60
    max_dwell_s: int = 10800
    # Two detections of one crossing inside this window are one crossing.
    duplicate_window_s: float = 3.0
    # Below this match confidence an identification is treated as unknown.
    # Never a best guess (THREAT_MODEL.md §1).
    identity_threshold: float = 0.0
    # How far the camera-reported time may disagree with the server clock before
    # the pair is called a clock anomaly. PROVISIONAL: cameras drift by seconds
    # routinely, so zero would flag everything; a minute is a guess pending
    # measurement on real hardware.
    clock_skew_tolerance_s: float = 60.0

    def __post_init__(self) -> None:
        if self.allowance_s < 0:
            raise PolicyError(f"allowance_s must not be negative, got {self.allowance_s}")
        if self.min_dwell_s < 0:
            raise PolicyError(f"min_dwell_s must not be negative, got {self.min_dwell_s}")
        if self.max_dwell_s <= self.min_dwell_s:
            raise PolicyError(
                f"max_dwell_s ({self.max_dwell_s}) must exceed min_dwell_s ({self.min_dwell_s})"
            )
        # An interval may cross at most one local midnight, which is what makes
        # day attribution total. A ceiling of a day or more would break that.
        if self.max_dwell_s >= 86400:
            raise PolicyError(
                f"max_dwell_s ({self.max_dwell_s}) must be under 24h: day attribution "
                "assumes an interval crosses at most one local midnight "
                "(DATA_MODEL.md §4)"
            )
        if self.duplicate_window_s <= 0:
            raise PolicyError(f"duplicate_window_s must be positive, got {self.duplicate_window_s}")
        if self.clock_skew_tolerance_s < 0:
            raise PolicyError("clock_skew_tolerance_s must not be negative")
        try:
            ZoneInfo(self.timezone)
        except Exception as e:
            raise PolicyError(f"unknown policy timezone {self.timezone!r}: {e}") from e

    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def as_json(self) -> dict[str, object]:
        return dict(sorted(asdict(self).items()))

    def fingerprint(self) -> str:
        """Stable hash of the policy, recorded alongside every result.

        Two runs that disagree are then traceable to the policy that produced
        each, rather than to a remembered configuration.
        """
        canonical = json.dumps(self.as_json(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()[:16]

    def describe(self) -> str:
        """One line for the top of a report page.

        The allowance has to be visible on the artefact, not just said by
        whoever is presenting it.
        """
        allowance = _humanise(self.allowance_s)
        return (
            f"allowance {allowance} per local day; dwell floor "
            f"{_humanise(self.min_dwell_s)}, ceiling {_humanise(self.max_dwell_s)}; "
            f"timezone {self.timezone}"
        )


def _humanise(seconds: int) -> str:
    if seconds % 3600 == 0 and seconds >= 3600:
        hours = seconds // 3600
        return f"{hours} hour" if hours == 1 else f"{hours} hours"
    if seconds % 60 == 0 and seconds >= 60:
        minutes = seconds // 60
        return f"{minutes} minute" if minutes == 1 else f"{minutes} minutes"
    return f"{seconds} s"
