"""Minimal, dependency-free PNG codec for golden frames.

Writes and reads the exact subset we produce: 8-bit RGB(A), non-interlaced.
Golden frames are committed as PNGs so a human can view them; the codec being
stdlib-only keeps determinism (no libpng build variance).

Not a general PNG library: unsupported chunks/formats raise loudly.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

import numpy as np

_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def write_png(path: str | Path, arr: np.ndarray) -> None:
    if arr.ndim != 3 or arr.dtype != np.uint8 or arr.shape[2] not in (3, 4):
        raise ValueError(f"expected HxWx3 or HxWx4 uint8, got {arr.shape} {arr.dtype}")
    h, w, c = arr.shape
    color_type = 2 if c == 3 else 6
    stride = w * c
    flat = arr.reshape(h * w * c)
    raw = b"".join(b"\x00" + flat[i * stride : (i + 1) * stride].tobytes() for i in range(h))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", w, h, 8, color_type, 0, 0, 0)
    blob = (
        _SIGNATURE
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )
    Path(path).write_bytes(blob)


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def read_png(path: str | Path) -> np.ndarray:
    data = Path(path).read_bytes()
    if data[:8] != _SIGNATURE:
        raise ValueError("not a PNG")
    pos = 8
    ihdr: tuple[int, int, int, int] | None = None
    idat = bytearray()
    while pos < len(data):
        length, tag = struct.unpack(">I4s", data[pos : pos + 8])
        payload = data[pos + 8 : pos + 8 + length]
        pos += 12 + length
        if tag == b"IHDR":
            w, h, depth, color = struct.unpack(">IIBB", payload[:10])
            if depth != 8 or color not in (2, 6):
                raise ValueError(f"unsupported PNG (depth={depth}, color={color})")
            ihdr = (w, h, depth, color)
        elif tag == b"IDAT":
            idat.extend(payload)
        elif tag == b"IEND":
            break
    if ihdr is None:
        raise ValueError("PNG missing IHDR")
    w, h, _, color = ihdr
    c = 3 if color == 2 else 4
    stride = w * c
    raw = zlib.decompress(bytes(idat))
    if len(raw) != h * (stride + 1):
        raise ValueError("PNG size mismatch (interlaced?)")
    out = np.zeros((h, w, c), dtype=np.uint8)
    prev = np.zeros(stride, dtype=np.int32)
    for y in range(h):
        off = y * (stride + 1)
        ftype = raw[off]
        line = np.frombuffer(raw[off + 1 : off + 1 + stride], dtype=np.uint8).astype(np.int32)
        if ftype == 0:
            cur = line
        elif ftype == 1:
            cur = line
            for i in range(c, stride):
                cur[i] = (cur[i] + cur[i - c]) & 0xFF
        elif ftype == 2:
            cur = (line + prev) & 0xFF
        elif ftype == 3:
            cur = line
            for i in range(stride):
                left = cur[i - c] if i >= c else 0
                cur[i] = (cur[i] + (left + prev[i]) // 2) & 0xFF
        elif ftype == 4:
            cur = line
            for i in range(stride):
                a = cur[i - c] if i >= c else 0
                b = int(prev[i])
                cc = int(prev[i - c]) if i >= c else 0
                cur[i] = (cur[i] + _paeth(a, b, cc)) & 0xFF
        else:
            raise ValueError(f"bad filter type {ftype}")
        out[y] = cur.reshape(w, c).astype(np.uint8)
        prev = cur
    if c == 4:
        out = out[:, :, :3]
    return out
