"""One thread per model session, with bounded, counted backpressure.

Not primarily a thread-safety workaround. One thread per session makes the
order frames reach a model deterministic, which is what the rig-replay
comparison depends on, and it makes latency attributable to a named model
rather than to "inference".

The queueing policy is a payroll decision wearing a performance costume. When
the analyser cannot keep up, something has to give, and the options are: block
the reader (the RTSP session stalls and the camera drops us), drop the newest
frame (the freshest evidence is the first thing thrown away), or drop the
oldest and **count it**. This does the last one, and a sustained drop rate is
what makes the canteen pipeline open an ``overload`` gap -- because a pipeline
that quietly analyses one frame in ten is claiming to measure something it is
not.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

log = logging.getLogger(__name__)

R = TypeVar("R")


class Dropped(Exception):
    """This frame was never given to the model. Not an error -- a fact to count."""


@dataclass(frozen=True, slots=True)
class RuntimeStats:
    name: str
    model_ref: str
    submitted: int
    completed: int
    dropped: int
    depth: int
    p50_ms: float
    p95_ms: float

    def as_line(self) -> str:
        return (
            f"{self.name} model={self.model_ref} submitted={self.submitted} "
            f"dropped={self.dropped} depth={self.depth} "
            f"p50={self.p50_ms:.0f}ms p95={self.p95_ms:.0f}ms"
        )


class SessionRuntime[T]:
    """Owns one backend instance and the single thread that may touch it."""

    def __init__(
        self,
        name: str,
        factory: Callable[[], T],
        *,
        max_queue: int = 2,
        latency_window: int = 128,
    ) -> None:
        if max_queue < 1:
            raise ValueError("max_queue must be at least 1")
        self.name = name
        self._factory = factory
        self._max_queue = max_queue
        self._pending: deque[tuple[Callable[[T], Any], asyncio.Future[Any], float]] = deque()
        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)
        self._stop = False
        self._thread: threading.Thread | None = None
        self._backend: T | None = None
        self._ready = threading.Event()
        self._error: BaseException | None = None
        self._latencies: deque[float] = deque(maxlen=latency_window)
        self.submitted = 0
        self.completed = 0
        self.dropped = 0

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._serve, name=f"runtime-{self.name}", daemon=True
        )
        self._thread.start()

    async def wait_ready(self, timeout: float = 120.0) -> None:
        """Block until the backend is constructed, or raise what stopped it.

        Construction is where a missing artefact, a bad hash or an unusable
        provider shows up. Surfacing it here means the pipeline fails at
        startup with the model's own error rather than on the first frame.
        """
        self.start()
        await asyncio.get_running_loop().run_in_executor(None, self._ready.wait, timeout)
        if not self._ready.is_set():
            raise TimeoutError(f"{self.name}: backend construction timed out after {timeout}s")
        if self._error is not None:
            raise self._error

    @property
    def model_ref(self) -> str:
        backend = self._backend
        return getattr(backend, "model_ref", "unconstructed")

    async def aclose(self) -> None:
        with self._wake:
            self._stop = True
            self._wake.notify_all()
        thread = self._thread
        if thread is not None:
            await asyncio.get_running_loop().run_in_executor(None, thread.join, 5.0)
        self._thread = None

    # -- work ---------------------------------------------------------------

    async def submit(self, fn: Callable[[T], R]) -> R:
        """Run `fn(backend)` on the session thread. Raises Dropped if displaced."""
        await self.wait_ready()
        loop = asyncio.get_running_loop()
        future: asyncio.Future[R] = loop.create_future()
        with self._wake:
            self._pending.append((fn, future, time.perf_counter()))
            self.submitted += 1
            while len(self._pending) > self._max_queue:
                _, stale, _ = self._pending.popleft()
                self.dropped += 1
                loop.call_soon_threadsafe(_fail_if_pending, stale, Dropped(self.name))
            self._wake.notify()
        return await future

    def stats(self) -> RuntimeStats:
        with self._lock:
            depth = len(self._pending)
            latencies = sorted(self._latencies)
        return RuntimeStats(
            name=self.name,
            model_ref=self.model_ref,
            submitted=self.submitted,
            completed=self.completed,
            dropped=self.dropped,
            depth=depth,
            p50_ms=_percentile(latencies, 0.50) * 1000.0,
            p95_ms=_percentile(latencies, 0.95) * 1000.0,
        )

    def drop_rate(self) -> float:
        return self.dropped / self.submitted if self.submitted else 0.0

    # -- the one thread -----------------------------------------------------

    def _serve(self) -> None:
        try:
            self._backend = self._factory()
        except BaseException as exc:  # construction failure is the caller's problem
            self._error = exc
            self._ready.set()
            return
        self._ready.set()
        while True:
            with self._wake:
                while not self._pending and not self._stop:
                    self._wake.wait(timeout=0.5)
                if self._stop and not self._pending:
                    return
                fn, future, queued_at = self._pending.popleft()
            if future.cancelled():
                continue
            loop = future.get_loop()
            started = time.perf_counter()
            try:
                result = fn(self._backend)
            except BaseException as exc:
                loop.call_soon_threadsafe(_fail_if_pending, future, exc)
                continue
            elapsed = time.perf_counter() - started
            with self._lock:
                self._latencies.append(elapsed)
                self.completed += 1
            del queued_at
            loop.call_soon_threadsafe(_set_if_pending, future, result)


def _set_if_pending(future: asyncio.Future[Any], value: Any) -> None:
    if not future.done():
        future.set_result(value)


def _fail_if_pending(future: asyncio.Future[Any], exc: BaseException) -> None:
    if not future.done():
        future.set_exception(exc)


def _percentile(sorted_values: list[float], q: float) -> float:
    if not sorted_values:
        return 0.0
    index = min(len(sorted_values) - 1, round(q * (len(sorted_values) - 1)))
    return sorted_values[index]
