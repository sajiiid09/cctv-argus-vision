from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
from argus.ingest.ringbuffer import Frame, RingBuffer


def _frame_at(i: int, shape: tuple[int, int] = (2, 3)) -> Frame:
    base = datetime(2026, 9, 16, 6, 0, tzinfo=UTC)
    return Frame(
        ts_server=base + timedelta(seconds=i),
        ts_media=float(i) / 30,
        image=np.zeros((*shape, 3), dtype=np.uint8),
    )


class TestRingBuffer:
    def test_bounds_by_duration(self) -> None:
        ring = RingBuffer(seconds=5)
        for i in range(20):
            ring.append(_frame_at(i))
        assert len(ring) == 6  # frames 14..19 (t=14..19, cutoff 19-5=14)
        assert ring.earliest().ts_media == 14 / 30
        assert ring.latest().ts_media == 19 / 30

    def test_frames_between_inclusive(self) -> None:
        ring = RingBuffer(seconds=60)
        for i in range(10):
            ring.append(_frame_at(i))
        base = datetime(2026, 9, 16, 6, 0, tzinfo=UTC)
        out = ring.frames_between(base + timedelta(seconds=3), base + timedelta(seconds=6))
        assert [f.ts_media for f in out] == [3 / 30, 4 / 30, 5 / 30, 6 / 30]

    def test_snapshot_is_copy(self) -> None:
        ring = RingBuffer(seconds=10)
        ring.append(_frame_at(0))
        snap = ring.snapshot()
        snap.clear()
        assert len(ring) == 1

    def test_rejects_nonpositive(self) -> None:
        import pytest

        with pytest.raises(ValueError):
            RingBuffer(seconds=0)
