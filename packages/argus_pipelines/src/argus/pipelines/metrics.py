"""Counters the pipelines emit, and the ones they cannot.

RISKS.md §8 asks M3 for four numbers from day one. Two of them are pipeline
facts -- the unknown-face rate and crossings by direction -- and two are
functions of stored rows: the unpaired-event rate and stream-gap minutes are
properties of what pairing produced, not of any frame. Pretending otherwise
would ship four metrics of which two are permanently zero, which is worse than
two metrics and an honest note.

So: live counters here, SQL rollups in the reporting CLI, same names.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

# Live, from a pipeline.
CROSSINGS = "canteen_crossing_total"
UNKNOWN_FACE = "canteen_unknown_face_total"
FRAMES_ANALYSED = "frames_analysed_total"
FRAMES_DROPPED = "frames_dropped_total"
EVENTS_WRITTEN = "doorway_event_written_total"
CLIP_FAILURES = "clip_extraction_failed_total"
AIM_DRIFT = "camera_aim_drift_total"
VIOLENCE_CANDIDATES = "violence_candidate_total"

# Derived from stored rows (see argus.report): unpaired_event_rate,
# stream_gap_minutes, flagged_day_percent, clip_access_total,
# violence_dismissal_latency_s.


class MetricsSink(Protocol):
    def incr(self, name: str, value: float = 1.0, **labels: str) -> None: ...

    def observe(self, name: str, value: float, **labels: str) -> None: ...


def _key(name: str, labels: dict[str, str]) -> str:
    if not labels:
        return name
    rendered = ",".join(f"{k}={v}" for k, v in sorted(labels.items()))
    return f"{name}{{{rendered}}}"


@dataclass
class InMemoryMetrics:
    """Counters for tests and the ingest status line. No exporter, by decision.

    An exporter would be a data-egress conversation (AGENTS.md §2.6), and these
    numbers are readable from the console page that already exists.
    """

    counters: dict[str, float] = field(default_factory=dict)
    observations: dict[str, list[float]] = field(default_factory=dict)

    def incr(self, name: str, value: float = 1.0, **labels: str) -> None:
        key = _key(name, labels)
        self.counters[key] = self.counters.get(key, 0.0) + value

    def observe(self, name: str, value: float, **labels: str) -> None:
        self.observations.setdefault(_key(name, labels), []).append(value)

    def get(self, name: str, **labels: str) -> float:
        return float(self.counters.get(_key(name, labels), 0.0))

    def as_lines(self) -> list[str]:
        lines = [f"{k}={v:g}" for k, v in sorted(self.counters.items())]
        for key, values in sorted(self.observations.items()):
            if values:
                lines.append(f"{key}.mean={sum(values) / len(values):g}")
        return lines


class NullMetrics:
    def incr(self, name: str, value: float = 1.0, **labels: str) -> None:
        return None

    def observe(self, name: str, value: float, **labels: str) -> None:
        return None
