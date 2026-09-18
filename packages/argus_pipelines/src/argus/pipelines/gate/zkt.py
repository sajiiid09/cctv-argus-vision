"""The ZKTeco reader client. The only module that speaks to the device.

**UNVERIFIED.** No reader has been on a LAN we can reach, so nothing here has
ever exchanged a byte with hardware. What is tested is the framing and the
record parsing, against fixtures **hand-constructed from the protocol
description** -- not captured from a device. That proves the parser is
self-consistent and nothing more, and it goes on AGENTS.md §7's list of what we
cannot test yet.

The protocol is implemented by hand rather than taken from a library: the
command set needed is small (connect, read the attendance log, subscribe to live
events, disconnect), and a dependency would bring an AGENTS.md §2.9 licence
conversation for hardware nobody can test against.

Wire format, for whoever meets a real device:

    header: 8 bytes little-endian -- command, checksum, session id, reply id
    payload follows; over TCP each message is prefixed with the 8-byte magic
    0x50 0x50 0x82 0x7d and a 4-byte length.

A structural test asserts this file is the only place the reader's port and
opcodes appear.
"""

from __future__ import annotations

import asyncio
import logging
import struct
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from argus.pipelines.gate.taps import ReaderHealth, Tap, TapChannel

log = logging.getLogger(__name__)

DEFAULT_PORT = 4370

CMD_CONNECT = 1000
CMD_EXIT = 1001
CMD_ACK_OK = 2000
CMD_ACK_UNAUTH = 2005
CMD_PREPARE_DATA = 1500
CMD_DATA = 1501
CMD_ATTLOG_RRQ = 13
CMD_REG_EVENT = 500

TCP_MAGIC = b"\x50\x50\x82\x7d"
ATTLOG_RECORD = 40


class ZktError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class AttendanceRecord:
    """One row of the reader's own log."""

    badge_id: str
    ts_reader_reported: datetime
    status: int
    index: int


def checksum(payload: bytes) -> int:
    """The protocol's 16-bit ones-complement checksum over 16-bit words."""
    if len(payload) % 2:
        payload += b"\x00"
    total = 0
    for (word,) in struct.iter_unpack("<H", payload):
        total += word
        total = (total & 0xFFFF) + (total >> 16)
    return (~total) & 0xFFFF


def encode_header(command: int, session_id: int, reply_id: int, payload: bytes = b"") -> bytes:
    body = struct.pack("<HHHH", command, 0, session_id, reply_id) + payload
    return struct.pack("<HHHH", command, checksum(body), session_id, reply_id) + payload


def decode_header(frame: bytes) -> tuple[int, int, int, int, bytes]:
    if len(frame) < 8:
        raise ZktError(f"frame shorter than a header: {len(frame)} bytes")
    command, given_checksum, session_id, reply_id = struct.unpack("<HHHH", frame[:8])
    payload = frame[8:]
    return command, given_checksum, session_id, reply_id, payload


def wrap_tcp(frame: bytes) -> bytes:
    return TCP_MAGIC + struct.pack("<I", len(frame)) + frame


def unwrap_tcp(buffer: bytes) -> tuple[bytes, bytes]:
    """Return (frame, rest). Raises if the buffer does not start with a frame."""
    if len(buffer) < 8:
        raise ZktError("incomplete TCP prefix")
    if buffer[:4] != TCP_MAGIC:
        raise ZktError(f"not a ZK TCP frame: {buffer[:4]!r}")
    (length,) = struct.unpack("<I", buffer[4:8])
    if len(buffer) < 8 + length:
        raise ZktError(f"truncated frame: want {length}, have {len(buffer) - 8}")
    return buffer[8 : 8 + length], buffer[8 + length :]


