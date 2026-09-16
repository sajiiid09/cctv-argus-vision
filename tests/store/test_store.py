from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from argus.common.config import CameraConfig
from argus.store.db import apply_migrations

pytestmark = pytest.mark.postgres

NOW = datetime(2026, 9, 16, 7, 30, tzinfo=UTC)


def _cam(camera_id: str = "canteen_door_01", **kw) -> CameraConfig:
    params = dict(
        camera_id=camera_id,
        role="canteen_door",
        space_id="canteen",
        door_id="c1",
        source_uri="rtsp://localhost:8554/canteen_door_01",
        is_virtual=True,
    )
    params.update(kw)
    return CameraConfig(**params)


async def test_migrations_idempotent(postgres_db):
    first = await apply_migrations(postgres_db)
    second = await apply_migrations(postgres_db)
    assert second == []
    assert first == [] or "0001_init.sql" in first


async def test_camera_upsert(store):
    await store.upsert_camera(_cam())
    await store.upsert_camera(_cam(source_uri="rtsp://new"))
    rows = await store.db.fetch_all(
        "select camera_id, source_uri, is_virtual, space_id from camera"
    )
    assert rows == [("canteen_door_01", "rtsp://new", True, "canteen")]


async def test_doorway_event_append_only(store):
    await store.upsert_camera(_cam())
    event = await store.insert_doorway_event(
        "canteen_door_01", "c1", NOW, "enter", person_id="p1", clip_ref="clips/x.mp4"
    )
    assert event.person_id == "p1"
    with pytest.raises(Exception, match="append-only"):
        await store.db.execute(
            "update doorway_event set person_id = 'someone_else' where event_id = %s",
            (event.event_id,),
        )
    with pytest.raises(Exception, match="append-only"):
        await store.db.execute("delete from doorway_event where event_id = %s", (event.event_id,))
    # still exactly one row, unmodified
    events = await store.list_events()
    assert len(events) == 1 and events[0].person_id == "p1"


async def test_doorway_event_validation(store):
    await store.upsert_camera(_cam())
    with pytest.raises(ValueError):
        await store.insert_doorway_event("canteen_door_01", "c1", NOW, "teleported")


async def test_gap_lifecycle(store):
    await store.upsert_camera(_cam())
    gap_id = await store.open_gap("canteen_door_01", NOW, "stall")
    gaps = await store.list_gaps("canteen_door_01")
    assert len(gaps) == 1
    assert gaps[0].to_utc is None and gaps[0].cause == "stall"
    await store.close_gap(gap_id, NOW + timedelta(seconds=12))
    gaps = await store.list_gaps("canteen_door_01")
    assert gaps[0].to_utc == NOW + timedelta(seconds=12)
    with pytest.raises(ValueError):
        await store.open_gap("canteen_door_01", NOW, "mystery")


async def test_unknown_face_event_has_null_person(store):
    await store.upsert_camera(_cam())
    event = await store.insert_doorway_event("canteen_door_01", "c1", NOW, "enter")
    assert event.person_id is None
    row = await store.db.fetch_one(
        "select person_id, match_confidence from doorway_event where event_id = %s",
        (event.event_id,),
    )
    assert row == (None, None)
