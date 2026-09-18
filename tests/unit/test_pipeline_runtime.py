"""SessionRuntime and GapKeeper: what happens when analysis cannot keep up.

The queueing policy is a payroll decision wearing a performance costume. A
pipeline that quietly analyses one frame in ten is claiming to measure something
it is not, so drops are counted and a sustained drop rate opens a gap.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from argus.common.clock import FixedClock
from argus.pipelines.base import FrameRef, GapKeeper, measuring
from argus.pipelines.runtime import Dropped, SessionRuntime

T0 = datetime(2026, 9, 18, 7, 0, tzinfo=UTC)


class _Backend:
    model_ref = "fake@000000000000"

    def __init__(self) -> None:
        self.calls = 0

    def work(self, value: int) -> int:
        self.calls += 1
        return value * 2


class _RecordingSink:
    def __init__(self) -> None:
        self.opened: list[tuple[str, str]] = []
        self.closed: list[UUID] = []

    async def insert_doorway_event(self, *args, **kwargs):  # pragma: no cover - unused here
        raise AssertionError("not used")

    async def open_gap(self, camera_id: str, from_utc: datetime, cause: str) -> UUID:
        self.opened.append((camera_id, cause))
        return uuid4()

    async def close_gap(self, gap_id: UUID, to_utc: datetime) -> None:
        self.closed.append(gap_id)


async def test_submitted_work_runs_on_the_session_thread() -> None:
    runtime: SessionRuntime[_Backend] = SessionRuntime("test", _Backend)
    try:
        assert await runtime.submit(lambda b: b.work(21)) == 42
        assert runtime.model_ref == "fake@000000000000"
        assert runtime.stats().completed == 1
    finally:
        await runtime.aclose()


async def test_construction_failure_surfaces_at_startup_not_on_the_first_frame() -> None:
    def explode() -> _Backend:
        raise RuntimeError("artefact not fetched")

    runtime: SessionRuntime[_Backend] = SessionRuntime("boom", explode)
    try:
        with pytest.raises(RuntimeError, match="artefact not fetched"):
            await runtime.wait_ready()
    finally:
        await runtime.aclose()


async def test_the_oldest_pending_frame_is_dropped_and_counted() -> None:
    """Dropping the newest frame would throw away the freshest evidence."""
    started = asyncio.Event()
    release = asyncio.Event()

    class _Slow(_Backend):
        def block(self, _: int) -> int:
            loop.call_soon_threadsafe(started.set)
            # block the single session thread until the test lets go
            asyncio.run_coroutine_threadsafe(release.wait(), loop).result(timeout=5)
            return 0

    loop = asyncio.get_running_loop()
    runtime: SessionRuntime[_Slow] = SessionRuntime("slow", _Slow, max_queue=1)
    try:
        first = asyncio.create_task(runtime.submit(lambda b: b.block(1)))
        await asyncio.wait_for(started.wait(), timeout=5)
        stale = asyncio.create_task(runtime.submit(lambda b: b.work(2)))
        await asyncio.sleep(0)
        fresh = asyncio.create_task(runtime.submit(lambda b: b.work(3)))
        with pytest.raises(Dropped):
            await asyncio.wait_for(stale, timeout=5)
        release.set()
        await asyncio.wait_for(first, timeout=5)
        assert await asyncio.wait_for(fresh, timeout=5) == 6
        assert runtime.dropped == 1
        assert runtime.drop_rate() == pytest.approx(1 / 3)
    finally:
        release.set()
        await runtime.aclose()


async def test_stats_report_the_model_and_the_queue() -> None:
    runtime: SessionRuntime[_Backend] = SessionRuntime("stats", _Backend)
    try:
        await runtime.submit(lambda b: b.work(1))
        line = runtime.stats().as_line()
        assert "model=fake@000000000000" in line and "dropped=0" in line
    finally:
        await runtime.aclose()


async def test_a_gap_opens_when_analysis_stops_and_closes_when_it_resumes() -> None:
    sink = _RecordingSink()
    gap = GapKeeper(sink, "canteen_door_01", FixedClock(T0))
    await gap.stopped("crash")
    await gap.stopped("crash")  # idempotent: the supervisor may fire twice
    assert sink.opened == [("canteen_door_01", "crash")]
    await gap.measuring()
    await gap.measuring()
    assert len(sink.closed) == 1
    assert gap.open_cause is None


async def test_measuring_records_a_gap_when_the_pipeline_raises() -> None:
    """ "We saw nothing" must stay distinguishable from "nothing happened"."""
    sink = _RecordingSink()
    gap = GapKeeper(sink, "canteen_door_01", FixedClock(T0))
    with pytest.raises(ZeroDivisionError):
        async with measuring(gap):
            raise ZeroDivisionError
    assert sink.opened == [("canteen_door_01", "crash")]


def test_frameref_matches_the_ingest_frame_fields() -> None:
    """The pipeline consumes RTSPSource's queue with no adapter, so a rename in
    either package has to fail loudly here rather than at runtime."""
    from argus.ingest.ringbuffer import Frame

    assert [f.name for f in Frame.__dataclass_fields__.values()] == [
        f.name for f in FrameRef.__dataclass_fields__.values()
    ]