def parse_attendance(payload: bytes) -> list[AttendanceRecord]:
    """Parse the 40-byte attendance records the device streams back.

    Layout per record: 2-byte index, 24-byte user id (NUL padded), 1 byte
    reserved, 1 byte status, 4-byte timestamp, 8 bytes reserved.
    """
    records: list[AttendanceRecord] = []
    for offset in range(0, len(payload) - ATTLOG_RECORD + 1, ATTLOG_RECORD):
        chunk = payload[offset : offset + ATTLOG_RECORD]
        index = struct.unpack("<H", chunk[0:2])[0]
        badge = chunk[2:26].split(b"\x00", 1)[0].decode("ascii", "replace").strip()
        status = chunk[27]
        (encoded,) = struct.unpack("<I", chunk[28:32])
        if not badge:
            continue
        records.append(
            AttendanceRecord(
                badge_id=badge,
                ts_reader_reported=decode_time(encoded),
                status=status,
                index=index,
            )
        )
    return records


def decode_time(encoded: int) -> datetime:
    """The device's packed timestamp.

    Seconds since 2000-01-01, encoded as
    ((((year*12 + month)*31 + day)*24 + hour)*60 + minute)*60 + second.
    Returned as UTC-tagged, which is a **lie of convenience**: the reader
    reports local time and we do not know its offset. This value is audit-only
    and never used in arithmetic, which is what makes that acceptable.
    """
    second = encoded % 60
    encoded //= 60
    minute = encoded % 60
    encoded //= 60
    hour = encoded % 24
    encoded //= 24
    day = encoded % 31 + 1
    encoded //= 31
    month = encoded % 12 + 1
    year = encoded // 12 + 2000
    return datetime(year, month, day, hour, minute, second, tzinfo=UTC)


def encode_time(moment: datetime) -> int:
    """Inverse of decode_time. Exists so the fixtures are constructed, not copied."""
    return (
        ((((moment.year - 2000) * 12 + moment.month - 1) * 31 + moment.day - 1) * 24 + moment.hour)
        * 60
        + moment.minute
    ) * 60 + moment.second


