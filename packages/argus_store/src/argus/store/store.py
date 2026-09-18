"""Typed store operations: cameras, ingest runs, doorway events, stream gaps.

Every write of a doorway event or a stream-gap transition publishes a
notification on the ``argus_events`` channel (ADR-0013: Postgres is the bus;
frames never go through it, only events).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from argus.common.config import CameraConfig
from argus.store.db import Database, new_uuid

EVENTS_CHANNEL = "argus_events"

# Kept in one place because the SQL check constraint and this tuple must agree;
# tests/structural/test_sql_contracts.py compares them in both directions.
# 'aim_changed' is ADR-0029 (the camera moved off its preset) and 'overload' is
# ADR-0033 (the analyser could not keep up). Both mean "we stopped measuring",
# which payroll reads as a reason to zero the day -- unlike 'offline', which
# means the camera went away.
GAP_CAUSES = (
    "offline",
    "decode_error",
    "crash",
    "clock_anomaly",
    "stall",
    "refused",
    "aim_changed",
    "overload",
)


@dataclass(slots=True)
class DoorwayEvent:
    event_id: UUID
    camera_id: str
    door_id: str | None
    ts_utc: datetime
    ts_camera_reported: datetime | None
    direction: str  # enter | exit | ambiguous
    person_id: str | None
    match_confidence: float | None
    detection_quality: float | None
    clip_ref: str | None
    ingest_run_id: UUID | None
    # Set on the LATER of two detections of one crossing, pointing at the
    # earlier row, which is the one that survives (ADR-0032).
    duplicate_of: UUID | None


@dataclass(slots=True)
class StreamGap:
    gap_id: UUID
    camera_id: str
    from_utc: datetime
    to_utc: datetime | None
    cause: str


def _as_utc(ts: datetime | None) -> datetime | None:
    if ts is None:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


class Store:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def _notify(self, kind: str, **fields: object) -> None:
        payload = json.dumps({"type": kind, **fields}, default=str)
        await self.db.execute("select pg_notify(%s, %s)", (EVENTS_CHANNEL, payload))

    async def upsert_camera(self, cam: CameraConfig) -> None:
        await self.db.execute(
            """
            insert into camera (camera_id, role, source_uri, analysis_uri, space_id,
                                door_id, direction_hint, is_virtual)
            values (%s, %s, %s, %s, %s, %s, %s, %s)
            on conflict (camera_id) do update set
                role = excluded.role,
                source_uri = excluded.source_uri,
                analysis_uri = excluded.analysis_uri,
                space_id = excluded.space_id,
                door_id = excluded.door_id,
                direction_hint = excluded.direction_hint,
                is_virtual = excluded.is_virtual
            """,
            (
                cam.camera_id,
                cam.role,
                cam.source_uri,
                cam.analysis_uri,
                cam.space_id,
                cam.door_id,
                cam.direction_hint,
                cam.is_virtual,
            ),
        )

    async def record_ingest_run(self, code_version: str, cfg_hash: str) -> UUID:
        run_id = new_uuid()
        await self.db.execute(
            "insert into ingest_run (ingest_run_id, code_version, config_hash) values (%s, %s, %s)",
            (run_id, code_version, cfg_hash),
        )
        return run_id

    async def insert_doorway_event(
        self,
        camera_id: str,
        door_id: str | None,
        ts_utc: datetime,
        direction: str,
        *,
        ts_camera_reported: datetime | None = None,
        person_id: str | None = None,
        match_confidence: float | None = None,
        detection_quality: float | None = None,
        clip_ref: str | None = None,
        ingest_run_id: UUID | None = None,
        duplicate_of: UUID | None = None,
    ) -> DoorwayEvent:
        if direction not in ("enter", "exit", "ambiguous"):
            raise ValueError(f"invalid direction {direction!r}")
        # person_id None is valid evidence, not an error: an unrecognised face is
        # the fail-open outcome and the row is kept so the flag can be counted.
        event_id = new_uuid()
        await self.db.execute(
            """
            insert into doorway_event (event_id, camera_id, door_id, ts_utc,
                                       ts_camera_reported, direction, person_id,
                                       match_confidence, detection_quality,
                                       clip_ref, ingest_run_id, duplicate_of)
            values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                event_id,
                camera_id,
                door_id,
                ts_utc,
                _as_utc(ts_camera_reported),
                direction,
                person_id,
                match_confidence,
                detection_quality,
                clip_ref,
                ingest_run_id,
                duplicate_of,
            ),
        )
        event = DoorwayEvent(
            event_id=event_id,
            camera_id=camera_id,
            door_id=door_id,
            ts_utc=ts_utc,
            ts_camera_reported=_as_utc(ts_camera_reported),
            direction=direction,
            person_id=person_id,
            match_confidence=match_confidence,
            detection_quality=detection_quality,
            clip_ref=clip_ref,
            ingest_run_id=ingest_run_id,
            duplicate_of=duplicate_of,
        )
        await self._notify(
            "doorway_event",
            event_id=str(event_id),
            camera_id=camera_id,
            direction=direction,
            ts_utc=ts_utc.isoformat(),
        )
        return event

    async def open_gap(self, camera_id: str, from_utc: datetime, cause: str) -> UUID:
        if cause not in GAP_CAUSES:
            raise ValueError(f"invalid gap cause {cause!r}; expected one of {GAP_CAUSES}")
        gap_id = new_uuid()
        await self.db.execute(
            "insert into stream_gap (gap_id, camera_id, from_utc, to_utc, cause)"
            " values (%s, %s, %s, null, %s)",
            (gap_id, camera_id, from_utc, cause),
        )
        await self._notify(
            "stream_gap_opened",
            gap_id=str(gap_id),
            camera_id=camera_id,
            from_utc=from_utc.isoformat(),
            cause=cause,
        )
        return gap_id

    async def close_gap(self, gap_id: UUID, to_utc: datetime) -> None:
        await self.db.execute(
            "update stream_gap set to_utc = %s where gap_id = %s and to_utc is null",
            (to_utc, gap_id),
        )
        await self._notify("stream_gap_closed", gap_id=str(gap_id), to_utc=to_utc.isoformat())

    async def list_gaps(self, camera_id: str | None = None) -> list[StreamGap]:
        if camera_id is None:
            rows = await self.db.fetch_all(
                "select gap_id, camera_id, from_utc, to_utc, cause from stream_gap"
                " order by from_utc"
            )
        else:
            rows = await self.db.fetch_all(
                "select gap_id, camera_id, from_utc, to_utc, cause from stream_gap"
                " where camera_id = %s order by from_utc",
                (camera_id,),
            )
        return [
            StreamGap(gap_id=r[0], camera_id=r[1], from_utc=r[2], to_utc=r[3], cause=r[4])
            for r in rows
        ]

    async def list_events(self, camera_id: str | None = None) -> list[DoorwayEvent]:
        if camera_id is None:
            rows = await self.db.fetch_all(
                "select event_id, camera_id, door_id, ts_utc, ts_camera_reported, direction,"
                " person_id, match_confidence, detection_quality, clip_ref, ingest_run_id,"
                " duplicate_of from doorway_event order by ts_utc"
            )
        else:
            rows = await self.db.fetch_all(
                "select event_id, camera_id, door_id, ts_utc, ts_camera_reported, direction,"
                " person_id, match_confidence, detection_quality, clip_ref, ingest_run_id,"
                " duplicate_of from doorway_event where camera_id = %s order by ts_utc",
                (camera_id,),
            )
        return [
            DoorwayEvent(
                event_id=r[0],
                camera_id=r[1],
                door_id=r[2],
                ts_utc=r[3],
                ts_camera_reported=r[4],
                direction=r[5],
                person_id=r[6],
                match_confidence=r[7],
                detection_quality=r[8],
                clip_ref=r[9],
                ingest_run_id=r[10],
                duplicate_of=r[11],
            )
            for r in rows
        ]
