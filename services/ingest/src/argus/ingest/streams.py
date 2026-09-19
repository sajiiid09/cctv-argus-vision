"""RTSP source: connect, decode, reconnect with backoff, stall detection, gaps.

Timestamps: the server clock is authoritative (``ts_server``); media time is
recorded alongside (``ts_media``) but never used in arithmetic
(ARCHITECTURE.md §5.8).

Gap semantics (RISKS.md §6): a gap opens when a session that produced
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
from typing import Any, Protocol
from uuid import UUID

import av
from argus.common.clock import Clock, SystemClock
from argus.common.config import CameraConfig, IngestConfig
from argus.ingest.ringbuffer import (
    CodecParams,
    Frame,
    PacketRecord,
    PacketRingBuffer,
    RingBuffer,
)
from av.codec.hwaccel import HWAccel, hwdevices_available

log = logging.getLogger(__name__)


class GapRecorder(Protocol):
    async def open_gap(self, camera_id: str, from_utc: datetime, cause: str) -> UUID: ...
    async def close_gap(self, gap_id: UUID, to_utc: datetime) -> None: ...


@dataclass(slots=True)
class SourceStatus:
    camera_id: str
    state: str = "connecting"  # connecting | up | down | stopped
    frames: int = 0  # DECODED frames emitted to pipelines (sampled, not demuxed)
    packets: int = 0  # demuxed packets buffered as evidence
    ring_bytes: int = 0  # packet ring occupancy — a leak shows here, not in dmesg
    reconnects: int = 0
    last_frame_ts: datetime | None = None
    open_cause: str | None = field(default=None)

    def as_line(self) -> str:
        return (
            f"camera={self.camera_id} state={self.state} frames={self.frames} "
            f"packets={self.packets} ring_bytes={self.ring_bytes} "
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


# decode mode -> the libav hardware device that serves it. `software` is not in
# here because it is the absence of one.
HWACCEL_DEVICE = {"nvidia": "cuda"}


def hwaccel_device(decode: str, available: list[str] | None = None) -> str | None:
    """The hardware device to decode with, or None for software.

    Hardware decode lives or dies on how *this* ffmpeg was built, never on what
    the box contains: a pip-installed PyAV wheel commonly ships without NVDEC
    even on a machine with four GPUs (ARCHITECTURE.md §5.5). So the question
    asked here is `hwdevices_available()` -- the devices this libav was compiled
    with -- and the answer is decided once, at startup, rather than being
    guessed per reconnect.

    Returning None for a configured accelerator is a fallback, and the caller
    says so at ERROR level. Silence would leave `decode: nvidia` in the config
    describing something that never happened.
    """
    device = HWACCEL_DEVICE.get(decode)
    if device is None:
        return None
    devices = hwdevices_available() if available is None else available
    if device not in devices:
        return None
    return device


def _codec_params(video: Any) -> CodecParams:
    """Snapshot what a muxer needs, at session open.

    ``extradata`` (SPS/PPS) is the part that is easy to forget and impossible to
    recover later: without it a remuxed file opens and shows nothing.
    """
    ctx = video.codec_context
    return CodecParams(
        codec_name=ctx.name,
        extradata=bytes(ctx.extradata) if ctx.extradata else None,
        width=int(video.width or 0),
        height=int(video.height or 0),
        time_base=video.time_base,
        average_rate=video.average_rate,
    )


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
        # Evidence path: encoded packets, the full pre-trigger window (ADR-0031).
        self.packets = PacketRingBuffer(cfg.ring_buffer_seconds, cfg.ring_buffer_bytes)
        # Analysis path: decoded frames, deliberately short and sampled.
        self.ring = RingBuffer(cfg.analysis_buffer_seconds)
        self.status = SourceStatus(camera_id=camera.camera_id)
        self._analysis_fps = cfg.fps_for(camera)
        self._last_decode_mono: float | None = None
        # Decided once per process, not per reconnect: a device that is absent
        # from this build will be absent from the next attempt too, and a
        # per-attempt probe would print the same error every backoff.
        self._hwaccel_device = hwaccel_device(cfg.decode)
        if cfg.decode != "software" and self._hwaccel_device is None:
            log.error(
                "camera %s: ingest.decode=%r but this libav build offers %s -- "
                "decoding in SOFTWARE. Hardware decode needs an ffmpeg/PyAV built "
                "with that device (ARCHITECTURE.md §5.5); the pip wheel usually is "
                'not. Check with: python -c "from av.codec.hwaccel import '
                'hwdevices_available as h; print(h())"',
                camera.camera_id,
                cfg.decode,
                hwdevices_available() or "no hardware devices",
            )
        elif self._hwaccel_device is not None:
            log.info("camera %s: hardware decode via %s", camera.camera_id, self._hwaccel_device)
        self._subscribers: list[asyncio.Queue[Frame]] = []
        self._packet_subscribers: list[asyncio.Queue[PacketRecord]] = []
        # The live input stream, kept only while a session is open. Remuxing a
        # clip needs a template stream: measured on PyAV 18, `add_mux_stream`
        # and a rebuilt encoder stream are both rejected by the mp4 muxer
        # (avformat_write_header, EINVAL), because neither carries usable
        # extradata. `add_stream_from_template` works. When no session is open
        # the clip store decodes and re-encodes instead.
        self._template: Any | None = None
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

    @property
    def template_stream(self) -> Any | None:
        """Input stream of the open session, or None while reconnecting."""
        return self._template

    def subscribe_packets(self) -> asyncio.Queue[PacketRecord]:
        """Live encoded packets, for a clip whose end is in the future."""
        q: asyncio.Queue[PacketRecord] = asyncio.Queue(maxsize=self._queue_size)
        self._packet_subscribers.append(q)
        return q

    def unsubscribe_packets(self, q: asyncio.Queue[PacketRecord]) -> None:
        if q in self._packet_subscribers:
            self._packet_subscribers.remove(q)

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
        # `hwaccel` is an ffmpeg COMMAND-LINE flag, not an AVOption: passing it in
        # `options` is accepted and silently dropped, which is how a box can spend
        # a week believing it decodes on the GPU. PyAV 18 takes a real HWAccel
        # object instead, and `allow_software_fallback` keeps one unsupported
        # stream from killing the camera. UNVERIFIED against NVDEC until the
        # NVIDIA box runs it (M1 exit).
        hwaccel = (
            HWAccel(device_type=self._hwaccel_device, allow_software_fallback=True)
            if self._hwaccel_device is not None
            else None
        )

        try:
            container = av.open(
                self.camera.source_uri,
                options=options,
                timeout=read_timeout_s,
                hwaccel=hwaccel,
            )
        except Exception:
            if hwaccel is None:
                raise
            # The build has the device; this machine could not initialise it (no
            # driver, no free GPU memory, a codec the device does not cover).
            # Downgrade once, permanently, rather than failing this attempt and
            # every reconnect after it with the same error.
            log.exception(
                "camera %s: opening with %s hardware decode failed -- falling back to "
                "SOFTWARE decode for the rest of this process",
                self.camera.camera_id,
                self._hwaccel_device,
            )
            self._hwaccel_device = None
            container = av.open(self.camera.source_uri, options=options, timeout=read_timeout_s)
        # NOTE: no idle reset here — the idle clock moves only on real frames
        try:
            video = container.streams.video[0]
            video.thread_type = "AUTO"
            self.packets.set_params(_codec_params(video))
            self._template = video
            had_frames = False
            interval = 1.0 / self._analysis_fps
            for packet in container.demux(video):
                if stop_flag.is_set():
                    break
                if packet.size == 0:
                    continue  # PyAV's end-of-demux flush packet carries no data
                now_ts = self.clock.now_utc()
                self.packets.append(
                    PacketRecord(
                        ts_server=now_ts,
                        pts=packet.pts,
                        dts=packet.dts,
                        duration=packet.duration,
                        is_keyframe=bool(packet.is_keyframe),
                        data=bytes(packet),
                    )
                )
                self.status.packets += 1
                self.status.ring_bytes = self.packets.nbytes()
                loop.call_soon_threadsafe(self._fan_out_packet, self.packets.latest())

                # Every packet is decoded — skipping one breaks the reference
                # chain for every frame that depends on it. What is throttled is
                # the rgb24 conversion and retention, which is the part that costs
                # memory and most of the CPU.
                mono = self.clock.monotonic()
                due = self._last_decode_mono is None or (mono - self._last_decode_mono) >= interval
                for frame in packet.decode():
                    had_frames = True
                    self._last_frame_mono = self.clock.monotonic()
                    ts_server = self.clock.now_utc()
                    self.status.last_frame_ts = ts_server
                    if not due:
                        continue
                    self._last_decode_mono = mono
                    due = False
                    image = frame.to_ndarray(format="rgb24")
                    ts_media = float(frame.time) if frame.time is not None else None
                    f = Frame(ts_server=ts_server, ts_media=ts_media, image=image)
                    self.ring.append(f)
                    self.status.frames += 1
                    loop.call_soon_threadsafe(self._fan_out, f)
            if stop_flag.is_set():
                raise _Stop()
            return had_frames
        finally:
            self._template = None
            container.close()

    def _fan_out_packet(self, packet: PacketRecord | None) -> None:
        if packet is None:
            return
        for q in self._packet_subscribers:
            if q.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    q.get_nowait()
            q.put_nowait(packet)

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
