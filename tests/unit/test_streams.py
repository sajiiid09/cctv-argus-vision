from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from argus.common.config import CameraConfig, IngestConfig
from argus.ingest.ringbuffer import Frame
from argus.ingest.streams import RTSPSource, classify_failure


class TestClassifyFailure:
    def test_timeout_with_frames_is_stall(self) -> None:
        assert classify_failure(TimeoutError("Operation timed out"), had_frames=True) == "stall"

    def test_timeout_without_frames_is_offline(self) -> None:
        assert classify_failure(TimeoutError("timed out"), had_frames=False) == "offline"

    def test_connection_refused(self) -> None:
        assert classify_failure(OSError("Connection refused"), True) == "offline"

    def test_eof(self) -> None:
        assert classify_failure(RuntimeError("End of file (EOF)"), True) == "offline"

    def test_invalid_data(self) -> None:
        class InvalidDataError(Exception):
            pass

        assert classify_failure(InvalidDataError(), True) == "decode_error"

    def test_unknown_is_crash_when_live(self) -> None:
        assert classify_failure(RuntimeError("weird"), True) == "crash"


class TestFanOut:
    async def test_drop_oldest(self) -> None:
        cam = CameraConfig(camera_id="t", role="floor", source_uri="rtsp://x")
        src = RTSPSource(cam, IngestConfig(), queue_size=2)
        q = src.subscribe()
        base = datetime(2026, 9, 16, 6, 0, tzinfo=UTC)
        for i in range(5):
            src._fan_out(Frame(base + timedelta(seconds=i), float(i), None))
        await asyncio.sleep(0)
        assert q.qsize() == 2
        assert src.status.state == "up"


class TestGapRecorderLifecycle:
    async def test_gap_opens_and_closes(self) -> None:
        class MemoryGaps:
            def __init__(self) -> None:
                self.opened: list[tuple[str, datetime, str]] = []
                self.closed: list[tuple[object, datetime]] = []

            async def open_gap(self, camera_id: str, from_utc: datetime, cause: str):
                self.opened.append((camera_id, from_utc, cause))
                return uuid4()

            async def close_gap(self, gap_id, to_utc: datetime) -> None:
                self.closed.append((gap_id, to_utc))

        gaps = MemoryGaps()
        cam = CameraConfig(camera_id="t", role="floor", source_uri="rtsp://x")
        src = RTSPSource(cam, IngestConfig(), gaps=gaps)
        last = datetime(2026, 9, 16, 6, 0, tzinfo=UTC)
        src.status.last_frame_ts = last

        await src._handle_loss("offline", had_frames=True)
        assert gaps.opened == [("t", last, "offline")]
        assert src._open_gap_id is not None

        resume = last + timedelta(seconds=5)
        await src._close_gap_if_open(resume)
        assert len(gaps.closed) == 1
        assert gaps.closed[0][1] == resume
        assert src._open_gap_id is None


@pytest.mark.parametrize("bad", [-1, 0])
def test_invalid_stall_timeout(bad: float) -> None:
    from argus.common.config import ConfigError

    with pytest.raises(ConfigError):
        IngestConfig(stall_timeout_s=bad)
