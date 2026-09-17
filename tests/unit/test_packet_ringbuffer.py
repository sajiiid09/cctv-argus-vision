"""PacketRingBuffer: GOP-aligned eviction and keyframe-snapped extraction.

The properties under test are the ones that decide whether a clip plays at all.
A buffer that evicts by age alone eventually starts with a P-frame, and the
resulting file opens and shows nothing -- a failure that looks like a camera
problem and is not.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from fractions import Fraction

import pytest
from argus.ingest.ringbuffer import (
    CodecParams,
    PacketRecord,
    PacketRingBuffer,
    PacketRingError,
)

BASE = datetime(2026, 9, 16, 6, 0, tzinfo=UTC)
PARAMS = CodecParams(
    codec_name="h264",
    extradata=b"\x00\x00\x01",
    width=640,
    height=360,
    time_base=Fraction(1, 90000),
    average_rate=Fraction(30, 1),
)


def _pkt(i: int, *, keyframe: bool = False, size: int = 100) -> PacketRecord:
    """One packet, i frames in at 30 fps."""
    return PacketRecord(
        ts_server=BASE + timedelta(seconds=i / 30),
        pts=i * 3000,
        dts=i * 3000,
        duration=3000,
        is_keyframe=keyframe,
        data=b"\x00" * size,
    )


def _filled(gop: int = 30, count: int = 300, *, seconds: float = 5.0, max_bytes: int = 10_000_000):
    ring = PacketRingBuffer(seconds=seconds, max_bytes=max_bytes)
    ring.set_params(PARAMS)
    for i in range(count):
        ring.append(_pkt(i, keyframe=(i % gop == 0)))
    return ring


class TestEviction:
    def test_head_is_always_a_keyframe(self) -> None:
        ring = _filled()
        head = ring.earliest()
        assert head is not None and head.is_keyframe, (
            "a buffer whose head is a P-frame produces a clip that does not decode"
        )

    def test_retains_at_least_the_requested_duration(self) -> None:
        ring = _filled(gop=30, count=300, seconds=5.0)
        first, last = ring.earliest(), ring.latest()
        assert first is not None and last is not None
        span = (last.ts_server - first.ts_server).total_seconds()
        assert span >= 5.0

    def test_retains_at_most_one_extra_gop(self) -> None:
        """GOP alignment costs up to one GOP of slack. More than that is a bug,
        not a consequence -- it would silently multiply memory use."""
        ring = _filled(gop=30, count=300, seconds=5.0)
        first, last = ring.earliest(), ring.latest()
        assert first is not None and last is not None
        span = (last.ts_server - first.ts_server).total_seconds()
        assert span <= 5.0 + (30 / 30)

    def test_byte_cap_evicts_whole_gops(self) -> None:
        # 5 s at 30 fps x 100 bytes ~= 15 kB; cap well below that
        ring = _filled(gop=30, count=300, seconds=60.0, max_bytes=4_000)
        assert ring.nbytes() <= 4_000
        head = ring.earliest()
        assert head is not None and head.is_keyframe

    def test_single_oversized_gop_is_kept_rather_than_broken(self) -> None:
        """If one GOP alone exceeds the cap, keeping an undecodable fragment
        would be worse than keeping one oversized GOP. Exceeding the cap is
        visible in ring_bytes; a silently broken buffer is not."""
        ring = PacketRingBuffer(seconds=60.0, max_bytes=500)
        ring.set_params(PARAMS)
        for i in range(10):
            ring.append(_pkt(i, keyframe=(i == 0), size=200))
        assert ring.nbytes() > 500
        head = ring.earliest()
        assert head is not None and head.is_keyframe

    def test_nbytes_tracks_contents(self) -> None:
        ring = _filled(gop=30, count=90, seconds=60.0)
        assert ring.nbytes() == sum(len(p) for p in ring.snapshot())


class TestExtraction:
    def test_snaps_back_to_preceding_keyframe(self) -> None:
        ring = _filled(gop=30, count=120, seconds=60.0)
        # ask from frame 45, which sits mid-GOP; expect the GOP head at 30
        start = BASE + timedelta(seconds=45 / 30)
        end = BASE + timedelta(seconds=60 / 30)
        params, packets = ring.packets_between(start, end)
        assert params is PARAMS
        assert packets[0].is_keyframe
        assert packets[0].pts == 30 * 3000
        assert packets[-1].pts == 60 * 3000

    def test_window_older_than_the_buffer_raises(self) -> None:
        ring = _filled(gop=30, count=300, seconds=1.0)
        start = BASE - timedelta(seconds=10)
        with pytest.raises(PacketRingError, match="older than the buffer"):
            ring.packets_between(start, start + timedelta(seconds=1))

    def test_no_params_raises_rather_than_returning_unmuxable_packets(self) -> None:
        ring = PacketRingBuffer(seconds=5, max_bytes=1_000_000)
        ring.append(_pkt(0, keyframe=True))
        with pytest.raises(PacketRingError, match="codec parameters"):
            ring.packets_between(BASE, BASE + timedelta(seconds=1))

    def test_snapshot_is_a_copy(self) -> None:
        ring = _filled(gop=30, count=30, seconds=60.0)
        snap = ring.snapshot()
        n = len(ring)
        snap.clear()
        assert len(ring) == n


class TestParams:
    def test_resolution_change_drops_the_buffer(self) -> None:
        """TESTING.md §4 lists mid-stream resolution change as a fault to inject.
        Muxing packets of two sizes into one stream yields a corrupt file, so the
        old packets go."""
        ring = _filled(gop=30, count=60, seconds=60.0)
        assert len(ring) > 0
        ring.set_params(
            CodecParams(
                codec_name="h264",
                extradata=b"\x00\x00\x01",
                width=1280,
                height=720,
                time_base=Fraction(1, 90000),
                average_rate=Fraction(30, 1),
            )
        )
        assert len(ring) == 0

    def test_identical_params_do_not_drop_the_buffer(self) -> None:
        ring = _filled(gop=30, count=60, seconds=60.0)
        n = len(ring)
        ring.set_params(PARAMS)
        assert len(ring) == n


class TestValidation:
    @pytest.mark.parametrize("seconds,max_bytes", [(0, 100), (-1, 100), (5, 0), (5, -1)])
    def test_rejects_nonpositive_bounds(self, seconds: float, max_bytes: int) -> None:
        with pytest.raises(ValueError):
            PacketRingBuffer(seconds=seconds, max_bytes=max_bytes)
