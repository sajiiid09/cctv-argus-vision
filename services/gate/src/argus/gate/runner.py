"""Tap in, gate event out, and a gap whenever we were not listening.

The ordering rule is the one that matters: **the tap is written before
verification is attempted** (ARCHITECTURE.md §7.2). A crash between the two
loses a verification result and never the attendance evidence, which is the
right way round -- somebody was at the gate either way, and that is the fact
payroll-adjacent reporting depends on.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from argus.common.clock import Clock, SystemClock
from argus.pipelines.gate.dedupe import TapDeduper
from argus.pipelines.gate.taps import Tap, TapChannel, TapSource
from argus.pipelines.gate.verify import Verdict, VerifyInput, VerifyOutcome, decide
from argus.store.db import Database

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class GateRecord:
    tap_id: UUID
    gate_event_id: UUID | None
    badge_id: str
    person_id: str | None
    outcome: VerifyOutcome
    score: float | None
    reason: str


class Verifier:
    """What the runner needs from the face stack. Faked in tests, absent today."""

    async def verify(
        self, person_id: str, around: datetime
    ) -> tuple[bool, float | None, float | None]:  # pragma: no cover - protocol-ish
        raise NotImplementedError


class GateRunner:
    def __init__(
        self,
        db: Database,
        source: TapSource,
        *,
        camera_id: str | None = None,
        verifier: Verifier | None = None,
        threshold: float | None = None,
        dedupe_window_s: float = 5.0,
        reader_gap_timeout_s: float = 30.0,
        verify_window_s: float = 3.0,
        clock: Clock | None = None,
        ingest_run_id: UUID | None = None,
    ) -> None:
        self.db = db
        self.source = source
        self.camera_id = camera_id
        self.verifier = verifier
        self.threshold = threshold
        self.verify_window_s = verify_window_s
        self.reader_gap_timeout_s = reader_gap_timeout_s
        self.clock = clock or SystemClock()
        self.ingest_run_id = ingest_run_id
        self.deduper = TapDeduper(window_s=dedupe_window_s)
        self._reader_gap_id: UUID | None = None

    async def handle(self, tap: Tap) -> GateRecord | None:
        """Record one tap and whatever could be decided about it."""
        if not self.deduper.accept(tap):
            log.info("ignoring duplicate tap for %s on %s", tap.badge_id, tap.reader_id)
            return None
        tap_id = await self._write_tap(tap)
        person_id = await self._badge_holder(tap.badge_id, tap.ts_utc)
        verdict, quality = await self._verify(tap, person_id)
        gate_event_id = await self._write_event(tap, tap_id, person_id, verdict, quality)
        log.info(
            "tap %s badge=%s person=%s -> %s (%s)",
            tap_id,
            tap.badge_id,
            person_id or "unassigned",
            verdict.outcome.value,
            verdict.reason,
        )
        return GateRecord(
            tap_id=tap_id,
            gate_event_id=gate_event_id,
            badge_id=tap.badge_id,
            person_id=person_id,
            outcome=verdict.outcome,
            score=verdict.score,
            reason=verdict.reason,
        )

    async def _write_tap(self, tap: Tap) -> UUID:
        tap_id = uuid4()
        await self.db.execute(
            "insert into gate_tap (tap_id, reader_id, badge_id, ts_utc, ts_reader_reported,"
            " source, reader_seq, ingest_run_id) values (%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                tap_id,
                tap.reader_id,
                tap.badge_id,
                tap.ts_utc,
                tap.ts_reader_reported,
                tap.channel.value,
                tap.reader_seq,
                self.ingest_run_id,
            ),
        )
        return tap_id

    async def _badge_holder(self, badge_id: str, when: datetime) -> str | None:
        """Who held this badge at tap time, as-of.

        As-of, not "currently": a tap from last week belongs to whoever held the
        badge last week, and reassignment must not rewrite history.
        """
        row = await self.db.fetch_one(
            "select person_id from badge_assignment where badge_id = %s"
            " and assigned_from <= %s and (assigned_to is null or assigned_to > %s)"
            " order by assigned_from desc limit 1",
            (badge_id, when, when),
        )
        return row[0] if row else None

    async def _verify(self, tap: Tap, person_id: str | None) -> tuple[Verdict, float | None]:
        template_present = False
        face_found = False
        score: float | None = None
        quality: float | None = None
        if person_id is not None and tap.channel is not TapChannel.REPLAY:
            row = await self.db.fetch_one(
                "select count(*) from face_template where person_id = %s and retired_at is null",
                (person_id,),
            )
            template_present = bool(row and row[0])
        if template_present and self.verifier is not None and self.threshold is not None:
            face_found, score, quality = await self.verifier.verify(
                person_id or "", tap.ts_utc + timedelta(seconds=self.verify_window_s / 2)
            )
        verdict = decide(
            VerifyInput(
                channel=tap.channel,
                template_present=template_present,
                face_found=face_found,
                score=score,
                threshold=self.threshold if self.verifier is not None else None,
            )
        )
        return verdict, quality

    async def _write_event(
        self,
        tap: Tap,
        tap_id: UUID,
        person_id: str | None,
        verdict: Verdict,
        quality: float | None,
    ) -> UUID:
        gate_event_id = uuid4()
        await self.db.execute(
            "insert into gate_event (gate_event_id, tap_id, person_id, camera_id,"
            " face_verified, match_score, face_quality, threshold, source)"
            " values (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                gate_event_id,
                tap_id,
                person_id,
                self.camera_id,
                verdict.outcome.value,
                verdict.score,
                quality,
                self.threshold,
                tap.channel.value,
            ),
        )
        return gate_event_id

    # -- "were we listening?" ----------------------------------------------

    async def check_reader_health(self) -> None:
        """Open a reader_gap when contact is lost, close it when it returns.

        A gate report that cannot tell "nobody tapped" from "we were not
        listening" has the bug stream_gap exists to prevent. This annotates
        only: the gate is not payroll-affecting and zeroes nothing.
        """
        health = await self.source.health()
        stale = (
            health.last_contact_utc is not None
            and (self.clock.now_utc() - health.last_contact_utc).total_seconds()
            > self.reader_gap_timeout_s
        )
        if (not health.connected or stale) and self._reader_gap_id is None:
            self._reader_gap_id = uuid4()
            await self.db.execute(
                "insert into reader_gap (gap_id, reader_id, from_utc, cause) values (%s,%s,%s,%s)",
                (
                    self._reader_gap_id,
                    self.source.reader_id,
                    self.clock.now_utc(),
                    "offline" if not health.connected else "clock_anomaly",
                ),
            )
            log.warning("reader %s: %s; gap opened", self.source.reader_id, health.detail)
        elif health.connected and not stale and self._reader_gap_id is not None:
            await self.db.execute(
                "update reader_gap set to_utc = %s where gap_id = %s and to_utc is null",
                (self.clock.now_utc(), self._reader_gap_id),
            )
            log.info("reader %s back; gap closed", self.source.reader_id)
            self._reader_gap_id = None

    async def replay_since(self, since: datetime) -> list[GateRecord]:
        """Recover taps from the reader's log. Every one is `not_attempted`."""
        recovered: list[GateRecord] = []
        async for tap in self.source.replay(since):
            record = await self.handle(tap)
            if record is not None:
                recovered.append(record)
        log.info("recovered %d tap(s) from the reader log", len(recovered))
        return recovered


def utc_now() -> datetime:
    return datetime.now(UTC)
