"""Clip store: video segments addressed by (camera, time range).

Every payroll-affecting record points into this store (ADR-0008); the ingest
side only has to make clips addressable and playable.

Clips are built from the packet ring (ADR-0031), by two routes:

**Remux** -- copy the encoded packets into a container without decoding. Fast,
and the result is bit-identical to what the camera sent, which is what
auditability actually wants. It needs a *template* stream to copy codec
parameters from. Measured on PyAV 18: ``add_mux_stream`` produces a stream with
no codec context, and rebuilding an encoder stream and assigning extradata is
rejected too -- both fail in ``avformat_write_header`` with EINVAL. Only
``add_stream_from_template`` works, and the only template available is the input
stream of an open session.

**Decode and re-encode** -- the fallback for when no session is open (the camera
is down, or reconnecting). A standalone decoder is built from the codec
parameters stored alongside the packets, so this works from the buffer alone.
Slower and lossy, but it never depends on the camera being reachable.

Neither route ever materialises a list of decoded frames: 30 s of 2304x1296
rgb24 is 6.75 GB, and the point of the packet ring was to stop holding that.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import av
from argus.ingest.ringbuffer import CodecParams, PacketRecord, PacketRingError
from argus.ingest.streams import RTSPSource

log = logging.getLogger(__name__)

ENCODE_FALLBACK_FPS = 30


class ClipError(Exception):
    pass


def clip_path(root: Path, camera_id: str, start_utc: datetime, end_utc: datetime) -> Path:
    day = start_utc.astimezone(UTC).strftime("%Y-%m-%d")
    start_ms = int(start_utc.timestamp() * 1000)
    end_ms = int(end_utc.timestamp() * 1000)
    return root / camera_id / day / f"{start_ms}_{end_ms}.mp4"


class ClipStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    async def extract(
        self,
        source: RTSPSource,
        start_utc: datetime,
        end_utc: datetime,
        *,
        timeout_s: float = 10.0,
    ) -> Path:
        """Extract [start, end] from the source's packet ring.

        The clip begins at the last keyframe at or before ``start``, so it may
        start slightly earlier than asked -- harmless for a pre-trigger clip, and
        the only way the result decodes. A range older than the buffer is a
        ClipError: loud, not a silently truncated clip.
        """
        if end_utc <= start_utc:
            raise ClipError(f"clip end {end_utc.isoformat()} is not after start")
        span = (end_utc - start_utc).total_seconds()
        if span > source.cfg.max_clip_seconds:
            raise ClipError(
                f"clip span {span:.1f}s exceeds max_clip_seconds "
                f"({source.cfg.max_clip_seconds}); a longer range would mean "
                "decoding gigabytes on the fallback path"
            )

        if end_utc > datetime.now(UTC):
            await self._await_live(source, end_utc, timeout_s)

        try:
            params, packets = source.packets.packets_between(start_utc, end_utc)
        except PacketRingError as e:
            raise ClipError(f"{source.camera.camera_id}: {e}") from e
        if not packets:
            raise ClipError(
                f"no packets for {source.camera.camera_id} in "
                f"[{start_utc.isoformat()}, {end_utc.isoformat()}]"
            )

        path = clip_path(
            self.root, source.camera.camera_id, packets[0].ts_server, packets[-1].ts_server
        )
        path.parent.mkdir(parents=True, exist_ok=True)

        template = source.template_stream
        if template is not None:
            try:
                await asyncio.to_thread(self._remux, path, template, packets)
                return path
            except Exception as e:  # any muxer failure falls back to decoding
                log.warning(
                    "camera %s: remux failed (%s); falling back to decode+encode",
                    source.camera.camera_id,
                    e,
                )
        await asyncio.to_thread(self._decode_encode, path, params, packets)
        return path

    async def _await_live(self, source: RTSPSource, end_utc: datetime, timeout_s: float) -> None:
        """Wait until packets covering ``end_utc`` have arrived.

        Packets go into the ring as they are demuxed, so there is nothing to
        collect here -- only to wait for. Reading the ring afterwards avoids
        merging two views of the same data and getting the order subtly wrong.
        """
        q = source.subscribe_packets()
        try:

            async def _drain() -> None:
                while True:
                    packet = await q.get()
                    if packet.ts_server >= end_utc:
                        return

            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(_drain(), timeout=timeout_s)
        finally:
            source.unsubscribe_packets(q)

    # -- writers ------------------------------------------------------------

    def _remux(self, path: Path, template: Any, packets: list[PacketRecord]) -> None:
        container = av.open(str(path), mode="w")
        try:
            stream = container.add_stream_from_template(template)
            for packet in _rebased(packets, stream):
                container.mux(packet)
        finally:
            container.close()

    def _decode_encode(self, path: Path, params: CodecParams, packets: list[PacketRecord]) -> None:
        if params.extradata is None:
            raise ClipError(
                "no codec extradata stored for this stream; cannot decode the "
                "buffered packets (SPS/PPS is not recoverable after the fact)"
            )
        decoder = av.CodecContext.create(params.codec_name, "r")
        decoder.extradata = params.extradata

        container = av.open(str(path), mode="w", format="mp4")
        try:
            stream = container.add_stream("libx264", rate=ENCODE_FALLBACK_FPS)
            stream.width = params.width
            stream.height = params.height
            stream.pix_fmt = "yuv420p"
            for pts, frame in enumerate(_decoded(decoder, packets)):
                frame.pts = pts
                for encoded in stream.encode(frame):
                    container.mux(encoded)
            for encoded in stream.encode():
                container.mux(encoded)
        finally:
            container.close()


def _rebased(packets: list[PacketRecord], stream: Any) -> Iterator[Any]:
    """Packets with timestamps rebased to start at zero.

    pts/dts are preserved and shifted, never derived from wallclock: two frames
    can share a wallclock millisecond, and B-frames make dts differ from pts, so
    a wallclock-derived pts produces jittery or unmuxable output.
    """
    base_pts = packets[0].pts or 0
    base_dts = packets[0].dts if packets[0].dts is not None else base_pts
    for record in packets:
        packet = av.Packet(record.data)
        packet.stream = stream
        if record.pts is not None:
            packet.pts = record.pts - base_pts
        if record.dts is not None:
            packet.dts = record.dts - base_dts
        if record.duration is not None:
            packet.duration = record.duration
        yield packet


def _decoded(decoder: Any, packets: list[PacketRecord]) -> Iterator[Any]:
    """Decode packets one at a time, yielding frames as they emerge.

    A generator, not a list: holding every decoded frame is the thing that makes
    a long clip an out-of-memory error rather than a slow one.
    """
    for record in packets:
        for frame in decoder.decode(av.Packet(record.data)):
            yield frame
    for frame in decoder.decode(None):  # flush
        yield frame
