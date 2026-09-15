"""Clip store: video segments addressed by (camera, time range).

Every payroll-affecting record points into this store (ADR-0008); the ingest
side only has to make clips addressable and playable.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import av
import numpy as np
from argus.ingest.ringbuffer import Frame
from argus.ingest.streams import RTSPSource


class ClipError(Exception):
    pass


def clip_path(root: Path, camera_id: str, start_utc: datetime, end_utc: datetime) -> Path:
    day = start_utc.astimezone(UTC).strftime("%Y-%m-%d")
    start_ms = int(start_utc.timestamp() * 1000)
    end_ms = int(end_utc.timestamp() * 1000)
    return root / camera_id / day / f"{start_ms}_{end_ms}.mp4"


class ClipStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    async def extract(
        self,
        source: RTSPSource,
        start_utc: datetime,
        end_utc: datetime,
        *,
        timeout_s: float = 10.0,
    ) -> Path:
        """Extract [start, end] from the source's ring buffer plus live frames.

        The pre-trigger case (start in the recent past, end now-ish) is the
        supported one; ranges entirely inside the ring buffer work too. Ranges
        older than the ring buffer are a ClipError — loud, not silent.
        """
        frames: list[Frame] = source.ring.frames_between(start_utc, end_utc)
        now = datetime.now(UTC)
        if end_utc > now:
            frames.extend(await self._collect_live(source, start_utc, end_utc, timeout_s))
        frames.sort(key=lambda f: f.ts_server)
        if not frames:
            raise ClipError(
                f"no frames available for {source.camera.camera_id} in "
                f"[{start_utc.isoformat()}, {end_utc.isoformat()}]"
            )

        path = clip_path(
            self.root, source.camera.camera_id, frames[0].ts_server, frames[-1].ts_server
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(self._encode, path, frames)
        return path

    async def _collect_live(
        self, source: RTSPSource, start_utc: datetime, end_utc: datetime, timeout_s: float
    ) -> list[Frame]:
        q = source.subscribe()
        collected: list[Frame] = []
        try:

            async def _drain() -> None:
                while True:
                    f = await q.get()
                    if f.ts_server >= end_utc:
                        return
                    if f.ts_server >= start_utc:
                        collected.append(f)

            await asyncio.wait_for(_drain(), timeout=timeout_s)
        except TimeoutError:
            pass
        finally:
            source.unsubscribe(q)
        return collected

    def _encode(self, path: Path, frames: list[Frame]) -> None:
        height, width = frames[0].image.shape[:2]
        container = av.open(str(path), mode="w", format="mp4")
        try:
            stream = container.add_stream("libx264", rate=30)
            stream.width = width
            stream.height = height
            stream.pix_fmt = "yuv420p"
            base = frames[0].ts_server.timestamp()
            next_pts = 0
            for f in frames:
                h, w = f.image.shape[:2]
                img = f.image
                if (h, w) != (height, width):
                    img = _resize_nearest(img, width, height)
                vframe = av.VideoFrame.from_ndarray(np.ascontiguousarray(img), format="rgb24")
                # wallclock-derived pts, forced strictly monotonic (duplicate
                # wallclock ms across two frames is normal and EINVALs the muxer)
                want_pts = round((f.ts_server.timestamp() - base) * 30)
                next_pts = max(next_pts + 1, want_pts, 1)
                vframe.pts = next_pts
                for packet in stream.encode(vframe):
                    container.mux(packet)
            for packet in stream.encode():
                container.mux(packet)
        finally:
            container.close()


def _resize_nearest(arr: np.ndarray, width: int, height: int) -> np.ndarray:
    ys = (np.arange(height) * (arr.shape[0] / height)).astype(np.intp).clip(0, arr.shape[0] - 1)
    xs = (np.arange(width) * (arr.shape[1] / width)).astype(np.intp).clip(0, arr.shape[1] - 1)
    return arr[ys][:, xs]
