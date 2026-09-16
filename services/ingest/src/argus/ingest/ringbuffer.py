"""Pre-trigger ring buffer (ARCHITECTURE.md §3): recent frames per stream so a
clip can include the seconds *before* a trigger."""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime

import numpy as np


@dataclass(slots=True)
class Frame:
    ts_server: datetime  # authoritative (server clock)
    ts_media: float | None  # seconds into the source media, for tests/audit
    image: np.ndarray  # rgb24, HxWx3 uint8


class RingBuffer:
    """Bounded by duration; thread-safe (append from decode thread, read anywhere)."""

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
