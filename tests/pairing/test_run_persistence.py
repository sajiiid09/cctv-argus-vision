"""The pairing service against a real database.

What is being checked is not "does pairing work" -- tests/payroll does that with
no database at all. It is the orchestration: what gets read, what gets written,
what happens when a write fails halfway, and whether a re-run can corrupt
anything.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from argus.common.clock import FixedClock
from argus.common.config import AppConfig, PayrollConfig
from argus.pairing.load import LoadError, Window, canteen_spaces, load_window
from argus.pairing.persist import summarise, write_run
from argus.pairing.policy import lead_in_seconds, policy_from_config
from argus.payroll import run_pairing
from argus.store.bus import EventBus

from conftest import TEST_DSN, insert_person

pytestmark = pytest.mark.postgres

DAY_START = datetime(2026, 9, 17, 18, 0, tzinfo=UTC)  # 2026-09-18 in Dhaka
DAY_END = DAY_START + timedelta(days=1)
LUNCH = datetime(2026, 9, 18, 7, 0, tzinfo=UTC)  # 13:00 Dhaka


def _config(**payroll) -> AppConfig:
    return AppConfig(payroll=PayrollConfig(**payroll))


def _window(config: AppConfig, spaces: tuple[str, ...] = ("canteen",)) -> Window:
    policy = policy_from_config(config)
    return Window(
        from_utc=DAY_START,
        to_utc=DAY_END,
        lead_in_s=lead_in_seconds(config, policy),
        space_ids=spaces,
    )


async def _cameras(store) -> None:
    for camera_id, door_id, space in (
        ("canteen_door_01", "c1", "canteen"),
        ("canteen_door_02", "c2", "canteen"),
    ):
        await store.db.execute(
            "insert into camera (camera_id, role, source_uri, space_id, door_id, is_virtual)"
            " values (%s, 'canteen_door', %s, %s, %s, true)"
            " on conflict (camera_id) do nothing",
            (camera_id, f"rtsp://localhost:8554/{camera_id}", space, door_id),
        )


async def _crossing(store, person_id: str, ts: datetime, direction: str, camera="canteen_door_01"):
    return await store.insert_doorway_event(
        camera,
        "c1" if camera.endswith("01") else "c2",
        ts,
        direction,
        person_id=person_id,
        clip_ref=f"{camera}/{ts.timestamp():.0f}.mp4",
    )


async def _run(store, config: AppConfig, **kw):
    policy = policy_from_config(config)
    evidence = await load_window(store.db, _window(config))
    result = run_pairing(
        evidence.events, evidence.gaps, policy, FixedClock(DAY_END + timedelta(hours=1))
    )
    run_id = await write_run(store.db, result, evidence, policy, code_version="test", **kw)
    return run_id, result


async def test_a_clean_pair_becomes_an_interval_a_day_and_a_line(store) -> None:
    await _cameras(store)
    person = await insert_person(store.db, "p-clean")
    await _crossing(store, person, LUNCH, "enter")
    await _crossing(store, person, LUNCH + timedelta(minutes=45), "exit")

    run_id, result = await _run(store, _config(allowance_s=1800))
    assert [i.state.value for i in result.intervals] == ["resolved"]

    rows = await store.db.fetch_all(
        "select state, duration_s, local_day from dwell_interval where pairing_run_id = %s",
        (run_id,),
    )
    assert rows == [("resolved", 2700, datetime(2026, 9, 18).date())]
    days = await store.db.fetch_all(
        "select day_state, total_dwell_s, overage_s from dwell_day where pairing_run_id = %s",
        (run_id,),
    )
    assert days == [("clean", 2700, 900)]
    lines = await store.db.fetch_all(
        "select overage_s, day_state, shadow_mode, exported_at from payroll_line"
        " where pairing_run_id = %s",
        (run_id,),
    )
    assert lines == [(900, "clean", True, None)]


async def test_a_flagged_day_still_gets_a_line_charging_nothing(store) -> None:
    """A flagged day with no row is indistinguishable from a person who did not
    eat. Fail-open is only visible if it is on the page."""
    await _cameras(store)
    person = await insert_person(store.db, "p-flagged")
    await _crossing(store, person, LUNCH, "enter")  # no exit, ever

    run_id, result = await _run(store, _config(allowance_s=600))
    # Past the plausibility ceiling by computation time, so it closes unpaired
    # rather than staying open -- and an unpaired enter charges nothing.
    assert [i.state.value for i in result.intervals] == ["unpaired_enter"]
    days = await store.db.fetch_all(
        "select day_state, overage_s from dwell_day where pairing_run_id = %s", (run_id,)
    )
    assert days == [("flagged", 0)]
    lines = await store.db.fetch_all(
        "select overage_s, day_state from payroll_line where pairing_run_id = %s", (run_id,)
    )
    assert lines == [(0, "flagged")], "a flagged day must still produce a line"


async def test_an_enter_still_inside_is_listed_not_charged(store) -> None:
    """Somebody who has not come out yet is not an interval and not a charge.

    The review queue needs to see them, which is why the run records them.
    """
    await _cameras(store)
    person = await insert_person(store.db, "p-inside")
    recent = DAY_END - timedelta(minutes=30)
    await _crossing(store, person, recent, "enter")

    config = _config(allowance_s=600)
    policy = policy_from_config(config)
    evidence = await load_window(store.db, _window(config))
    result = run_pairing(
        evidence.events, evidence.gaps, policy, FixedClock(recent + timedelta(minutes=5))
    )
    run_id = await write_run(store.db, result, evidence, policy, code_version="test")
    assert result.intervals == ()
    open_enters = await store.db.fetch_all(
        "select count(*) from pairing_open_enter where pairing_run_id = %s", (run_id,)
    )
    assert open_enters == [(1,)]


async def test_a_gap_in_the_space_zeroes_the_day(store) -> None:
    """ADR-0023, through the whole service: a gap on EITHER door of the space."""
    await _cameras(store)
    person = await insert_person(store.db, "p-gap")
    await _crossing(store, person, LUNCH, "enter")
    await _crossing(store, person, LUNCH + timedelta(minutes=50), "exit")
    # The gap is on the OTHER door: the unseen exit might have been there.
    gap_id = await store.open_gap("canteen_door_02", LUNCH + timedelta(minutes=10), "offline")
    await store.close_gap(gap_id, LUNCH + timedelta(minutes=20))

    run_id, result = await _run(store, _config(allowance_s=600))
    assert [i.state.value for i in result.intervals] == ["gap_affected"]
    days = await store.db.fetch_all(
        "select day_state, overage_s from dwell_day where pairing_run_id = %s", (run_id,)
    )
    assert days == [("flagged", 0)]


async def test_an_open_gap_is_never_dropped(store) -> None:
    """Dropping a gap because it has no end is the most plausible way to charge
    somebody straight through an outage."""
    await _cameras(store)
    person = await insert_person(store.db, "p-open-gap")
    await _crossing(store, person, LUNCH, "enter")
    await _crossing(store, person, LUNCH + timedelta(minutes=40), "exit")
    await store.open_gap("canteen_door_01", LUNCH + timedelta(minutes=5), "stall")

    _run_id, result = await _run(store, _config(allowance_s=60))
    assert [i.state.value for i in result.intervals] == ["gap_affected"]
    assert all(day.overage_s == 0 for day in result.days)


async def test_the_lead_in_keeps_an_earlier_enter_from_becoming_unpaired(store) -> None:
    """An interval that began before the window must not be truncated: a window
    boundary must not decide whether somebody is charged."""
    await _cameras(store)
    person = await insert_person(store.db, "p-lead-in")
    # enter 40 minutes before the local day starts, exit just after it starts
    await _crossing(store, person, DAY_START - timedelta(minutes=40), "enter")
    await _crossing(store, person, DAY_START + timedelta(minutes=5), "exit")

    config = _config(allowance_s=600, max_dwell_s=10800)
    _run_id, result = await _run(store, config)
    assert [i.state.value for i in result.intervals] == ["resolved"]

    # Without the lead-in the enter is invisible and the exit is unpaired.
    policy = policy_from_config(config)
    narrow = Window(from_utc=DAY_START, to_utc=DAY_END, lead_in_s=0, space_ids=("canteen",))
    evidence = await load_window(store.db, narrow)
    truncated = run_pairing(
        evidence.events, evidence.gaps, policy, FixedClock(DAY_END + timedelta(hours=1))
    )
    assert [i.state.value for i in truncated.intervals] == ["unpaired_exit"]


async def test_pairing_is_per_space_not_per_door(store) -> None:
    await _cameras(store)
    person = await insert_person(store.db, "p-two-doors")
    await _crossing(store, person, LUNCH, "enter", camera="canteen_door_01")
    await _crossing(store, person, LUNCH + timedelta(minutes=30), "exit", camera="canteen_door_02")

    _run_id, result = await _run(store, _config(allowance_s=600))
    assert [i.state.value for i in result.intervals] == ["resolved"]


async def test_unknown_people_are_retained_as_evidence_and_charged_nothing(store) -> None:
    await _cameras(store)
    await store.insert_doorway_event("canteen_door_01", "c1", LUNCH, "enter")
    await store.insert_doorway_event("canteen_door_01", "c1", LUNCH + timedelta(minutes=20), "exit")

    run_id, result = await _run(store, _config())
    assert result.intervals == ()
    rows = await store.db.fetch_all(
        "select flag, count(*) from pairing_unattributable where pairing_run_id = %s group by flag",
        (run_id,),
    )
    assert rows == [("unknown_person", 2)]


async def test_a_rerun_writes_a_new_run_and_edits_nothing(store) -> None:
    """Recomputation is safe by construction: new run id, identical interval
    ids, and the append-only triggers make an in-place edit impossible."""
    await _cameras(store)
    person = await insert_person(store.db, "p-rerun")
    await _crossing(store, person, LUNCH, "enter")
    await _crossing(store, person, LUNCH + timedelta(minutes=30), "exit")

    first_id, first = await _run(store, _config(allowance_s=600))
    second_id, second = await _run(store, _config(allowance_s=600))
    assert first_id != second_id
    assert [i.interval_id for i in first.intervals] == [i.interval_id for i in second.intervals]
    runs = await store.db.fetch_all("select count(*) from pairing_run")
    assert runs == [(2,)]


async def test_a_half_written_run_leaves_nothing_behind(store, monkeypatch) -> None:
    """One transaction. `Database.connect` is autocommit, so reusing the
    ordinary execute path would leave a run that looks complete."""
    await _cameras(store)
    person = await insert_person(store.db, "p-atomic")
    await _crossing(store, person, LUNCH, "enter")
    await _crossing(store, person, LUNCH + timedelta(minutes=30), "exit")

    config = _config(allowance_s=600)
    policy = policy_from_config(config)
    evidence = await load_window(store.db, _window(config))
    result = run_pairing(
        evidence.events, evidence.gaps, policy, FixedClock(DAY_END + timedelta(hours=1))
    )

    real_uuid4 = uuid4
    calls = {"n": 0}

    def exploding_uuid4():
        calls["n"] += 1
        if calls["n"] > 2:  # run id and the first day id are fine; then fail
            raise RuntimeError("disk on fire")
        return real_uuid4()

    monkeypatch.setattr("argus.pairing.persist.uuid4", exploding_uuid4)
    with pytest.raises(RuntimeError, match="disk on fire"):
        await write_run(store.db, result, evidence, policy, code_version="test")

    for table in ("pairing_run", "dwell_interval", "dwell_day", "payroll_line"):
        rows = await store.db.fetch_all(f"select count(*) from {table}")
        assert rows == [(0,)], f"{table} kept rows from a failed run"


async def test_the_notification_arrives_only_after_the_commit(store) -> None:
    """A subscriber must never see a run id that does not exist yet."""
    await _cameras(store)
    person = await insert_person(store.db, "p-notify")
    await _crossing(store, person, LUNCH, "enter")
    await _crossing(store, person, LUNCH + timedelta(minutes=30), "exit")

    bus = EventBus(TEST_DSN)
    await bus.start()
    queue = bus.subscribe()
    try:
        run_id, _result = await _run(store, _config(allowance_s=600))
        import asyncio

        event = await asyncio.wait_for(queue.get(), timeout=10)
        while event.type != "pairing_run":
            event = await asyncio.wait_for(queue.get(), timeout=10)
        assert event.fields["pairing_run_id"] == str(run_id)
        rows = await store.db.fetch_all(
            "select count(*) from pairing_run where pairing_run_id = %s", (run_id,)
        )
        assert rows == [(1,)]
    finally:
        bus.unsubscribe(queue)
        await bus.stop()


async def test_spaces_come_from_the_camera_rows(store) -> None:
    await _cameras(store)
    assert await canteen_spaces(store.db) == ("canteen",)


async def test_a_camera_with_no_space_is_refused_rather_than_guessed(store) -> None:
    """CameraConfig makes this impossible in config, so a null here means a row
    written by hand -- and a wrong space pairs the wrong crossings together."""
    await store.db.execute(
        "insert into camera (camera_id, role, source_uri, door_id, is_virtual)"
        " values ('rogue', 'floor', 'rtsp://x', 'r1', true)"
    )
    person = await insert_person(store.db, "p-rogue")
    await store.db.execute(
        "insert into doorway_event (event_id, camera_id, door_id, ts_utc, direction, person_id)"
        " values (%s, 'rogue', 'r1', %s, 'enter', %s)",
        (uuid4(), LUNCH, person),
    )
    with pytest.raises(LoadError, match="no space_id"):
        await load_window(
            store.db,
            Window(from_utc=DAY_START, to_utc=DAY_END, lead_in_s=0, space_ids=()),
        )


async def test_the_summary_says_shadow_mode_out_loud(store) -> None:
    await _cameras(store)
    person = await insert_person(store.db, "p-summary")
    await _crossing(store, person, LUNCH, "enter")
    await _crossing(store, person, LUNCH + timedelta(minutes=70), "exit")
    _run_id, result = await _run(store, _config(allowance_s=3600))
    text = "\n".join(summarise(result))
    assert "shadow mode" in text
    assert "resolved=1" in text
