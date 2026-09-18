"""A short, single-camera tracker. Nothing more, on purpose.

ADR-0002 resolves identity at doorways only: no cross-camera tracking, no
re-identification. This tracker exists to link the few frames of one person
walking through one doorway, so that a line crossing can be detected at all.
Three properties keep it on the right side of that line, and each is a test:

* Track ids are process-local, monotonic, and **never stored**. A persisted
  track id is the first step of re-ID.
* **Samples are trimmed to a short window** (``sample_window_s``), so a track
  carries a few seconds of geometry and never a history. ``max_age_s`` is a
  separate hard ceiling on a track's life, generous enough for someone walking
  in from the edge of frame but bounded: "caching a person's last known
  location to help with pairing" is on AGENTS.md §9's list of things that look
  helpful and are not.

  The window and the ceiling are different jobs and were briefly the same one,
  which is how the rig replay lost three crossings: a 3-second ceiling dropped
  the track of anyone who took longer than that to reach the line -- a slow
  walker, or anyone starting at the far edge of frame -- and the crossing
  vanished with it. The ceiling now bounds memory; hysteresis is judged from
  the trimmed window.
* Association uses geometry only -- IoU and centroid distance. **No appearance
  features of any kind**, because an appearance descriptor is a re-ID feature
  whatever it is called.

It is also deterministic: candidate pairs are sorted by a total order before the
greedy assignment. Greedy association over an unordered mapping is the classic
source of run-to-run drift, and it would make the rig replay unreproducible.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from argus.backends.types import Box, Detection


@dataclass(frozen=True, slots=True)
class TrackSample:
    ts_server: datetime
    # Bottom-centre of the box, in source pixels. A head-centre reference
    # crosses a floor-projected line early or late as a function of height,
    # which is a systematic bias by body size rather than noise -- SOUL.md
    # names exactly that among the harms.
    point: tuple[float, float]
    box: Box
    score: float


@dataclass(slots=True)
class Track:
    track_id: int
    samples: list[TrackSample] = field(default_factory=list)

    @property
    def first_seen(self) -> datetime:
        return self.samples[0].ts_server

    @property
    def last_seen(self) -> datetime:
        return self.samples[-1].ts_server

    @property
    def last_box(self) -> Box:
        return self.samples[-1].box

    def age_s(self, now: datetime) -> float:
        return (now - self.first_seen).total_seconds()


def bottom_centre(box: Box) -> tuple[float, float]:
    return ((box.x1 + box.x2) / 2.0, box.y2)


def _centroid_distance(a: Box, b: Box) -> float:
    ax, ay = (a.x1 + a.x2) / 2.0, (a.y1 + a.y2) / 2.0
    bx, by = (b.x1 + b.x2) / 2.0, (b.y1 + b.y2) / 2.0
    return math.hypot(ax - bx, ay - by)


class ShortTracker:
    def __init__(
        self,
        *,
        max_age_s: float = 20.0,
        max_gap_s: float = 0.6,
        iou_gate: float = 0.2,
        max_travel_frac: float = 0.15,
        max_tracks: int = 64,
        sample_window_s: float = 4.0,
    ) -> None:
        self.sample_window_s = sample_window_s
        self.max_age_s = max_age_s
        self.max_gap_s = max_gap_s
        self.iou_gate = iou_gate
        self.max_travel_frac = max_travel_frac
        self.max_tracks = max_tracks
        self._next_id = 1
        self._live: dict[int, Track] = {}

    @property
    def live(self) -> list[Track]:
        return [self._live[k] for k in sorted(self._live)]

    def update(
        self,
        ts: datetime,
        detections: list[Detection],
        frame_width: int,
    ) -> list[Track]:
        """Attach detections to tracks; return the tracks that ended this frame.

        A track ends when nothing matched it for ``max_gap_s``, or when it hits
        the ``max_age_s`` ceiling. Ended tracks are what the doorway detector
        reads, so ending them promptly is what keeps a crossing's latency near
        one sample period rather than near the ceiling.
        """
        # Two expiry passes, and both results are returned. The first ends
        # tracks nothing has matched for max_gap_s -- they must not compete for
        # this frame's detections. The second catches tracks that have just hit
        # the max_age_s ceiling. Dropping either set on the floor would strand
        # a finished crossing in the tracker until shutdown.
        ended = self._expire_stale(ts)
        travel_limit = self.max_travel_frac * max(1, frame_width)
        candidates: list[tuple[float, float, int, int]] = []
        for det_index, det in enumerate(detections):
            for track_id, track in self._live.items():
                iou = track.last_box.iou(det.box)
                distance = _centroid_distance(track.last_box, det.box)
                if iou < self.iou_gate and distance > travel_limit:
                    continue
                # Sorted by (-iou, distance, track_id, det_index): a total order,
                # so the assignment cannot depend on dict iteration order.
                candidates.append((-iou, distance, track_id, det_index))
        candidates.sort()

        claimed_tracks: set[int] = set()
        claimed_dets: set[int] = set()
        for _, _, track_id, det_index in candidates:
            if track_id in claimed_tracks or det_index in claimed_dets:
                continue
            claimed_tracks.add(track_id)
            claimed_dets.add(det_index)
            det = detections[det_index]
            track = self._live[track_id]
            track.samples.append(
                TrackSample(
                    ts_server=ts,
                    point=bottom_centre(det.box),
                    box=det.box,
                    score=det.score,
                )
            )
            self._trim(track)

        for det_index, det in enumerate(detections):
            if det_index in claimed_dets:
                continue
            if len(self._live) >= self.max_tracks:
                # Refusing to grow is better than an unbounded dict on a busy
                # doorway; the oldest track is the one most likely already done.
                oldest = min(self._live, key=lambda k: self._live[k].last_seen)
                del self._live[oldest]
            track = Track(track_id=self._next_id)
            self._next_id += 1
            track.samples.append(
                TrackSample(
                    ts_server=ts, point=bottom_centre(det.box), box=det.box, score=det.score
                )
            )
            self._live[track.track_id] = track

        return ended + self._expire_stale(ts)

    def flush(self, ts: datetime) -> list[Track]:
        """End every live track: the stream stopped, so nothing will match them."""
        ended = self.live
        self._live.clear()
        return ended

    def _expire_stale(self, ts: datetime) -> list[Track]:
        ended: list[Track] = []
        for track_id in sorted(self._live):
            track = self._live[track_id]
            silent_for = (ts - track.last_seen).total_seconds()
            if silent_for > self.max_gap_s or track.age_s(ts) > self.max_age_s:
                ended.append(track)
                del self._live[track_id]
        return ended

    def _trim(self, track: Track) -> None:
        """Keep a short window of samples. Geometry, not history."""
        horizon = track.samples[-1].ts_server - timedelta(seconds=self.sample_window_s)
        if track.samples[0].ts_server >= horizon:
            return
        kept = [s for s in track.samples if s.ts_server >= horizon]
        # Always keep at least the last two samples, whatever the window: one
        # sample is not a direction.
        track.samples = kept if len(kept) >= 2 else track.samples[-2:]

    def window(self, track: Track, around: datetime, span: timedelta) -> list[TrackSample]:
        """Samples within `span` of a moment; used to pick face frames."""
        return [s for s in track.samples if abs(s.ts_server - around) <= span]
