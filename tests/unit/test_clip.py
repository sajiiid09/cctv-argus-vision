"""Clip writers: remux and the decode+encode fallback.

These build packets from a tiny synthetic H.264 file rather than the rig, so
they run without docker or an ffmpeg binary. The rig tests cover the live RTSP
path; what is checked here is that both writers produce a file that actually
decodes -- the failure mode being a clip that opens and shows nothing.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import av
import numpy as np
import pytest
from argus.common.config import CameraConfig, IngestConfig
from argus.ingest.clip import ClipError, ClipStore, clip_path
from argus.ingest.ringbuffer import CodecParams, PacketRecord
from argus.ingest.streams import RTSPSource

BASE = datetime(2026, 9, 17, 8, 0, tzinfo=UTC)
FPS = 30
GOP = 10
N_FRAMES = 60


@pytest.fixture(scope="module")
def encoded(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A short H.264 file with a one-third-second GOP."""
    path = tmp_path_factory.mktemp("src") / "sample.mp4"
    container = av.open(str(path), mode="w", format="mp4")
    stream = container.add_stream("libx264", rate=FPS)
    stream.width, stream.height, stream.pix_fmt = 64, 48, "yuv420p"
    stream.gop_size = GOP
    stream.options = {"preset": "ultrafast", "g": str(GOP)}
    rng = np.random.default_rng(0)
    for i in range(N_FRAMES):
        img = np.full((48, 64, 3), i * 4 % 256, dtype=np.uint8)
        img[:8, :8] = rng.integers(0, 255, (8, 8, 3), dtype=np.uint8)
        frame = av.VideoFrame.from_ndarray(img, format="rgb24")
        frame.pts = i
        for packet in stream.encode(frame):
            container.mux(packet)
    for packet in stream.encode():
        container.mux(packet)
    container.close()
    return path


def _records(path: Path) -> tuple[CodecParams, list[PacketRecord]]:
    container = av.open(str(path))
    video = container.streams.video[0]
    ctx = video.codec_context
    params = CodecParams(
        codec_name=ctx.name,
        extradata=bytes(ctx.extradata) if ctx.extradata else None,
        width=video.width,
        height=video.height,
        time_base=video.time_base,
        average_rate=video.average_rate,
    )
    records = []
    for i, packet in enumerate(container.demux(video)):
        if packet.size == 0:
            continue
        records.append(
            PacketRecord(
                ts_server=BASE + timedelta(seconds=i / FPS),
                pts=packet.pts,
                dts=packet.dts,
                duration=packet.duration,
                is_keyframe=bool(packet.is_keyframe),
                data=bytes(packet),
            )
        )
    container.close()
    return params, records


def _source(cfg: IngestConfig | None = None) -> RTSPSource:
    camera = CameraConfig(
        camera_id="canteen_door_01",
        role="canteen_door",
        space_id="canteen",
        door_id="c1",
        source_uri="rtsp://localhost:8554/canteen_door_01",
        is_virtual=True,
    )
    return RTSPSource(camera, cfg or IngestConfig(ring_buffer_seconds=60.0))


def _decodable_frames(path: Path) -> int:
    container = av.open(str(path))
    n = sum(1 for _ in container.decode(video=0))
    container.close()
    return n


class TestClipPath:
    def test_layout_is_camera_day_range(self) -> None:
        path = clip_path(Path("/var/clips"), "gate_door", BASE, BASE + timedelta(seconds=5))
        assert path.parent == Path("/var/clips/gate_door/2026-09-17")
        assert path.name.endswith(".mp4")
        start_ms, end_ms = path.stem.split("_")
        assert int(end_ms) - int(start_ms) == 5000


class TestValidation:
    async def test_rejects_inverted_range(self) -> None:
        store = ClipStore(Path("/tmp/unused"))
        with pytest.raises(ClipError, match="not after start"):
            await store.extract(_source(), BASE, BASE - timedelta(seconds=1))

    async def test_rejects_span_beyond_max_clip_seconds(self) -> None:
        store = ClipStore(Path("/tmp/unused"))
        source = _source(IngestConfig(max_clip_seconds=5.0))
        with pytest.raises(ClipError, match="exceeds max_clip_seconds"):
            await store.extract(source, BASE, BASE + timedelta(seconds=30))

    async def test_range_older_than_the_buffer_is_loud(self, encoded: Path, tmp_path: Path) -> None:
        """A window predating the buffer must fail, not return a short clip.
        A truncated clip silently covering the wrong seconds is worse than an
        error, because it looks like evidence."""
        params, records = _records(encoded)
        source = _source()
        source.packets.set_params(params)
        for record in records:
            source.packets.append(record)

        store = ClipStore(tmp_path)
        stale = BASE - timedelta(seconds=30)
        with pytest.raises(ClipError, match="older than the buffer"):
            await store.extract(source, stale, stale + timedelta(seconds=2))

    async def test_empty_ring_raises_rather_than_writing_an_empty_file(
        self, tmp_path: Path
    ) -> None:
        store = ClipStore(tmp_path)
        source = _source()
        with pytest.raises(ClipError):
            await store.extract(source, BASE, BASE + timedelta(seconds=2))


class TestWriters:
    async def test_decode_encode_fallback_produces_a_playable_clip(
        self, encoded: Path, tmp_path: Path
    ) -> None:
        """No session open -> no template stream -> the fallback path runs."""
        params, records = _records(encoded)
        source = _source()
        source.packets.set_params(params)
        for record in records:
            source.packets.append(record)
        assert source.template_stream is None

        store = ClipStore(tmp_path)
        out = await store.extract(source, BASE, BASE + timedelta(seconds=1.0))
        assert out.exists()
        assert _decodable_frames(out) > 0

    async def test_remux_produces_a_playable_clip(self, encoded: Path, tmp_path: Path) -> None:
        """With a template stream the packets are copied, not re-encoded."""
        params, records = _records(encoded)
        source = _source()
        source.packets.set_params(params)
        for record in records:
            source.packets.append(record)

        container = av.open(str(encoded))
        try:
            source._template = container.streams.video[0]
            store = ClipStore(tmp_path)
            out = await store.extract(source, BASE, BASE + timedelta(seconds=1.0))
        finally:
            container.close()
        assert _decodable_frames(out) > 0

    async def test_clip_starts_at_a_keyframe(self, encoded: Path, tmp_path: Path) -> None:
        """Asking from mid-GOP must snap back, or the clip does not decode."""
        params, records = _records(encoded)
        source = _source()
        source.packets.set_params(params)
        for record in records:
            source.packets.append(record)

        mid_gop = BASE + timedelta(seconds=15 / FPS)  # frame 15, GOP is 10
        _, selected = source.packets.packets_between(mid_gop, mid_gop + timedelta(seconds=1))
        assert selected[0].is_keyframe

        store = ClipStore(tmp_path)
        out = await store.extract(source, mid_gop, mid_gop + timedelta(seconds=1))
        assert _decodable_frames(out) > 0
