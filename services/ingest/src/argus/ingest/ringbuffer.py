"""Pre-trigger buffers (ARCHITECTURE.md §3, ADR-0031).

Two buffers per stream, holding different things for different reasons:

``PacketRingBuffer`` holds **encoded** packets and is the evidence path. A clip
is produced by remuxing these, so it is bit-identical to what the camera sent —
which is what ADR-0008 auditability actually wants, and incidentally ~100x
smaller than the alternative. At 2304x1296 a 15-second buffer of decoded rgb24
frames is about 4 GB *per camera*; the same window of H.264 packets is about
11 MB.

``RingBuffer`` holds **decoded** frames for the pipelines, and is deliberately
short — a couple of seconds at the sampled analysis rate, not the full
pre-trigger window.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from fractions import Fraction

import numpy as np


class PacketRingError(Exception):
    pass


@dataclass(slots=True)
class Frame:
    ts_server: datetime  # authoritative (server clock)
    ts_media: float | None  # seconds into the source media, for tests/audit
    image: np.ndarray  # rgb24, HxWx3 uint8


@dataclass(frozen=True, slots=True)
class CodecParams:
    """What a muxer needs to write these packets without decoding them.

    ``extradata`` carries SPS/PPS. Without it the packets are unplayable, and
    the failure is a file that opens and shows nothing rather than an error.
    """

    codec_name: str
    extradata: bytes | None
    width: int
    height: int
    time_base: Fraction | None
    average_rate: Fraction | None


@dataclass(slots=True)
class PacketRecord:
    ts_server: datetime  # wallclock at demux — used only to select a range
    pts: int | None  # native stream units — authoritative for playback timing
    dts: int | None
    duration: int | None
    is_keyframe: bool
    data: bytes

    def __len__(self) -> int:
        return len(self.data)


class PacketRingBuffer:
    """Duration- and byte-bounded, evicting whole GOPs. Thread-safe.

    Eviction is GOP-aligned because a P-frame without its keyframe is not
    decodable: dropping packets by age alone would leave a buffer whose head
    cannot be played. So the buffer retains from the last keyframe at or before
    the cutoff, which means it holds up to one extra GOP beyond ``seconds``.

    That makes GOP length an operational setting rather than a camera detail. At
    the default 50-100 frame GOP most cameras ship with, clips drag back two or
    three seconds; set the I-frame interval to 1x fps (ADR-0031).

    ``max_bytes`` is the second bound, and it is not belt-and-braces: a bitrate
    spike, or a camera with a very long GOP, would otherwise grow this buffer
    without limit while still satisfying the duration bound.
    """

    def __init__(self, seconds: float, max_bytes: int) -> None:
        if seconds <= 0:
            raise ValueError("packet ring seconds must be positive")
        if max_bytes <= 0:
            raise ValueError("packet ring max_bytes must be positive")
        self._seconds = seconds
        self._max_bytes = max_bytes
        self._packets: deque[PacketRecord] = deque()
        self._nbytes = 0
        self._params: CodecParams | None = None
        self._lock = threading.Lock()

    # -- parameters ---------------------------------------------------------

    def set_params(self, params: CodecParams) -> None:
        """Record the stream's codec parameters, captured at session open.

        A mid-stream resolution change (TESTING.md §4 lists it as a fault worth
        injecting) makes older packets unmuxable alongside newer ones, so the
        buffer is dropped rather than left to produce a corrupt clip.
        """
        with self._lock:
            if self._params is not None and (
                params.width != self._params.width
                or params.height != self._params.height
                or params.codec_name != self._params.codec_name
            ):
                self._packets.clear()
                self._nbytes = 0
            self._params = params

    @property
    def params(self) -> CodecParams | None:
        with self._lock:
            return self._params

    # -- writing ------------------------------------------------------------

    def append(self, packet: PacketRecord) -> None:
        with self._lock:
            self._packets.append(packet)
            self._nbytes += len(packet)
            self._evict_by_age(packet.ts_server.timestamp() - self._seconds)
            self._evict_by_size()

    def _evict_by_age(self, cutoff: float) -> None:
        keep_from = None
        for i, p in enumerate(self._packets):
            if p.ts_server.timestamp() >= cutoff:
                break
            if p.is_keyframe:
                keep_from = i
        if keep_from:
            self._drop_front(keep_from)

    def _evict_by_size(self) -> None:
        while self._nbytes > self._max_bytes:
            nxt = self._next_keyframe_index(1)
            if nxt is None:
                # a single GOP already exceeds the cap: keeping an undecodable
                # fragment would be worse than keeping one oversized GOP
                return
            self._drop_front(nxt)

    def _next_keyframe_index(self, start: int) -> int | None:
        for i in range(start, len(self._packets)):
            if self._packets[i].is_keyframe:
                return i
        return None

    def _drop_front(self, count: int) -> None:
        for _ in range(count):
            self._nbytes -= len(self._packets.popleft())

    # -- reading ------------------------------------------------------------

    def packets_between(
        self, start_utc: datetime, end_utc: datetime
    ) -> tuple[CodecParams, list[PacketRecord]]:
        """Packets covering [start, end], widened back to a keyframe.

        The returned clip therefore begins slightly *earlier* than asked, which
        is harmless for a pre-trigger clip and is the only way it decodes.
        """
        start = start_utc.timestamp()
        end = end_utc.timestamp()
        with self._lock:
            if self._params is None:
                raise PacketRingError("no codec parameters captured yet for this stream")
            packets = list(self._packets)
            first = None
            for i, p in enumerate(packets):
                if p.ts_server.timestamp() > end:
                    break
                if p.is_keyframe and p.ts_server.timestamp() <= start:
                    first = i
            if first is None:
                # the requested window predates the buffer, or no keyframe has
                # arrived yet; either way, say so rather than return a prefix
                # that will not decode
                raise PacketRingError(
                    f"no keyframe at or before {start_utc.isoformat()} in the packet ring "
                    f"({len(packets)} packets buffered); the window is older than the buffer"
                )
            selected = [p for p in packets[first:] if p.ts_server.timestamp() <= end]
            return self._params, selected

    def snapshot(self) -> list[PacketRecord]:
        with self._lock:
            return list(self._packets)

    def nbytes(self) -> int:
        with self._lock:
            return self._nbytes

    def earliest(self) -> PacketRecord | None:
        with self._lock:
            return self._packets[0] if self._packets else None

    def latest(self) -> PacketRecord | None:
        with self._lock:
            return self._packets[-1] if self._packets else None

    def __len__(self) -> int:
        with self._lock:
            return len(self._packets)


class RingBuffer:
    """Decoded frames for the pipelines. Bounded by duration; thread-safe
    (append from the decode thread, read anywhere).

    Kept short on purpose — the pre-trigger window lives in the packet ring now.
    """

    def __init__(self, seconds: float) -> None:
        if seconds <= 0:
            raise ValueError("ring buffer seconds must be positive")
        self._seconds = seconds
        self._frames: deque[Frame] = deque()
        self._lock = threading.Lock()

    def append(self, frame: Frame) -> None:
        with self._lock:
            self._frames.append(frame)
            cutoff = frame.ts_server.timestamp() - self._seconds
            while self._frames and self._frames[0].ts_server.timestamp() < cutoff:
                self._frames.popleft()

    def earliest(self) -> Frame | None:
        with self._lock:
            return self._frames[0] if self._frames else None

    def latest(self) -> Frame | None:
        with self._lock:
            return self._frames[-1] if self._frames else None

    def frames_between(self, start_utc: datetime, end_utc: datetime) -> list[Frame]:
        start = start_utc.timestamp()
        end = end_utc.timestamp()
        with self._lock:
            return [f for f in self._frames if start <= f.ts_server.timestamp() <= end]

    def snapshot(self) -> list[Frame]:
        with self._lock:
            return list(self._frames)

    def __len__(self) -> int:
        with self._lock:
            return len(self._frames)
