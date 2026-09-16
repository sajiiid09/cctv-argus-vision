from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from argus.common.config import CameraConfig
from argus.store.bus import EventBus

from conftest import TEST_DSN

pytestmark = pytest.mark.postgres

NOW = datetime(2026, 9, 16, 8, 0, tzinfo=UTC)


async def test_events_travel_over_notify(postgres_db, store):
    bus = EventBus(TEST_DSN)
    await bus.start()
    try:
        q = bus.subscribe()
        await store.upsert_camera(
            CameraConfig(
                camera_id="c1",
                role="floor",
                source_uri="rtsp://x",
                is_virtual=True,
            )
        )
        await store.insert_doorway_event("c1", None, NOW, "ambiguous")
        gap_id = await store.open_gap("c1", NOW, "offline")

        received: list[str] = []
        for _ in range(40):
            try:
                received.append(q.get_nowait().type)
            except asyncio.QueueEmpty:
                await asyncio.sleep(0.1)
        assert "doorway_event" in received
        assert "stream_gap_opened" in received

        await store.close_gap(gap_id, NOW)
        got_close = False
        for _ in range(40):
            try:
                if q.get_nowait().type == "stream_gap_closed":
                    got_close = True
                    break
            except asyncio.QueueEmpty:
                await asyncio.sleep(0.1)
        assert got_close
    finally:
        await bus.stop()
