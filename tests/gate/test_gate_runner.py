"""The gate runner against a real database.

The invariant worth the postgres tier: **the tap is written before verification
is attempted**. A crash between them loses a verification result and never the
attendance evidence, and that is the right way round -- somebody was at the gate
either way.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from argus.common.clock import FixedClock
from argus.gate.runner import GateRunner
from argus.pipelines.gate.simulated import SimulatedTapSource
from argus.pipelines.gate.taps import Tap, TapChannel
from argus.pipelines.gate.verify import VerifyOutcome

from conftest import insert_person

pytestmark = pytest.mark.postgres

T0 = datetime(2026, 9, 18, 6, 0, tzinfo=UTC)


class _AlwaysMatches:
    def __init__(self, score: float = 0.9, found: bool = True) -> None:
        self.score = score
        self.found = found

    async def verify(self, person_id: str, around: datetime):
        return self.found, (self.score if self.found else None), 0.8


async def _gate_camera(store) -> str:
    await store.db.execute(
        "insert into camera (camera_id, role, source_uri, door_id, is_virtual)"
        " values ('gate_door', 'gate', 'rtsp://localhost:8554/gate_door', 'g1', true)"
        " on conflict (camera_id) do nothing"
    )
    return "gate_door"


async def _holder(store, person_id: str, badge_id: str, since: datetime = T0) -> None:
    await store.db.execute(
        "insert into badge_assignment (assignment_id, badge_id, person_id, assigned_from,"
        " assigned_by) values (gen_random_uuid(), %s, %s, %s, 'a human')",
        (badge_id, person_id, since - timedelta(days=1)),
    )


async def _template(store, person_id: str) -> None:
    await store.db.execute(
        "insert into consent_record (consent_id, person_id, purpose, granted_on, expires_on,"
        " recorded_by) values (gen_random_uuid(), %s, 'gate', '2026-09-01', '2026-12-31',"
        " 'a human')",
        (person_id,),
    )
    row = await store.db.fetch_one(
        "select consent_id from consent_record where person_id = %s", (person_id,)
    )
    await store.db.execute(
        "insert into face_template (template_id, person_id, consent_id, model_ref, embedding,"
        " dim, source, enrolled_by)"
        " values (gen_random_uuid(), %s, %s, 'mock@1', %s, 4, 'images', 'a human')",
        (person_id, row[0], b"\x00" * 16),
    )


def _runner(store, **kw) -> GateRunner:
    source = kw.pop("source", SimulatedTapSource())
    return GateRunner(
        store.db,
        source,
        camera_id="gate_door",
        clock=FixedClock(T0),
        **kw,
    )


def _tap(badge: str = "B-1", channel=TapChannel.SIMULATED, seq: int | None = 1, at=T0) -> Tap:
    return Tap(
        reader_id="gate_reader_01",
        badge_id=badge,
        ts_utc=at,
        ts_reader_reported=at - timedelta(seconds=3),
        channel=channel,
        reader_seq=seq,
    )


async def test_a_tap_with_no_verifier_is_recorded_as_not_attempted(store) -> None:
    """Today's real state: no enrolment thresholds, so nothing is compared --
    and the attendance evidence is kept anyway."""
    await _gate_camera(store)
    person = await insert_person(store.db, "p-gate")
    await _holder(store, person, "B-1")
    record = await _runner(store).handle(_tap())
    assert record is not None
    assert record.outcome is VerifyOutcome.NOT_ATTEMPTED
    rows = await store.db.fetch_all(
        "select t.badge_id, e.person_id, e.face_verified, e.match_score, e.source"
        " from gate_tap t join gate_event e on e.tap_id = t.tap_id"
    )
    assert rows == [("B-1", person, "not_attempted", None, "simulated")]


async def test_a_matching_face_is_recorded_true_with_its_score(store) -> None:
    await _gate_camera(store)
    person = await insert_person(store.db, "p-match")
    await _holder(store, person, "B-2")
    await _template(store, person)
    runner = _runner(store, verifier=_AlwaysMatches(0.77), threshold=0.4)
    record = await runner.handle(_tap("B-2"))
    assert record is not None and record.outcome is VerifyOutcome.TRUE
    rows = await store.db.fetch_all("select face_verified, match_score from gate_event")
    assert rows[0][0] == "true" and rows[0][1] == pytest.approx(0.77, abs=1e-6)


async def test_a_mismatch_is_a_review_item_not_an_alarm(store) -> None:
    """PLAN.md M4: never a block, never an alarm. A row a human looks at."""
    await _gate_camera(store)
    person = await insert_person(store.db, "p-mismatch")
    await _holder(store, person, "B-3")
    await _template(store, person)
    runner = _runner(store, verifier=_AlwaysMatches(0.05), threshold=0.4)
    record = await runner.handle(_tap("B-3"))
    assert record is not None and record.outcome is VerifyOutcome.FALSE
    rows = await store.db.fetch_all("select face_verified, review_state from gate_event")
    assert rows == [("false", "pending")]


async def test_no_face_is_stored_as_no_face(store) -> None:
    await _gate_camera(store)
    person = await insert_person(store.db, "p-noface")
    await _holder(store, person, "B-4")
    await _template(store, person)
    runner = _runner(store, verifier=_AlwaysMatches(found=False), threshold=0.4)
    record = await runner.handle(_tap("B-4"))
    assert record is not None and record.outcome is VerifyOutcome.NO_FACE
    rows = await store.db.fetch_all("select face_verified, match_score from gate_event")
    assert rows == [("no_face", None)]


async def test_an_unassigned_badge_still_records_the_tap(store) -> None:
    """A tap on a badge nobody holds is worth seeing, not worth losing."""
    await _gate_camera(store)
    record = await _runner(store).handle(_tap("B-orphan"))
    assert record is not None and record.person_id is None
    rows = await store.db.fetch_all("select badge_id from gate_tap")
    assert rows == [("B-orphan",)]


async def test_the_badge_holder_is_resolved_as_of_the_tap(store) -> None:
    """Reassigning a badge must not rewrite who tapped it last week."""
    await _gate_camera(store)
    first = await insert_person(store.db, "p-then")
    second = await insert_person(store.db, "p-now")
    await store.db.execute(
        "insert into badge_assignment (assignment_id, badge_id, person_id, assigned_from,"
        " assigned_to, assigned_by)"
        " values (gen_random_uuid(), 'B-5', %s, %s, %s, 'a human')",
        (first, T0 - timedelta(days=10), T0 - timedelta(days=2)),
    )
    await store.db.execute(
        "insert into badge_assignment (assignment_id, badge_id, person_id, assigned_from,"
        " assigned_by) values (gen_random_uuid(), 'B-5', %s, %s, 'a human')",
        (second, T0 - timedelta(days=1)),
    )
    runner = _runner(store)
    old = await runner.handle(_tap("B-5", at=T0 - timedelta(days=5), seq=10))
    new = await runner.handle(_tap("B-5", at=T0, seq=11))
    assert old is not None and old.person_id == first
    assert new is not None and new.person_id == second


async def test_a_replayed_tap_is_not_attempted_even_with_a_verifier(store) -> None:
    await _gate_camera(store)
    person = await insert_person(store.db, "p-replay")
    await _holder(store, person, "B-6")
    await _template(store, person)
    runner = _runner(store, verifier=_AlwaysMatches(0.99), threshold=0.4)
    record = await runner.handle(_tap("B-6", channel=TapChannel.REPLAY, seq=99))
    assert record is not None and record.outcome is VerifyOutcome.NOT_ATTEMPTED
    rows = await store.db.fetch_all("select source, face_verified from gate_event")
    assert rows == [("zkt_replay", "not_attempted")]


async def test_a_duplicate_tap_is_recorded_once(store) -> None:
    await _gate_camera(store)
    runner = _runner(store, dedupe_window_s=5.0)
    assert await runner.handle(_tap("B-7", seq=1)) is not None
    assert await runner.handle(_tap("B-7", seq=2, at=T0 + timedelta(seconds=2))) is None
    rows = await store.db.fetch_all("select count(*) from gate_tap")
    assert rows == [(1,)]


async def test_the_tap_survives_a_failed_verification(store, monkeypatch) -> None:
    """The ordering that matters: evidence first, verdict second."""
    await _gate_camera(store)
    person = await insert_person(store.db, "p-crash")
    await _holder(store, person, "B-8")
    await _template(store, person)

    class _Exploding:
        async def verify(self, person_id: str, around: datetime):
            raise RuntimeError("the camera process died")

    runner = _runner(store, verifier=_Exploding(), threshold=0.4)
    with pytest.raises(RuntimeError, match="camera process died"):
        await runner.handle(_tap("B-8"))
    taps = await store.db.fetch_all("select badge_id from gate_tap")
    events = await store.db.fetch_all("select count(*) from gate_event")
    assert taps == [("B-8",)], "the attendance evidence must survive"
    assert events == [(0,)], "and the verdict must not be invented"


async def test_a_reader_going_quiet_opens_and_closes_a_gap(store) -> None:
    """"Nobody tapped" and "we were not listening" are different facts."""
    await _gate_camera(store)

    class _Source(SimulatedTapSource):
        def __init__(self) -> None:
            super().__init__()
            self.connected = True

        async def health(self):
            from argus.pipelines.gate.taps import ReaderHealth

            return ReaderHealth(
                connected=self.connected, last_contact_utc=None, detail="test source"
            )

    source = _Source()
    runner = _runner(store, source=source)
    source.connected = False
    await runner.check_reader_health()
    rows = await store.db.fetch_all("select reader_id, cause, to_utc from reader_gap")
    assert rows == [("gate_reader_01", "offline", None)]

    source.connected = True
    await runner.check_reader_health()
    rows = await store.db.fetch_all("select to_utc is not null from reader_gap")
    assert rows == [(True,)]


async def test_the_simulated_source_is_the_configured_default() -> None:
    from argus.common.config import GateConfig

    assert GateConfig().tap_source == "simulated"


async def test_the_simulated_source_admits_it_has_no_log(store) -> None:
    source = SimulatedTapSource()
    recovered = [tap async for tap in source.replay(T0)]
    assert recovered == []
