"""--day is a local day. The conversion happens once, and here."""

from __future__ import annotations

from datetime import UTC, date, datetime

from argus.pairing.main import local_day_window


def test_a_dhaka_day_spans_the_right_utc_hours() -> None:
    """Asia/Dhaka is UTC+6 with no DST, which removes a bug class and tempts
    people into assuming UTC. A local day starts at 18:00 the day before."""
    from_utc, to_utc = local_day_window(date(2026, 9, 18), "Asia/Dhaka")
    assert from_utc == datetime(2026, 9, 17, 18, 0, tzinfo=UTC)
    assert to_utc == datetime(2026, 9, 18, 18, 0, tzinfo=UTC)
    assert (to_utc - from_utc).total_seconds() == 86400


def test_a_utc_day_is_midnight_to_midnight() -> None:
    from_utc, to_utc = local_day_window(date(2026, 9, 18), "UTC")
    assert from_utc == datetime(2026, 9, 18, 0, 0, tzinfo=UTC)
    assert to_utc == datetime(2026, 9, 19, 0, 0, tzinfo=UTC)


def test_a_dst_zone_still_covers_exactly_one_day() -> None:
    """Bangladesh has no DST, but the helper must not assume that: a 23-hour
    local day is a real thing elsewhere and the window must still be the day."""
    from_utc, to_utc = local_day_window(date(2026, 3, 29), "Europe/London")
    assert (to_utc - from_utc).total_seconds() == 82800
