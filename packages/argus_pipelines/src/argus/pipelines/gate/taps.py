"""The tap contract. Standard library only -- a structural test enforces it.

This is what `SimulatedTapSource` and `ZktTapSource` both satisfy, and what the
tests fake. A database import here would put a database behind every test of
either; a numpy import would put numpy in the reader client.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable


class TapChannel(StrEnum):
    """How a tap reached us. Values match gate_tap.source in SQL."""

    LIVE = "zkt_live"
    # Recovered from the reader's own log after an outage. Never verified,
    # because the person walked past minutes or hours ago.
    REPLAY = "zkt_replay"
    SIMULATED = "simulated"


@dataclass(frozen=True, slots=True)
class Tap:
    reader_id: str
    badge_id: str
    # Server clock at receipt: authoritative, and the only value used in
    # arithmetic (ARCHITECTURE.md §7.1).
    ts_utc: datetime
    # What the reader said. Audit only. Readers drift, and a reader that has
    # never been set has a clock from the factory.
    ts_reader_reported: datetime | None
    channel: TapChannel
    # The reader's own log index, where it has one. It is what makes a replayed
    # copy of a live tap identifiable as the same tap rather than a second one.
    reader_seq: int | None = None


@dataclass(frozen=True, slots=True)
class ReaderHealth:
    connected: bool
    last_contact_utc: datetime | None
    detail: str = ""


@runtime_checkable
class TapSource(Protocol):
    """A stream of taps, and an honest account of whether it is listening."""

    reader_id: str

    def taps(self) -> AsyncIterator[Tap]:
        """Live taps, as they happen."""
        ...

    def replay(self, since: datetime) -> AsyncIterator[Tap]:
        """Taps the reader logged while nobody was listening."""
        ...

    async def health(self) -> ReaderHealth: ...

    async def close(self) -> None: ...
