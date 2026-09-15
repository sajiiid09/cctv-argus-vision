"""RTSP source: connect, decode, reconnect with backoff, stall detection, gaps.

Timestamps: the server clock is authoritative (``ts_server``); media time is
recorded alongside (``ts_media``) but never used in arithmetic
(ARCHITECTURE.md §5.8).

Gap semantics (THREAT_MODEL.md §3): a gap opens when a session that produced
frames ends without a clean stop, and closes when frames flow again. "We saw
nothing" and "nothing happened" must stay distinguishable downstream.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol
from uuid import UUID

import av
from argus.common.clock import Clock, SystemClock
from argus.common.config import CameraConfig, IngestConfig
from argus.ingest.ringbuffer import Frame, RingBuffer

log = logging.getLogger(__name__)


class GapRecorder(Protocol):
    async def open_gap(self, camera_id: str, from_utc: datetime, cause: str) -> UUID: ...
    async def close_gap(self, gap_id: UUID, to_utc: datetime) -> None: ...


@dataclass(slots=True)
class SourceStatus:
    camera_id: str
    state: str = "connecting"  # connecting | up | down | stopped
    frames: int = 0
    reconnects: int = 0
    last_frame_ts: datetime | None = None
    open_cause: str | None = field(default=None)

    def as_line(self) -> str:
        return (
            f"camera={self.camera_id} state={self.state} frames={self.frames} "
            f"reconnects={self.reconnects} last_frame="
            f"{self.last_frame_ts.isoformat() if self.last_frame_ts else 'never'}"
        )


def classify_failure(exc: BaseException, had_frames: bool) -> str:
    text = f"{type(exc).__name__}: {exc}".lower()
    name = type(exc).__name__
    # 'Immediate exit requested' is AVERROR_EXIT: PyAV's container-level idle
    # timeout (the `timeout=` kwarg) aborting a read that saw no data — the
    # stall case. av.error.TimeoutError is the RTSP socket timeout, same story.
    if (
        "timed out" in text
        or "timeout" in text
        or "temporarily unavailable" in text
        or "immediate exit" in text
        or "exit requested" in text
    ):
        return "stall" if had_frames else "offline"
    if any(
        s in text
        for s in (
            "connection",
            "refused",
            "reset",
            "eof",
            "unreachable",
            "404",
            "500",
            "no route",
            "broken pipe",
        )
    ):
        return "offline"
    if name in ("InvalidDataError",) or "invalid data" in text:
        return "decode_error"
    return "crash" if had_frames else "offline"


@dataclass(slots=True)
class _Stop(Exception):
    pass


class RTSPSource:
    def __init__(
        self,
        camera: CameraConfig,
        cfg: IngestConfig,
        clock: Clock | None = None,
        gaps: GapRecorder | None = None,
        queue_size: int = 120,
    ) -> None:
        self.camera = camera
        self.cfg = cfg
        self.clock = clock or SystemClock()
        self.gaps = gaps
        self.ring = RingBuffer(cfg.ring_buffer_seconds)
        self.status = SourceStatus(camera_id=camera.camera_id)
        self._subscribers: list[asyncio.Queue[Frame]] = []
        self._stopping = asyncio.Event()
        self._open_gap_id: UUID | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._last_frame_mono: float | None = None
        self._rng = random.Random()
        self._queue_size = queue_size

    # -- public API ---------------------------------------------------------

    def subscribe(self) -> asyncio.Queue[Frame]:
        q: asyncio.Queue[Frame] = asyncio.Queue(maxsize=self._queue_size)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[Frame]) -> None:
        if q in self._subscribers:
            self._subscribers.remove(q)

    def stop(self) -> None:
        self._stopping.set()

    async def run(self) -> None:
        backoff_s = self.cfg.reconnect.initial_s
        watchdog = asyncio.create_task(
            self._stall_watchdog(), name=f"stall-{self.camera.camera_id}"
        )
        try:
            backoff_s = await self._run_loop(backoff_s)
        finally:
            watchdog.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watchdog

    async def _stall_watchdog(self) -> None:
        """Deterministic stall detection, independent of ffmpeg/PyAV timeouts.

        RTCP keepalives can keep a blocked socket 'alive' indefinitely, so the
        socket timeout is unreliable as the sole tripwire. This watchdog looks
        at one fact only: how long since the last decoded FRAME — in ANY state
        (a reconnect can block inside a stalled open/read with state 'down').
        If the idle time exceeds stall_timeout_s, a stall gap opens right
        there; it closes on the next real frame. If some other cause (offline
        etc.) already opened a gap, the guard below keeps this one silent.
        """
        while True:
            await asyncio.sleep(min(0.5, self.cfg.stall_timeout_s / 4))
            if self._stopping.is_set() or self._last_frame_mono is None:
                continue
            idle = self.clock.monotonic() - self._last_frame_mono
            if idle >= self.cfg.stall_timeout_s and self._open_gap_id is None:
                log.warning(
                    "camera %s: stall — no frames for %.1fs (watchdog, state=%s)",
                    self.camera.camera_id,
                    idle,
                    self.status.state,
                )
                await self._handle_loss("stall", had_frames=True)

    async def _run_loop(self, backoff_s: float) -> float:
        while not self._stopping.is_set():
            try:
                had_frames = await self._session()
                if self._stopping.is_set():
                    break
                # clean EOF on a live camera is still a loss of measurement
                await self._handle_loss("offline", had_frames)
            except _Stop:
                break
            except Exception as exc:
                if self._stopping.is_set():
                    break
                had_frames = self.status.frames > 0
                cause = classify_failure(exc, had_frames)
                # Application-level stall rule: the socket tripwire fires at
                # half the stall timeout (RTCP keepalives can keep a socket
                # alive during a stall), but a read timeout only counts as a
                # stall once no FRAME has arrived for the full stall timeout.
                # The idle clock only moves on real frames, never on reconnects.
                idle = (
                    self.clock.monotonic() - self._last_frame_mono
                    if self._last_frame_mono is not None
                    else None
                )
                if (
                    had_frames
                    and idle is not None
                    and idle >= self.cfg.stall_timeout_s
                    and cause in ("stall", "crash")
                ):
                    # reconnect attempts during a stall meet refused/reset
                    # sessions (Errno 5 etc.); the honest cause is the stall
                    cause = "stall"
                if (
                    cause == "stall"
                    and had_frames
                    and (idle is None or idle < self.cfg.stall_timeout_s)
                ):
                    log.info(
                        "camera %s reconnecting after transient read error (idle=%s): %s",
                        self.camera.camera_id,
                        f"{idle:.1f}s" if idle is not None else "n/a",
                        exc,
                    )
                else:
                    log.warning(
                        "camera %s session failed (%s): %s", self.camera.camera_id, cause, exc
                    )
                    await self._handle_loss(cause, had_frames)
            if self._stopping.is_set():
                break
            self.status.state = "down"
            self.status.reconnects += 1
            delay = min(backoff_s, self.cfg.reconnect.max_s)
            backoff_s = min(backoff_s * 2, self.cfg.reconnect.max_s)
            jitter = delay * self.cfg.reconnect.jitter * (self._rng.random() - 0.5) * 2
            sleep_for = max(0.05, delay + jitter)
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=sleep_for)
                break
            except TimeoutError:
                continue
        self.status.state = "stopped"
        if self._open_gap_id is not None and self.gaps is not None:
            # planned stop closes the gap at the last frame we actually saw
            await self.gaps.close_gap(
                self._open_gap_id, self.status.last_frame_ts or self.clock.now_utc()
            )
            self._open_gap_id = None
            self.status.open_cause = None
        return backoff_s

    # -- internals ----------------------------------------------------------

    async def _handle_loss(self, cause: str, had_frames: bool) -> None:
        if self.status.last_frame_ts is None:
            # never saw a frame: nothing to bound a gap from; surface loudly instead
            log.error("camera %s: no frames ever received (cause=%s)", self.camera.camera_id, cause)
            self.status.open_cause = cause
            return
        if self._open_gap_id is None and self.gaps is not None:
            self._open_gap_id = await self.gaps.open_gap(
                self.camera.camera_id, self.status.last_frame_ts, cause
            )
            self.status.open_cause = cause
            log.warning(
                "camera %s: gap opened at %s cause=%s",
                self.camera.camera_id,
                self.status.last_frame_ts.isoformat(),
                cause,
            )

    async def _close_gap_if_open(self, now: datetime) -> None:
        if self._open_gap_id is not None and self.gaps is not None:
            await self._close_gap(self._open_gap_id, now)
        self._open_gap_id = None
        self.status.open_cause = None

    async def _close_gap(self, gap_id: UUID, now: datetime) -> None:
        if self.gaps is not None:
            await self.gaps.close_gap(gap_id, now)
        log.info("camera %s: gap closed at %s", self.camera.camera_id, now.isoformat())
        self.status.open_cause = None

    async def _session(self) -> bool:
        """One connection attempt. Returns True if any frame was decoded."""
        loop = asyncio.get_running_loop()
        stop_flag = self._stopping

        def reader() -> bool:
            return self._read_stream(loop, stop_flag)

        had = await asyncio.to_thread(reader)
        return had

    def _read_stream(self, loop: asyncio.AbstractEventLoop, stop_flag: asyncio.Event) -> bool:
        # Socket read timeout at HALF the stall timeout: RTCP keepalives can
        # keep a socket alive during a stall, so the socket tripwire fires
        # early and run()'s idle-since-last-frame rule decides what it meant
        # ('timeout' is the ffmpeg ≥5 RTSP option; it was 'stimeout' before).
        read_timeout_s = max(0.5, self.cfg.stall_timeout_s / 2)
        timeout_us = int(read_timeout_s * 1_000_000)
        options: dict[str, str] = {"rtsp_transport": "tcp", "timeout": str(timeout_us)}
        # decode mode 'nvidia' asks ffmpeg for NVDEC; if the box has no NVIDIA
        # decoder the open/read fails and the caller falls back on reconnect to
        # software below. UNVERIFIED until exercised on the staging box (M1 exit).
        if self.cfg.decode == "nvidia":
            options["hwaccel"] = "cuda"

        container = av.open(self.camera.source_uri, options=options, timeout=read_timeout_s)
        # NOTE: no idle reset here — the idle clock moves only on real frames
        try:
            video = container.streams.video[0]
            video.thread_type = "AUTO"
            had_frames = False
            for packet in container.demux(video):
                if stop_flag.is_set():
                    break
                for frame in packet.decode():
                    image = frame.to_ndarray(format="rgb24")
                    ts_server = self.clock.now_utc()
                    ts_media = float(frame.time) if frame.time is not None else None
                    f = Frame(ts_server=ts_server, ts_media=ts_media, image=image)
                    had_frames = True
                    self._last_frame_mono = self.clock.monotonic()
                    self.ring.append(f)
                    self.status.frames += 1
                    self.status.last_frame_ts = ts_server
                    loop.call_soon_threadsafe(self._fan_out, f)
            if stop_flag.is_set():
                raise _Stop()
            return had_frames
        finally:
            container.close()

    def _fan_out(self, frame: Frame) -> None:
        if self.status.state != "up":
            self.status.state = "up"
        if self._open_gap_id is not None:
            # capture-and-clear synchronously so a burst of frames schedules
            # exactly one close (close_gap is idempotent, but the log should
            # not lie about how many closes happened)
            gap_id = self._open_gap_id
            self._open_gap_id = None
            self._close_task = asyncio.create_task(self._close_gap(gap_id, frame.ts_server))
        for q in self._subscribers:
            if q.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    q.get_nowait()
            q.put_nowait(frame)

    async def wait_until_up(self, timeout: float = 20.0) -> bool:
        deadline = self.clock.monotonic() + timeout
        while self.status.state != "up":
            if self._stopping.is_set() or self.clock.monotonic() > deadline:
                return False
            await asyncio.sleep(0.05)
        return True
