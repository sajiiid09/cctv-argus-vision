"""Wiring the analysis pipelines into the ingest process (ADR-0033).

This module is the only place that knows about both ends. `argus.pipelines`
declares Protocols; `RTSPSource`, `ClipStore` and `Store` satisfy them
structurally; nothing in the library imports the service.

Two rules encoded here:

* **Off by default.** `pipelines.enabled: false` means a box with no model
  artefacts runs ingest exactly as it did before, and CI's smoke test is
  unaffected. The system makes no claim to be measuring.
* **On, but broken, is a gap.** If pipelines are enabled and a backend cannot be
  constructed, the affected cameras get a `crash` gap rather than silence,
  because the configuration said this camera was being watched.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any
from uuid import UUID

from argus.backends.interfaces import Detector
from argus.backends.registry import get_detector, get_pose_estimator
from argus.common.clock import Clock
from argus.common.config import AppConfig, CameraConfig
from argus.ingest.clip import ClipStore
from argus.ingest.floor import FloorRunner
from argus.ingest.streams import RTSPSource
from argus.pipelines.aim import AimMonitor
from argus.pipelines.base import GapKeeper, measuring
from argus.pipelines.canteen import CanteenPipeline
from argus.pipelines.faces import FaceStage
from argus.pipelines.metrics import InMemoryMetrics
from argus.pipelines.runtime import SessionRuntime
from argus.store.store import Store

log = logging.getLogger(__name__)


def build_canteen_pipelines(
    config: AppConfig,
    sources: list[RTSPSource],
    store: Store,
    clips: ClipStore,
    clock: Clock,
    ingest_run_id: UUID,
    metrics: InMemoryMetrics,
) -> tuple[list[CanteenPipeline], list[SessionRuntime[Detector]]]:
    """One pipeline per canteen door that has a door line configured."""
    if not config.pipelines.enabled:
        log.info("pipelines disabled; ingest is recording, not analysing")
        return [], []

    faces = _face_stage(config)
    runtimes: list[SessionRuntime[Detector]] = []
    pipelines: list[CanteenPipeline] = []
    for source in sources:
        camera = source.camera
        if camera.role != "canteen_door":
            continue
        if camera.door_line is None:
            log.warning(
                "%s is a canteen door with no door_line: not analysed. "
                "A pipeline without a line has nothing to cross",
                camera.camera_id,
            )
            continue
        runtime: SessionRuntime[Detector] = SessionRuntime(
            f"detector-{camera.camera_id}",
            lambda: get_detector(config.pipelines.detector_backend),
            max_queue=config.pipelines.queue_depth,
        )
        runtimes.append(runtime)
        pipelines.append(
            CanteenPipeline(
                camera,
                source,
                store,
                detector=runtime,
                clips=clips,
                faces=faces,
                aim=_aim_monitor(camera),
                cfg=config.pipelines.canteen,
                clock=clock,
                ingest_run_id=ingest_run_id,
                metrics=metrics,
            )
        )
    log.info("analysing %d canteen door(s) in this process (ADR-0033)", len(pipelines))
    return pipelines, runtimes


def _face_stage(config: AppConfig) -> FaceStage | None:
    """Faces are opt-in and, for now, always off.

    Enrolment exists (`services/enrol`); what does not exist is a measured
    threshold and the wiring between the two. Until both land every crossing is
    `unknown`, which is the fail-open outcome and not a missing feature
    (ADR-0010).
    """
    if not config.face.enabled:
        return None
    raise NotImplementedError(
        "face.enabled is true, but the canteen pipeline has no face stage wired "
        "to it yet. Two things are missing, in this order: a canteen 1:N "
        "threshold measured from real faces (ADR-0010 forbids a default), and "
        "the code here that builds a FaceStage from the registry's face "
        "detector and embedder and hands it the enrolled templates. "
        "`argus-enrol` already produces those templates. Until then run with "
        "face.enabled false: every crossing stays unknown, which charges nobody."
    )


def _aim_monitor(camera: CameraConfig) -> AimMonitor | None:
    """ADR-0029's drift check needs an accepted reference frame first.

    Until `camera_reference_frame` has a row for this camera, there is nothing
    to compare against -- and inventing one from the first frame the process
    happens to see would accept whatever the camera is pointing at today as
    correct, which is the opposite of the check.
    """
    return None


async def supervise(pipeline: CanteenPipeline, *, backoff_s: float = 2.0) -> None:
    """Run a pipeline forever, recording a gap whenever it is not running."""
    gap = GapKeeper(pipeline.events, pipeline.camera.camera_id, pipeline.clock)
    while True:
        try:
            async with measuring(gap):
                await pipeline.run()
            return  # clean stop
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await gap.stopped("crash")
            raise
        except Exception:
            log.exception("%s crashed; restarting in %.1fs", pipeline.name, backoff_s)
            await asyncio.sleep(backoff_s)


def build_floor_pipelines(
    config: AppConfig,
    sources: list[RTSPSource],
    store: Store,
    clips: ClipStore,
    clock: Clock,
    metrics: InMemoryMetrics,
) -> tuple[list[FloorRunner], list[SessionRuntime[Detector]]]:
    """Occupancy and the violence trigger, on the floor cameras.

    Both are off unless configured, and occupancy additionally needs seats: a
    floor camera with no seat regions has nothing to sample, and inventing them
    from the frame would be a guess about where people sit.
    """
    occupancy_cfg = config.pipelines.occupancy
    violence_cfg = config.pipelines.violence
    if not config.pipelines.enabled or not (occupancy_cfg.enabled or violence_cfg.enabled):
        return [], []

    runtimes: list[SessionRuntime[Detector]] = []
    runners: list[FloorRunner] = []
    for source in sources:
        camera = source.camera
        if camera.role != "floor":
            continue
        detector: SessionRuntime[Detector] | None = None
        pose: SessionRuntime[Any] | None = None
        if occupancy_cfg.enabled and occupancy_cfg.seats:
            detector = SessionRuntime(
                f"detector-{camera.camera_id}",
                lambda: get_detector(config.pipelines.detector_backend),
                max_queue=config.pipelines.queue_depth,
            )
            runtimes.append(detector)
        elif occupancy_cfg.enabled:
            log.warning(
                "%s: occupancy is enabled but no seats are configured, so nothing is "
                "sampled. Seat regions are configuration (PLAN.md M5)",
                camera.camera_id,
            )
        if violence_cfg.enabled:
            pose = SessionRuntime(
                f"pose-{camera.camera_id}",
                lambda: get_pose_estimator(config.pipelines.pose_backend),
                max_queue=config.pipelines.queue_depth,
            )
            runtimes.append(pose)
        runners.append(
            FloorRunner(
                camera,
                source,
                store.db,
                detector=detector,
                pose=pose,
                occupancy_cfg=occupancy_cfg,
                violence_cfg=violence_cfg,
                clips=clips,
                clock=clock,
                metrics=metrics,
            )
        )
    if runners:
        log.info(
            "watching %d floor camera(s): occupancy=%s violence-trigger=%s",
            len(runners),
            occupancy_cfg.enabled,
            violence_cfg.enabled,
        )
    return runners, runtimes
