"""The floor camera's two consumers: occupancy, and the violence trigger.

Both run off the same frames as one task per camera, because they are the same
question asked twice -- what is happening on the floor -- and because a second
subscriber is cheaper than a second decode.

Neither writes a `person_id`. Occupancy measures presence at a coordinate and
violence hands a clip to a human; if either ever needed identity, that would be
an `AGENTS.md` §2.3 sign-off and a new ADR rather than a column.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any
from uuid import UUID, uuid4

from argus.common.clock import Clock, SystemClock
from argus.common.config import CameraConfig, OccupancyConfig, ViolenceConfig
from argus.ingest.clip import ClipStore
from argus.ingest.streams import RTSPSource
from argus.pipelines import metrics as m
from argus.pipelines.base import GapKeeper
from argus.pipelines.metrics import MetricsSink, NullMetrics
from argus.pipelines.occupancy import INSERT_SAMPLE, OccupancyPipeline
from argus.pipelines.runtime import Dropped, SessionRuntime
from argus.pipelines.violence import INSERT_CANDIDATE, ViolenceTrigger
from argus.store.db import Database

log = logging.getLogger(__name__)


class FloorRunner:
    """One floor camera: seats sampled slowly, pose watched continuously."""

    def __init__(
        self,
        camera: CameraConfig,
        source: RTSPSource,
        db: Database,
        *,
        detector: SessionRuntime[Any] | None,
        pose: SessionRuntime[Any] | None,
        occupancy_cfg: OccupancyConfig,
        violence_cfg: ViolenceConfig,
        clips: ClipStore | None = None,
        clock: Clock | None = None,
        metrics: MetricsSink | None = None,
    ) -> None:
        self.camera = camera
        self.source = source
        self.db = db
        self.detector = detector
        self.pose = pose
        self.clips = clips
        self.clock = clock or SystemClock()
        self.metrics: MetricsSink = metrics or NullMetrics()
        self.occupancy = (
            OccupancyPipeline(camera.camera_id, occupancy_cfg)
            if occupancy_cfg.enabled and occupancy_cfg.seats
            else None
        )
        self.violence = (
            ViolenceTrigger(camera.camera_id, violence_cfg) if violence_cfg.enabled else None
        )
        self.violence_cfg = violence_cfg
        self.gap = GapKeeper(_GapSink(db), camera.camera_id, self.clock)
        self._stopping = asyncio.Event()
        self.samples_written = 0
        self.candidates_written = 0

    @property
    def name(self) -> str:
        return f"floor-{self.camera.camera_id}"

    def stop(self) -> None:
        self._stopping.set()

    async def run(self) -> None:
        queue = self.source.subscribe()
        try:
            while not self._stopping.is_set():
                try:
                    frame = await asyncio.wait_for(queue.get(), timeout=1.0)
                except TimeoutError:
                    continue
                await self.handle(frame)
        finally:
            self.source.unsubscribe(queue)

    async def handle(self, frame: Any) -> None:
        if (
            self.occupancy is not None
            and self.detector is not None
            and self.occupancy.due(frame.ts_server)
        ):
            await self._sample_seats(frame)
        if self.violence is not None and self.pose is not None:
            await self._watch_pose(frame)

    async def _sample_seats(self, frame: Any) -> None:
        assert self.occupancy is not None and self.detector is not None
        try:
            detections = await self.detector.submit(lambda backend: backend.detect(frame.image))
        except Dropped:
            self.metrics.incr(m.FRAMES_DROPPED, camera_id=self.camera.camera_id)
            return
        people = [d for d in detections if d.label in ("person", "disk")]
        observations = self.occupancy.observe(
            frame.ts_server, people, frame.image.shape[:2]
        )
        for row in self.occupancy.rows(observations):
            await self.db.execute(INSERT_SAMPLE, row)
        self.samples_written += len(observations)
        await self.gap.measuring()

    async def _watch_pose(self, frame: Any) -> None:
        assert self.violence is not None and self.pose is not None
        try:
            people = await self.pose.submit(lambda backend: backend.estimate(frame.image))
        except Dropped:
            self.metrics.incr(m.FRAMES_DROPPED, camera_id=self.camera.camera_id)
            return
        candidate = self.violence.observe(frame.ts_server, people)
        if candidate is None:
            return
        clip_id = await self._clip_for(candidate)
        await self.db.execute(INSERT_CANDIDATE, self.violence.row(candidate, clip_id))
        self.candidates_written += 1
        self.metrics.incr(m.VIOLENCE_CANDIDATES, camera_id=self.camera.camera_id)
        log.info(
            "violence candidate on %s at %s score=%.2f clip=%s -- for a human to watch",
            self.camera.camera_id,
            candidate.at.isoformat(),
            candidate.score,
            clip_id or "none",
        )

    async def _clip_for(self, candidate: Any) -> UUID | None:
        """A candidate without a clip is a review item nobody can review.

        It is still written -- the trigger fired, and hiding that would make the
        queue look calmer than the floor was -- but the missing clip is visible
        on the page rather than implied.
        """
        if self.clips is None:
            return None
        from datetime import timedelta

        start = candidate.at - timedelta(seconds=self.violence_cfg.clip_pre_s)
        end = candidate.at + timedelta(seconds=self.violence_cfg.clip_post_s)
        try:
            path = await self.clips.extract(self.source, start, end, timeout_s=15.0)
        except Exception as exc:
            log.warning("%s: clip for candidate failed: %s", self.camera.camera_id, exc)
            self.metrics.incr(m.CLIP_FAILURES, camera_id=self.camera.camera_id)
            return None
        clip_id = uuid4()
        await self.db.execute(
            "insert into clip (clip_id, camera_id, rel_path, start_utc, end_utc, keyframe_utc,"
            " is_virtual) values (%s,%s,%s,%s,%s,%s,%s)",
            (
                clip_id,
                self.camera.camera_id,
                str(path.relative_to(self.clips.root)),
                start,
                end,
                start,
                self.camera.is_virtual,
            ),
        )
        return clip_id


class _GapSink:
    """The slice of EventSink a floor pipeline needs: gaps, and no events.

    A floor camera produces no doorway events by design (identity lives at
    doorways only, ADR-0002), so this sink cannot write one.
    """

    def __init__(self, db: Database) -> None:
        self.db = db

    async def insert_doorway_event(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a floor camera never writes a doorway event (ADR-0002)")

    async def open_gap(self, camera_id: str, from_utc: Any, cause: str) -> UUID:
        gap_id = uuid4()
        await self.db.execute(
            "insert into stream_gap (gap_id, camera_id, from_utc, cause) values (%s,%s,%s,%s)",
            (gap_id, camera_id, from_utc, cause),
        )
        return gap_id

    async def close_gap(self, gap_id: UUID, to_utc: Any) -> None:
        await self.db.execute(
            "update stream_gap set to_utc = %s where gap_id = %s and to_utc is null",
            (to_utc, gap_id),
        )


async def supervise_floor(runner: FloorRunner, *, backoff_s: float = 2.0) -> None:
    while True:
        try:
            await runner.run()
            return
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await runner.gap.stopped("crash")
            raise
        except Exception:
            log.exception("%s crashed; restarting in %.1fs", runner.name, backoff_s)
            await runner.gap.stopped("crash")
            await asyncio.sleep(backoff_s)
