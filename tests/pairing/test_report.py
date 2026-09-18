"""The RISKS.md §8 metrics, against stored rows.

Each of these is a number somebody would act on, so the test checks the
arithmetic rather than just that the query runs: a gap that straddles the window
edge must count only the minutes inside it, and a rerun must not double the
day's totals.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from argus.common.clock import FixedClock
from argus.common.config import AppConfig, PayrollConfig
from argus.pairing.load import Window, load_window
from argus.pairing.persist import write_run
from argus.pairing.policy import lead_in_seconds, policy_from_config
from argus.pairing.report import (
    flagged_day_percent,
    gap_minutes,
    unknown_face_rate,
    unpaired_rate,
)
from argus.payroll import run_pairing

from helpers import insert_person

pytestmark = pytest.mark.postgres

DAY = date(2026, 9, 18)
START = datetime(2026, 9, 17, 18, 0, tzinfo=UTC)
END = START + timedelta(days=1)
LUNCH = datetime(2026, 9, 18, 7, 0, tzinfo=UTC)


async def _camera(store, camera_id="canteen_door_01", door_id="c1") -> None:
    await store.db.execute(
        "insert into camera (camera_id, role, source_uri, space_id, door_id, is_virtual)"
        " values (%s, 'canteen_door', %s, 'canteen', %s, true)"
        " on conflict (camera_id) do nothing",
        (camera_id, f"rtsp://localhost:8554/{camera_id}", door_id),
    )


async def _pair(store, person: str, enter: datetime, exit_: datetime) -> None:
    await store.insert_doorway_event(
        "canteen_door_01", "c1", enter, "enter", person_id=person, clip_ref="a.mp4"
    )
    await store.insert_doorway_event(
        "canteen_door_01", "c1", exit_, "exit", person_id=person, clip_ref="b.mp4"
    )


async def _run(store, **payroll):
    config = AppConfig(payroll=PayrollConfig(**payroll))
    policy = policy_from_config(config)
    window = Window(
        from_utc=START,
        to_utc=END,
        lead_in_s=lead_in_seconds(config, policy),
        space_ids=("canteen",),
    )
    evidence = await load_window(store.db, window)
    result = run_pairing(
        evidence.events, evidence.gaps, policy, FixedClock(END + timedelta(hours=1))
    )
    return await write_run(store.db, result, evidence, policy, code_version="test")


async def test_the_unknown_face_rate_is_per_door_per_hour(store) -> None:
    await _camera(store)
    person = await insert_person(store.db, "p-known")
    await store.insert_doorway_event("canteen_door_01", "c1", LUNCH, "enter", person_id=person)
    await store.insert_doorway_event("canteen_door_01", "c1", LUNCH + timedelta(minutes=1), "enter")
    await store.insert_doorway_event("canteen_door_01", "c1", LUNCH + timedelta(minutes=2), "enter")

    rows = await unknown_face_rate(store.db, START, END)
    assert rows == [("c1", datetime(2026, 9, 18, 7, 0, tzinfo=UTC), 2, 3)]


async def test_the_unpaired_rate_reads_the_latest_run_only(store) -> None:
    """A rerun supersedes rather than adds: a metric that summed every run
    would double after any recomputation."""
    await _camera(store)
    paired = await insert_person(store.db, "p-paired")
    lone = await insert_person(store.db, "p-lone-exit")
    await _pair(store, paired, LUNCH, LUNCH + timedelta(minutes=20))
    # An exit with no enter: somebody was missed on the way in. It resolves to
    # UNPAIRED_EXIT and charges nothing, and the rate is how often that happens.
    await store.insert_doorway_event(
        "canteen_door_01", "c1", LUNCH + timedelta(hours=2), "exit", person_id=lone
    )

    await _run(store, allowance_s=600)
    await _run(store, allowance_s=600)
    rows = await unpaired_rate(store.db, DAY)
    assert rows == [("canteen", 1, 2)]


async def test_gap_minutes_count_only_the_part_inside_the_window(store) -> None:
    await _camera(store)
    # a gap that starts two hours before the window and ends one hour into it
    gap_id = await store.open_gap("canteen_door_01", START - timedelta(hours=2), "offline")
    await store.close_gap(gap_id, START + timedelta(hours=1))
    # and an open one, an hour before the window's end
    await store.open_gap("canteen_door_01", END - timedelta(hours=1), "stall")

    rows = await gap_minutes(store.db, START, END)
    assert rows == [
        ("canteen_door_01", "offline", pytest.approx(60.0)),
        ("canteen_door_01", "stall", pytest.approx(60.0)),
    ]


async def test_the_flagged_day_percentage_and_the_total_charge(store) -> None:
    await _camera(store)
    clean = await insert_person(store.db, "p-clean")
    flagged = await insert_person(store.db, "p-flagged")
    await _pair(store, clean, LUNCH, LUNCH + timedelta(minutes=30))
    await store.insert_doorway_event(
        "canteen_door_01", "c1", LUNCH, "exit", person_id=flagged, clip_ref="c.mp4"
    )

    await _run(store, allowance_s=600)
    rows = await flagged_day_percent(store.db, DAY)
    flagged_count, days, overage_s = rows[0]
    assert (flagged_count, days) == (1, 2)
    # 30 minutes with a 10-minute allowance: 1200s charged, and the flagged
    # person's day charges nothing.
    assert overage_s == 1200
