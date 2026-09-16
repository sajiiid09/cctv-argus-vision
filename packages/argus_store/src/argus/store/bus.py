"""Event bus: Postgres LISTEN/NOTIFY (ADR-0013).

Only events travel. Frames never go through the bus.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass

import psycopg
from argus.store.store import EVENTS_CHANNEL


@dataclass(slots=True)
class Event:
    type: str
    fields: dict[str, str]


class EventBus:
    """Subscribes to ``argus_events`` and fans payloads out to local queues."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._queues: list[asyncio.Queue[Event]] = []
        self._conn: psycopg.AsyncConnection | None = None
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._conn = await psycopg.AsyncConnection.connect(self._dsn, autocommit=True)
        async with self._conn.cursor() as cur:
            await cur.execute(f"listen {EVENTS_CHANNEL}")
        self._task = asyncio.create_task(self._pump(), name="eventbus-pump")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def _pump(self) -> None:
        assert self._conn is not None
        async for notify in self._conn.notifies():
            try:
                payload = json.loads(notify.payload)
            except (json.JSONDecodeError, TypeError):
                continue
            event = Event(
                type=str(payload.get("type", "unknown")),
                fields={k: str(v) for k, v in payload.items()},
            )
            for q in self._queues:
                if q.full():
                    with contextlib.suppress(asyncio.QueueEmpty):
                        q.get_nowait()
                q.put_nowait(event)

    def subscribe(self) -> asyncio.Queue[Event]:
        q: asyncio.Queue[Event] = asyncio.Queue(maxsize=1000)
        self._queues.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[Event]) -> None:
        if q in self._queues:
            self._queues.remove(q)

    async def stream(self) -> AsyncIterator[Event]:
        q = self.subscribe()
        try:
            while True:
                yield await q.get()
        finally:
            self.unsubscribe(q)
