"""Serving a clip: an allow-list, two path checks, and Range support.

The route takes a `clip_id` and never a path, so no user input reaches the
filesystem. The allow-list is the `clip` table. The path is checked twice --
once at write time by the schema's CHECK, once here after resolving symlinks --
because a symlink planted after the row was written would pass the first check
and not the second.

A refusal is **404, not 403**: a 403 confirms the file exists, which is
information the refused request has not earned.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

from fastapi import HTTPException, status
from fastapi.responses import Response, StreamingResponse

log = logging.getLogger(__name__)

CHUNK = 64 * 1024
RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


class ClipNotServable(Exception):
    pass


def resolve_clip(root: Path, rel_path: str) -> Path:
    """The file this row points at, or a refusal.

    Two independent checks, and both matter: the stored value must be relative
    with no parent traversal, and the resolved path must still be inside the
    clips root and be a regular file.
    """
    if rel_path.startswith("/") or ".." in rel_path:
        raise ClipNotServable(f"stored path is not relative: {rel_path!r}")
    root = root.resolve()
    candidate = (root / rel_path).resolve()
    if not candidate.is_relative_to(root):
        raise ClipNotServable(f"{rel_path!r} resolves outside the clips root")
    if not candidate.is_file():
        raise ClipNotServable(f"{rel_path!r} is recorded but not on disk")
    return candidate


def clip_offset_s(event_ts: datetime, keyframe_utc: datetime, duration_s: float) -> float:
    """Where in the clip the crossing is, for a `#t=` deep link.

    Measured from the clip's **actual** first frame, not from the range that was
    requested: clips snap back to the previous keyframe (ADR-0031), so an offset
    computed from the requested start lands a second or two early. Clamped at
    both ends, because a negative or past-the-end fragment silently starts the
    video from the beginning.
    """
    offset = (event_ts - keyframe_utc).total_seconds()
    return max(0.0, min(offset, max(0.0, duration_s)))


def parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    """One byte range, or None for "send the whole thing"."""
    if not header:
        return None
    match = RANGE_RE.match(header.strip())
    if match is None:
        raise HTTPException(
            status_code=status.HTTP_416_RANGE_NOT_SATISFIABLE,
            detail="only a single bytes= range is supported",
        )
    start_text, end_text = match.groups()
    if not start_text and not end_text:
        raise HTTPException(status_code=status.HTTP_416_RANGE_NOT_SATISFIABLE)
    if not start_text:  # suffix range: the last N bytes
        length = int(end_text)
        if length == 0:
            raise HTTPException(status_code=status.HTTP_416_RANGE_NOT_SATISFIABLE)
        start = max(0, size - length)
        return start, size - 1
    start = int(start_text)
    end = int(end_text) if end_text else size - 1
    if start >= size or end < start:
        raise HTTPException(status_code=status.HTTP_416_RANGE_NOT_SATISFIABLE)
    return start, min(end, size - 1)


def _stream(path: Path, start: int, end: int) -> Iterator[bytes]:
    """Read in chunks. A 30-second evidence clip is not small."""
    remaining = end - start + 1
    with path.open("rb") as handle:
        handle.seek(start)
        while remaining > 0:
            block = handle.read(min(CHUNK, remaining))
            if not block:
                return
            remaining -= len(block)
            yield block


def range_response(path: Path, range_header: str | None) -> Response:
    size = path.stat().st_size
    window = parse_range(range_header, size)
    if window is None:
        return StreamingResponse(
            _stream(path, 0, size - 1),
            media_type="video/mp4",
            headers={"Accept-Ranges": "bytes", "Content-Length": str(size)},
        )
    start, end = window
    return StreamingResponse(
        _stream(path, start, end),
        status_code=status.HTTP_206_PARTIAL_CONTENT,
        media_type="video/mp4",
        headers={
            "Accept-Ranges": "bytes",
            "Content-Range": f"bytes {start}-{end}/{size}",
            "Content-Length": str(end - start + 1),
        },
    )
