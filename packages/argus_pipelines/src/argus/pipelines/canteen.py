"""The canteen doorway pipeline: frames in, doorway events and clips out.

This is the one pipeline that touches pay, so its failure behaviour is the
design and the happy path is the easy part.

``_emit`` is the only place in the system that writes a doorway event, and the
order inside it is forced by the schema: doorway_event is append-only, so an
event written before its clip can never have its clip_ref filled in afterwards.
Hence clip first, with a bounded timeout -- and **if the clip fails, the event
is still written, with clip_ref null**. Dropping the event would lose evidence
*and* reduce the flag count, which is a failure in the punitive direction: the
day would look clean. With the event and no clip, payroll flags the day and
charges nothing (ADR-0008).

Everything else here is about not claiming to measure when we are not: a
crashed analyser, a drifted camera and a sustained frame-drop rate all open a
stream_gap, each with its own cause.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

from argus.backends.interfaces import Detector
from argus.backends.types import Box, Detection
from argus.common.clock import Clock, SystemClock
from argus.common.config import CameraConfig, CanteenConfig
from argus.pipelines import metrics as m
from argus.pipelines.aim import AimMonitor
from argus.pipelines.base import ClipWriter, EventSink, FrameRef, FrameSource, GapKeeper
from argus.pipelines.doorway import Crossing, DoorLine, DoorwayDetector
from argus.pipelines.faces import FaceStage
from argus.pipelines.metrics import MetricsSink, NullMetrics
from argus.pipelines.runtime import Dropped, SessionRuntime
from argus.pipelines.tracking import ShortTracker, Track

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class EmittedEvent:
    """What the pipeline wrote, for tests and for the status line."""

    event_id: Any
    ts_utc: datetime
    direction: str
    person_id: str | None
    clip_path: Path | None
    duplicate_of: UUID | None
    flags: tuple[str, ...]


class CanteenPipeline:
    def __init__(
        self,
        camera: CameraConfig,
        source: FrameSource,
        events: EventSink,
        *,
        detector: SessionRuntime[Detector],
        clips: ClipWriter | None = None,
        faces: FaceStage | None = None,
        aim: AimMonitor | None = None,
        cfg: CanteenConfig | None = None,
        clock: Clock | None = None,
        ingest_run_id: UUID | None = None,
        metrics: MetricsSink | None = None,
    ) -> None:
        if camera.door_line is None:
            raise ValueError(
                f"camera {camera.camera_id} has no door_line; a canteen pipeline without a "
                "line would have nothing to cross (config/dev.yaml)"
            )
        self.camera = camera
        self.source = source
        self.events = events
        self.detector = detector
        self.clips = clips
        self.faces = faces
        self.aim = aim
        self.cfg = cfg or CanteenConfig()
        self.clock = clock or SystemClock()
        self.ingest_run_id = ingest_run_id
        self.metrics: MetricsSink = metrics or NullMetrics()
        self.gap = GapKeeper(events, camera.camera_id, self.clock)
        self.line = DoorLine.from_camera(camera.door_line, camera.inside_sign)
        self.doorway = DoorwayDetector(
            self.line,
            band=self.cfg.band,
            min_samples_per_side=self.cfg.min_samples_per_side,
        )
        self.tracker = ShortTracker(
            max_age_s=self.cfg.track_max_age_s,
            max_gap_s=self.cfg.track_max_gap_s,
            sample_window_s=self.cfg.track_sample_window_s,
        )
        # Crossings are watched as samples arrive, not when a track ends: a
        # track that ends mid-passage would otherwise take its crossing with it.
        self.watcher = self.doorway.watcher()
        self.emitted: list[EmittedEvent] = []
        self._recent_frames: list[FrameRef] = []
        self._last_emission: dict[tuple[str | None, str], tuple[datetime, UUID]] = {}
        self._last_aim_check: datetime | None = None
        self._stopping = asyncio.Event()
        self._analysed = 0
        self._dropped = 0

    @property
    def name(self) -> str:
        return f"canteen-{self.camera.camera_id}"

    def stop(self) -> None:
        self._stopping.set()

    async def run(self) -> None:
        """Consume frames until stopped. One more subscriber on a bounded queue."""
        queue = self.source.subscribe()
        try:
            await self.detector.wait_ready()
            while not self._stopping.is_set():
                try:
                    frame = await asyncio.wait_for(queue.get(), timeout=1.0)
                except TimeoutError:
                    continue
                await self.handle(frame)
        finally:
            self.source.unsubscribe(queue)
            with contextlib.suppress(Exception):
                await self._finish_open_tracks()

    async def handle(self, frame: Any) -> list[EmittedEvent]:
        """Analyse one frame; return whatever it caused to be written."""
        ref = FrameRef(ts_server=frame.ts_server, ts_media=frame.ts_media, image=frame.image)
        self._remember(ref)

        if await self._aim_blocks(ref):
            return []

        try:
            detections = await self.detector.submit(
                lambda backend: backend.detect(ref.image, score_threshold=self.cfg.person_score)
            )
        except Dropped:
            self._dropped += 1
            self.metrics.incr(m.FRAMES_DROPPED, camera_id=self.camera.camera_id)
            await self._check_drop_rate()
            return []

        self._analysed += 1
        self.metrics.incr(m.FRAMES_ANALYSED, camera_id=self.camera.camera_id)
        await self.gap.measuring()

        people = [d for d in detections if _is_person(d)]
        height, width = ref.image.shape[:2]
        finished = self.tracker.update(ref.ts_server, people, width)
        written: list[EmittedEvent] = []
        for track in self.tracker.live:
            sample = track.samples[-1]
            if sample.ts_server != ref.ts_server:
                continue  # nothing matched this track on this frame
            for crossing in self.watcher.observe(track.track_id, sample, (height, width)):
                written.append(await self._emit(track, crossing))
        for track in finished:
            # The track is over, so its zone runs are over too. Forgetting is
            # the whole of "we do not remember people" here.
            self.watcher.forget(track.track_id)
        return written

    async def _finish_open_tracks(self) -> list[EmittedEvent]:
        """Shutting down. Crossings are already emitted as they happen, so this
        only drops the per-track state -- there is no backlog to flush."""
        for track in self.tracker.flush(self.clock.now_utc()):
            self.watcher.forget(track.track_id)
        return []

    # -- the one place a doorway event is written ---------------------------

    async def _emit(self, track: Track, crossing: Crossing) -> EmittedEvent:
        flags: list[str] = []
        person_id: str | None = None
        confidence: float | None = None
        if self.faces is not None:
            attempt = self.faces.identify(
                [f.image for f in self._frames_near(crossing.ts_utc)],
                person_box=_box_at(track, crossing.ts_utc),
            )
            person_id = attempt.identification.person_id
            confidence = attempt.identification.score
            if person_id is None:
                flags.append(attempt.identification.reason)
                self.metrics.incr(
                    m.UNKNOWN_FACE,
                    camera_id=self.camera.camera_id,
                    reason=attempt.identification.reason,
                )
        else:
            # Faces disabled: unknown is the correct outcome, not an omission.
            flags.append("faces_disabled")
            self.metrics.incr(
                m.UNKNOWN_FACE, camera_id=self.camera.camera_id, reason="faces_disabled"
            )

        clip_path = await self._clip_for(crossing)
        if clip_path is None and self.clips is not None:
            flags.append("missing_clip")

        duplicate_of = self._duplicate_of(person_id, crossing)
        if duplicate_of is not None:
            flags.append("deduplicated")

        event = await self.events.insert_doorway_event(
            self.camera.camera_id,
            self.camera.door_id,
            crossing.ts_utc,
            crossing.direction,
            person_id=person_id,
            match_confidence=confidence,
            detection_quality=crossing.quality,
            clip_ref=self._clip_ref(clip_path),
            ingest_run_id=self.ingest_run_id,
            duplicate_of=duplicate_of,
        )
        event_id = getattr(event, "event_id", event)
        if duplicate_of is None and person_id is not None:
            self._last_emission[(person_id, crossing.direction)] = (crossing.ts_utc, event_id)
        self.metrics.incr(
            m.CROSSINGS, camera_id=self.camera.camera_id, direction=crossing.direction
        )
        self.metrics.incr(m.EVENTS_WRITTEN, camera_id=self.camera.camera_id)
        emitted = EmittedEvent(
            event_id=event_id,
            ts_utc=crossing.ts_utc,
            direction=crossing.direction,
            person_id=person_id,
            clip_path=clip_path,
            duplicate_of=duplicate_of,
            flags=tuple(flags),
        )
        self.emitted.append(emitted)
        log.info(
            "%s %s at %s person=%s clip=%s flags=%s",
            self.camera.camera_id,
            crossing.direction,
            crossing.ts_utc.isoformat(),
            person_id or "unknown",
            clip_path.name if clip_path else "none",
            ",".join(flags) or "-",
        )
        return emitted

    async def _clip_for(self, crossing: Crossing) -> Path | None:
        if self.clips is None:
            return None
        start = crossing.ts_utc - timedelta(seconds=self.cfg.clip_pre_s)
        end = crossing.ts_utc + timedelta(seconds=self.cfg.clip_post_s)
        try:
            return await self.clips.extract(
                self.source, start, end, timeout_s=self.cfg.clip_timeout_s
            )
        except Exception as exc:
            # Not fatal, and not silent. The event is still written with no
            # clip_ref, which flags the day and charges nothing (ADR-0008).
            log.warning(
                "%s: clip extraction failed for %s: %s",
                self.camera.camera_id,
                crossing.ts_utc.isoformat(),
                exc,
            )
            self.metrics.incr(m.CLIP_FAILURES, camera_id=self.camera.camera_id)
            return None

    def _clip_ref(self, clip_path: Path | None) -> str | None:
        """Relative to the clips root, because that is what the row may contain."""
        if clip_path is None or self.clips is None:
            return None
        return str(clip_path.relative_to(Path(self.clips.root)))

    def _duplicate_of(self, person_id: str | None, crossing: Crossing) -> UUID | None:
        """In-process dedup, reading the same window payroll reads.

        Two detections of one crossing within duplicate_window_s are one
        crossing. The later row points back at the earlier one (ADR-0032), and
        payroll dedupes again on read -- the two must use the same number or
        they will disagree about what a duplicate is.
        """
        if person_id is None:
            return None
        previous = self._last_emission.get((person_id, crossing.direction))
        if previous is None:
            return None
        ts, event_id = previous
        if abs((crossing.ts_utc - ts).total_seconds()) <= self.cfg.duplicate_window_s:
            return event_id
        return None

    # -- not measuring? say so ---------------------------------------------

    async def _aim_blocks(self, ref: FrameRef) -> bool:
        if self.aim is None:
            return False
        due = (
            self._last_aim_check is None
            or (ref.ts_server - self._last_aim_check).total_seconds()
            >= self.cfg.aim_check_interval_s
        )
        if due:
            self._last_aim_check = ref.ts_server
            state = self.aim.check(ref.image)
            if state == "drifted":
                self.metrics.incr(m.AIM_DRIFT, camera_id=self.camera.camera_id)
                await self.gap.stopped("aim_changed")
            elif state == "recovered":
                await self.gap.measuring()
        return self.aim.drifted

    async def _check_drop_rate(self) -> None:
        total = self._analysed + self._dropped
        if total < self.cfg.drop_rate_window:
            return
        rate = self._dropped / total
        if rate > self.cfg.max_drop_rate:
            await self.gap.stopped("overload")

    # -- frame bookkeeping --------------------------------------------------

    def _remember(self, ref: FrameRef) -> None:
        self._recent_frames.append(ref)
        horizon = ref.ts_server - timedelta(seconds=self.cfg.track_max_age_s + 1.0)
        self._recent_frames = [f for f in self._recent_frames if f.ts_server >= horizon]

    def _frames_near(self, ts: datetime) -> list[FrameRef]:
        """Frames closest to the crossing, best first, for best-of-K."""
        return sorted(self._recent_frames, key=lambda f: abs((f.ts_server - ts).total_seconds()))


def _is_person(detection: Detection) -> bool:
    # "disk" is the mock detector's label: the rig draws people as bright disks,
    # and a pipeline that silently found nothing on rig footage would be a
    # frustrating way to discover that.
    return detection.label in ("person", "disk")


def _box_at(track: Track, ts: datetime) -> Box | None:
    if not track.samples:
        return None
    nearest = min(track.samples, key=lambda s: abs((s.ts_server - ts).total_seconds()))
    return nearest.box
