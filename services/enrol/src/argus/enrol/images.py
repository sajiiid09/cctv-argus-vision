"""Enrolment photographs on disk: where they go, and how tightly.

ADR-0027 permits keeping these unencrypted for the demo roster, and the scope of
that permission is only defensible if the directory mode is actually what the
ADR says. So the mode is set explicitly rather than left to umask, checked at
startup, and the filename is the image hash -- which makes re-adding the same
photograph idempotent instead of silently doubling somebody's templates.
"""

from __future__ import annotations

import hashlib
import logging
import os
import stat
from dataclasses import dataclass
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

DIR_MODE = 0o700
FILE_MODE = 0o600


class ImageError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class LoadedImage:
    path: Path
    sha256: str
    pixels: np.ndarray  # HxWx3 uint8 rgb


def ensure_root(root: Path) -> Path:
    """Create or tighten the enrolment root, and refuse if it stays loose.

    A directory we find at 0755 is tightened rather than refused -- refusing
    would leave the photographs sitting there readable, which is worse -- but it
    is said out loud, because somebody widened it and should know it changed
    back. If it cannot be tightened, that is fatal: ADR-0027 permits these
    images unencrypted only on the basis that the mode is what it says.
    """
    existed = root.exists()
    before = stat.S_IMODE(root.stat().st_mode) if existed else None
    root.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
    # mkdir's mode is masked by umask, so set it explicitly -- otherwise a
    # default umask of 022 quietly produces 0755 and the ADR's scope no longer
    # matches reality.
    os.chmod(root, DIR_MODE)
    mode = stat.S_IMODE(root.stat().st_mode)
    if before is not None and before & 0o077 and before != mode:
        log.warning(
            "%s was mode %o and has been tightened to %o: enrolment images must not be "
            "readable by anyone else (ADR-0027)",
            root,
            before,
            mode,
        )
    if mode & 0o077:
        raise ImageError(
            f"{root} is mode {mode:o} and could not be tightened; enrolment images must not "
            "be readable by anyone else (ADR-0027 permits them unencrypted only on that basis)"
        )
    return root


def person_dir(root: Path, person_id: str) -> Path:
    directory = ensure_root(root) / person_id
    directory.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
    os.chmod(directory, DIR_MODE)
    return directory


def read_image(path: Path) -> LoadedImage:
    """Decode one photograph to rgb24, and hash the file as it was given to us.

    The hash is of the file, not of the pixels: it identifies the artefact a
    human handed over, which is what an audit question is about.
    """
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    try:
        import av
    except ImportError as exc:  # pragma: no cover - av is a declared dependency
        raise ImageError("PyAV is required to read enrolment images") from exc
    try:
        with av.open(str(path)) as container:
            frame = next(container.decode(video=0))
            pixels = frame.to_ndarray(format="rgb24")
    except Exception as exc:
        raise ImageError(f"{path.name}: could not decode as an image ({exc})") from exc
    if pixels.ndim != 3 or pixels.shape[2] != 3:
        raise ImageError(f"{path.name}: decoded to {pixels.shape}, expected HxWx3 rgb")
    return LoadedImage(path=path, sha256=digest, pixels=pixels)


def store_image(root: Path, person_id: str, image: LoadedImage, suffix: str = ".png") -> Path:
    """Copy a photograph into the enrolment store, named by its hash."""
    directory = person_dir(root, person_id)
    destination = directory / f"{image.sha256}{suffix}"
    if destination.exists():
        return destination
    destination.write_bytes(image.path.read_bytes())
    os.chmod(destination, FILE_MODE)
    return destination


def delete_person_images(root: Path, person_id: str) -> list[Path]:
    directory = root / person_id
    if not directory.exists():
        return []
    removed = []
    for path in sorted(directory.iterdir()):
        if path.is_file():
            path.unlink()
            removed.append(path)
    directory.rmdir()
    return removed
