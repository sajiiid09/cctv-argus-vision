"""Dedup, and the reader protocol nobody has spoken to a device.

The ZKT fixtures are **hand-constructed from the protocol description**, not
captured from hardware, so what they prove is that the parser is
self-consistent. That is worth having -- signature and framing drift is the
realistic decay mode for code nobody can run -- and it is not evidence the
reader works.
"""

from __future__ import annotations

import struct
from datetime import UTC, datetime, timedelta

import pytest
from argus.pipelines.gate.dedupe import TapDeduper
from argus.pipelines.gate.taps import Tap, TapChannel, TapSource
from argus.pipelines.gate.zkt import (
    ATTLOG_RECORD,
    CMD_CONNECT,
    ZktError,
    ZktTapSource,
    checksum,
    decode_header,
    decode_time,
    encode_header,
    encode_time,
    parse_attendance,
    unwrap_tcp,
    wrap_tcp,
)

T0 = datetime(2026, 9, 18, 6, 0, tzinfo=UTC)


def _tap(badge: str = "B-1", offset_s: float = 0.0, channel=TapChannel.LIVE, seq=None) -> Tap:
    return Tap(
        reader_id="gate_reader_01",
        badge_id=badge,
        ts_utc=T0 + timedelta(seconds=offset_s),
        ts_reader_reported=None,
        channel=channel,
        reader_seq=seq,
    )


def test_a_second_tap_inside_the_window_is_one_tap() -> None:
    deduper = TapDeduper(window_s=5.0)
    assert deduper.accept(_tap())
    assert not deduper.accept(_tap(offset_s=2.0))
    assert deduper.accept(_tap(offset_s=30.0))


def test_different_badges_never_collide() -> None:
    deduper = TapDeduper(window_s=5.0)
    assert deduper.accept(_tap("B-1"))
    assert deduper.accept(_tap("B-2", offset_s=0.5))


def test_a_replayed_copy_of_a_live_tap_is_dropped_by_sequence() -> None:
    deduper = TapDeduper(window_s=5.0)
    assert deduper.accept(_tap(seq=7))
    assert not deduper.accept(_tap(offset_s=600, channel=TapChannel.REPLAY, seq=7))


def test_the_checksum_round_trips() -> None:
    frame = encode_header(CMD_CONNECT, session_id=0, reply_id=1, payload=b"\x01\x02\x03\x04")
    command, given, session, reply, payload = decode_header(frame)
    assert (command, session, reply) == (CMD_CONNECT, 0, 1)
    assert payload == b"\x01\x02\x03\x04"
    body = struct.pack("<HHHH", command, 0, session, reply) + payload
    assert given == checksum(body)


def test_the_tcp_wrapper_round_trips_and_rejects_rubbish() -> None:
    frame = encode_header(CMD_CONNECT, 0, 1)
    wrapped = wrap_tcp(frame)
    unwrapped, rest = unwrap_tcp(wrapped + b"trailing")
    assert unwrapped == frame and rest == b"trailing"
    with pytest.raises(ZktError, match="not a ZK TCP frame"):
        unwrap_tcp(b"\x00\x00\x00\x00\x04\x00\x00\x00abcd")
    with pytest.raises(ZktError, match="truncated"):
        unwrap_tcp(wrapped[:-2])


def test_the_packed_timestamp_round_trips() -> None:
    for moment in (
        datetime(2026, 9, 18, 13, 45, 30, tzinfo=UTC),
        datetime(2000, 1, 1, 0, 0, 0, tzinfo=UTC),
        datetime(2031, 12, 31, 23, 59, 59, tzinfo=UTC),
    ):
        assert decode_time(encode_time(moment)) == moment


def test_attendance_records_parse_and_skip_empty_slots() -> None:
    def record(index: int, badge: str, when: datetime) -> bytes:
        chunk = bytearray(ATTLOG_RECORD)
        chunk[0:2] = struct.pack("<H", index)
        chunk[2 : 2 + len(badge)] = badge.encode()
        chunk[27] = 1
        chunk[28:32] = struct.pack("<I", encode_time(when))
        return bytes(chunk)

    payload = (
        record(1, "B-1", datetime(2026, 9, 18, 6, 0, tzinfo=UTC))
        + bytes(ATTLOG_RECORD)  # an empty slot: the device pads its log
        + record(3, "B-2", datetime(2026, 9, 18, 7, 30, tzinfo=UTC))
    )
    records = parse_attendance(payload)
    assert [(r.index, r.badge_id) for r in records] == [(1, "B-1"), (3, "B-2")]
    assert records[1].ts_reader_reported.hour == 7


def test_the_source_satisfies_the_tap_contract_without_a_reader() -> None:
    """Signature drift is the realistic decay mode for code nobody can run."""
    source = ZktTapSource(host="10.0.0.9", password="hunter2")
    assert isinstance(source, TapSource)
    for method in ("taps", "replay", "health", "close"):
        assert callable(getattr(source, method))


def test_the_repr_never_carries_the_password() -> None:
    source = ZktTapSource(host="10.0.0.9", password="hunter2")
    assert "hunter2" not in repr(source)
    assert "10.0.0.9" in repr(source)


@pytest.mark.reader
def test_a_real_reader_answers() -> None:  # pragma: no cover - never runs in CI
    """The day-one check on the box: ping, port 4370, connect, one live tap.

    Marked `reader`, which nothing selects. It is here so the command exists
    before somebody is standing next to the hardware.
    """
    pytest.skip("needs a ZKTeco reader on the LAN; run it by hand on the box")
