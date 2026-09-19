"""The default tap source: a file, or nothing.

`gate.tap_source: simulated` is the configured default because the reader is
the one unknown whose fallback lives outside the software (there is no reader on
any LAN we can reach yet). A simulated source keeps the whole gate path -- taps,
verification, review, the page -- real and demonstrable, and says in every row
that the tap was simulated.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

from argus.pipelines.gate.taps import ReaderHealth, Tap, TapChannel

log = logging.getLogger(__name__)


class SimulatedTapSource:
    """Taps from a JSON-lines file, or from `feed()` in a test.

    Each line is ``{"badge_id": "B-1", "after_s": 2.0}`` -- a badge and how long
    after the previous tap it happens. Absolute times would make a fixture go
    stale the moment it was written.
    """

    def __init__(
        self,
        reader_id: str = "gate_reader_01",
        path: str | Path | None = None,
        *,
        loop: bool = False,
    ) -> None:
        self.reader_id = reader_id
        self.path = Path(path) if path else None
        self.loop = loop
        self._queue: asyncio.Queue[Tap] = asyncio.Queue()
        self._seq = 0
        self._closed = False
        self._last_contact: datetime | None = None

    def feed(self, badge_id: str, when: datetime | None = None) -> Tap:
        """Inject one tap. What a test uses, and what a demo button would."""
        self._seq += 1
        tap = Tap(
            reader_id=self.reader_id,
            badge_id=badge_id,
            ts_utc=when or datetime.now(UTC),
            ts_reader_reported=None,
            channel=TapChannel.SIMULATED,
            reader_seq=self._seq,
        )
        self._queue.put_nowait(tap)
        return tap

    async def taps(self) -> AsyncIterator[Tap]:
        if self.path is not None:
            asyncio.get_running_loop().create_task(self._play_file())
        while not self._closed:
            try:
                yield await asyncio.wait_for(self._queue.get(), timeout=0.5)
            except TimeoutError:
                continue

    async def replay(self, since: datetime) -> AsyncIterator[Tap]:
        """A simulated reader keeps no log, and says so rather than inventing one."""
        log.info("simulated source has no reader log to replay since %s", since.isoformat())
        return
        yield  # pragma: no cover - makes this an async generator

    async def health(self) -> ReaderHealth:
        return ReaderHealth(
            connected=not self._closed,
            last_contact_utc=self._last_contact,
            detail="simulated source",
        )

    async def close(self) -> None:
        self._closed = True

    async def _play_file(self) -> None:
        assert self.path is not None
        for line in self.path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            entry = json.loads(line)
            await asyncio.sleep(float(entry.get("after_s", 0.0)))
            if self._closed:
                return
            self._last_contact = datetime.now(UTC)
            self.feed(entry["badge_id"])
        if self.loop and not self._closed:
            await self._play_file()