class ZktTapSource:
    """Live and replayed taps from a ZKTeco reader over TCP.

    Ships UNVERIFIED. Day one on the box is: ping the reader, open port 4370,
    connect, read one live tap. That is the highest remaining unknown in the
    project and the only one whose fallback -- a person with a clipboard -- lives
    outside the software.
    """

    def __init__(
        self,
        host: str,
        *,
        reader_id: str = "gate_reader_01",
        port: int = DEFAULT_PORT,
        password: str | None = None,
        connect_timeout_s: float = 5.0,
        poll_interval_s: float = 1.0,
    ) -> None:
        self.reader_id = reader_id
        self.host = host
        self.port = port
        self._password = password
        self._connect_timeout_s = connect_timeout_s
        self._poll_interval_s = poll_interval_s
        self._session_id = 0
        self._reply_id = 0
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._last_contact: datetime | None = None
        self._closed = False

    def __repr__(self) -> str:
        # Never the password. A repr in a log is a credential in a log.
        return f"ZktTapSource(host={self.host!r}, port={self.port}, reader_id={self.reader_id!r})"

    async def connect(self) -> None:
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port), timeout=self._connect_timeout_s
        )
        payload = b""
        if self._password:
            payload = struct.pack("<I", _password_key(self._password))
        reply = await self._exchange(CMD_CONNECT, payload)
        command = reply[0]
        if command == CMD_ACK_UNAUTH:
            raise ZktError("reader refused the connection: wrong or missing password")
        if command != CMD_ACK_OK:
            raise ZktError(f"reader replied {command} to connect, expected {CMD_ACK_OK}")
        self._session_id = reply[2]
        self._last_contact = datetime.now(UTC)
        log.info("connected to reader %s at %s:%d", self.reader_id, self.host, self.port)

    async def taps(self) -> AsyncIterator[Tap]:
        """Live capture. Each event the reader pushes becomes a tap."""
        if self._writer is None:
            await self.connect()
        await self._exchange(CMD_REG_EVENT, struct.pack("<I", 0xFFFF))
        while not self._closed:
            frame = await self._read_frame(timeout_s=self._poll_interval_s)
            if frame is None:
                continue
            _command, _checksum, _session, _reply, payload = decode_header(frame)
            self._last_contact = datetime.now(UTC)
            for record in parse_attendance(payload):
                yield Tap(
                    reader_id=self.reader_id,
                    badge_id=record.badge_id,
                    ts_utc=datetime.now(UTC),
                    ts_reader_reported=record.ts_reader_reported,
                    channel=TapChannel.LIVE,
                    reader_seq=record.index,
                )

    async def replay(self, since: datetime) -> AsyncIterator[Tap]:
        """The reader's own log, for the window nobody was listening.

        Every tap from here is `zkt_replay`, which the schema constrains to
        `not_attempted`: we never compared a face against it.
        """
        if self._writer is None:
            await self.connect()
        reply = await self._exchange(CMD_ATTLOG_RRQ, b"")
        payload = reply[4]
        if reply[0] == CMD_PREPARE_DATA:
            payload = await self._read_data_stream()
        for record in parse_attendance(payload):
            if record.ts_reader_reported < since:
                continue
            yield Tap(
                reader_id=self.reader_id,
                badge_id=record.badge_id,
                ts_utc=datetime.now(UTC),
                ts_reader_reported=record.ts_reader_reported,
                channel=TapChannel.REPLAY,
                reader_seq=record.index,
            )

    async def health(self) -> ReaderHealth:
        connected = self._writer is not None and not self._closed
        detail = "connected" if connected else "not connected"
        if self._last_contact is not None:
            age = datetime.now(UTC) - self._last_contact
            if age > timedelta(seconds=60):
                detail = f"no contact for {age.total_seconds():.0f}s"
        return ReaderHealth(connected=connected, last_contact_utc=self._last_contact, detail=detail)

    async def close(self) -> None:
        self._closed = True
        if self._writer is None:
            return
        try:
            await self._exchange(CMD_EXIT, b"")
        except (ZktError, OSError, TimeoutError):
            log.debug("reader did not acknowledge disconnect", exc_info=True)
        finally:
            self._writer.close()
            self._writer = None
            self._reader = None

    # -- framing ------------------------------------------------------------

    async def _exchange(self, command: int, payload: bytes) -> tuple[int, int, int, int, bytes]:
        if self._writer is None or self._reader is None:
            raise ZktError("not connected")
        self._reply_id = (self._reply_id + 1) & 0xFFFF
        frame = encode_header(command, self._session_id, self._reply_id, payload)
        self._writer.write(wrap_tcp(frame))
        await self._writer.drain()
        reply = await self._read_frame(timeout_s=self._connect_timeout_s)
        if reply is None:
            raise ZktError(f"no reply to command {command}")
        return decode_header(reply)

    async def _read_frame(self, *, timeout_s: float) -> bytes | None:
        if self._reader is None:
            raise ZktError("not connected")
        try:
            prefix = await asyncio.wait_for(self._reader.readexactly(8), timeout=timeout_s)
        except (TimeoutError, asyncio.IncompleteReadError):
            return None
        if prefix[:4] != TCP_MAGIC:
            raise ZktError(f"not a ZK TCP frame: {prefix[:4]!r}")
        (length,) = struct.unpack("<I", prefix[4:8])
        return await asyncio.wait_for(self._reader.readexactly(length), timeout=timeout_s)

    async def _read_data_stream(self) -> bytes:
        chunks: list[bytes] = []
        while True:
            frame = await self._read_frame(timeout_s=self._connect_timeout_s)
            if frame is None:
                break
            command, _checksum, _session, _reply, payload = decode_header(frame)
            if command == CMD_DATA:
                chunks.append(payload)
                continue
            if command == CMD_ACK_OK:
                break
        return b"".join(chunks)


def _password_key(password: str) -> int:
    """The device's password scramble, as documented by the protocol notes."""
    key = 0
    for index, char in enumerate(password[:8]):
        key ^= (ord(char) << (index % 4 * 8)) & 0xFFFFFFFF
    return key & 0xFFFFFFFF
