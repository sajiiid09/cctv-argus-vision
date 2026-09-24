"""The seams every pipeline shares: frames in, evidence out, gaps when neither.

The Protocols here are satisfied *structurally* by ``argus.ingest.RTSPSource``,
``argus.ingest.ClipStore`` and ``argus.store.Store``. That is deliberate: a
library importing a service would invert the layering, and the wiring belongs in
the service that already owns both ends (ADR-0033).

The important thing in this module is not the types. It is ``GapKeeper``: a
pipeline that stops analysing **opens a stream_gap**. "We saw nothing" and
"nothing happened" must stay different facts all the way down to payroll, which
zeroes a day that overlaps a gap -- so an analyser that dies quietly would turn
a measurement failure into a clean day.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

import numpy as np
from argus.common.clock import Clock
from argus.common.config import CameraConfig

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class FrameRef:
    """One decoded frame.

    Field names match ``argus.ingest.ringbuffer.Frame`` exactly so that an
    RTSPSource queue can be consumed with no adapter; a unit test asserts the
    two stay in step.

    ``ts_server`` is authoritative and the only value used in arithmetic.
    ``ts_media`` exists for rig alignment and audit -- never for a calculation
    that reaches a number someone is paid by (ARCHITECTURE.md §7.1).
    """

    ts_server: datetime
    ts_media: float | None
    image: np.ndarray


class FrameSource(Protocol):
    camera: CameraConfig

    def subscribe(self) -> Any: ...

    def unsubscribe(self, q: Any) -> None: ...


class ClipWriter(Protocol):
    # The root every clip path is relative to. Stored rows must be relative --
    # an absolute path is unusable on another machine and the schema refuses it
    # anyway -- so the writer has to say where its root is rather than the
    # caller inferring it from the shape of a path.
    root: Path

    async def extract(
        self, source: Any, start_utc: datetime, end_utc: datetime, *, timeout_s: float = ...
    ) -> Path: ...


class GapSink(Protocol):
    """Just the gap half of the store.

    Narrower than EventSink on purpose: a floor camera records gaps and, by
    ADR-0002, must never write a doorway event. Asking it for the whole event
    interface would mean giving it a method it is not allowed to call.
    """

    async def open_gap(self, camera_id: str, from_utc: datetime, cause: str) -> UUID: ...

    async def close_gap(self, gap_id: UUID, to_utc: datetime) -> None: ...


class EventSink(GapSink, Protocol):
    """What a doorway pipeline needs from the store, spelled out.

    The keyword arguments are named rather than **kwargs so that a pipeline
    passing a field the store does not have is a type error here, not a
    TypeError at the moment a doorway event is written.
    """

    async def insert_doorway_event(
        self,
        camera_id: str,
        door_id: str | None,
        ts_utc: datetime,
        direction: str,
        *,
        ts_camera_reported: datetime | None = ...,
        person_id: str | None = ...,
        match_confidence: float | None = ...,
        detection_quality: float | None = ...,
        clip_ref: str | None = ...,
        ingest_run_id: UUID | None = ...,
        duplicate_of: UUID | None = ...,
    ) -> Any: ...

    async def insert_clip(
        self,
        camera_id: str,
        rel_path: str,
        start_utc: datetime,
        end_utc: datetime,
        *,
        keyframe_utc: datetime | None = ...,
        is_virtual: bool = ...,
        size_bytes: int | None = ...,
    ) -> UUID: ...


class GapKeeper:
    """Opens a gap while a pipeline is not measuring; closes it when it is.

    Idempotent in both directions, because the callers are a supervisor loop and
    an exception handler, and both can fire twice. The gap's cause is the
    pipeline's best account of *why* it stopped: ``crash`` for an exception,
    ``aim_changed`` when the camera moved off its preset (ADR-0029),
    ``overload`` when the analyser could not keep up (ADR-0033). Those are
    different operational facts even though payroll treats them identically.
    """

    def __init__(self, sink: GapSink, camera_id: str, clock: Clock) -> None:
        self._sink = sink
        self._camera_id = camera_id
        self._clock = clock
        self._gap_id: UUID | None = None
        self._cause: str | None = None

    @property
    def open_cause(self) -> str | None:
        return self._cause

    async def stopped(self, cause: str) -> None:
        if self._gap_id is not None:
            return
        self._gap_id = await self._sink.open_gap(self._camera_id, self._clock.now_utc(), cause)
        self._cause = cause
        log.warning("%s: analysis stopped, gap opened cause=%s", self._camera_id, cause)

    async def measuring(self) -> None:
        if self._gap_id is None:
            return
        gap_id, cause = self._gap_id, self._cause
        self._gap_id = None
        self._cause = None
        await self._sink.close_gap(gap_id, self._clock.now_utc())
        log.info("%s: analysis resumed, gap closed (was %s)", self._camera_id, cause)


@asynccontextmanager
async def measuring(gap: GapKeeper, *, cause: str = "crash") -> AsyncIterator[GapKeeper]:
    """Run a pipeline; if it raises, record that we stopped seeing.

    The gap is *not* closed on entry -- claiming measurement before a frame has
    been processed would paper over a pipeline that starts and immediately
    fails. The pipeline calls ``gap.measuring()`` itself once it has handled a
    frame.
    """
    try:
        yield gap
    except Exception:
        # asyncio.CancelledError is a BaseException on Python 3.12.  A deliberate
        # service shutdown is not a measurement failure and must not manufacture
        # a crash gap that payroll later has to treat as a bad camera window.
        await gap.stopped(cause)
        raise
