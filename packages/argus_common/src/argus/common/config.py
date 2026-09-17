"""Configuration: one file/env mechanism on every platform (ARCHITECTURE.md §5.4).

YAML file plus ``ARGUS_SECTION__KEY`` env overrides, coerced to field types.
No ad-hoc CLI arguments that only exist on one machine.
"""

from __future__ import annotations

import os
import typing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class ConfigError(Exception):
    pass


@dataclass(slots=True)
class CameraConfig:
    camera_id: str
    role: str  # gate | canteen_door | floor
    source_uri: str
    space_id: str | None = None
    door_id: str | None = None
    direction_hint: str | None = None
    is_virtual: bool = False
    # Second, lower-resolution stream for inference (ADR-0031). The main stream
    # feeds the packet ring at evidence quality; this feeds the sampled decode.
    # None means "analyse the main stream", which is the single-connection
    # fallback for a camera that refuses two concurrent clients.
    analysis_uri: str | None = None
    # Sampled decode rate. None inherits IngestConfig.analysis_fps.
    analysis_fps: float | None = None
    # Door line as FRACTIONS of frame width/height, never pixels: the main and
    # analysis streams have different resolutions, and pixel coordinates would be
    # silently reinterpreted between them.
    door_line: tuple[float, float, float, float] | None = None
    # Which side of the line is inside the space: +1 or -1.
    inside_sign: int = 1
    # PTZ preset this camera is expected to sit at (ADR-0029). Recorded so a
    # drift check has something to name when it fires.
    preset_id: str | None = None

    def __post_init__(self) -> None:
        valid_roles = {"gate", "canteen_door", "floor"}
        if self.role not in valid_roles:
            raise ConfigError(
                f"camera {self.camera_id}: role must be one of {sorted(valid_roles)}, "
                f"got {self.role!r}"
            )
        if self.role == "canteen_door" and not self.space_id:
            raise ConfigError(
                f"camera {self.camera_id}: canteen_door cameras need space_id "
                "(pairing is per canteen space, DATA_MODEL.md §4)"
            )
        if self.analysis_fps is not None and self.analysis_fps <= 0:
            raise ConfigError(
                f"camera {self.camera_id}: analysis_fps must be positive, got {self.analysis_fps}"
            )
        if self.door_line is not None:
            values = [float(v) for v in self.door_line]
            if len(values) != 4:
                raise ConfigError(
                    f"camera {self.camera_id}: door_line must be [x1, y1, x2, y2] as "
                    "fractions of frame size"
                )
            if not all(0.0 <= v <= 1.0 for v in values):
                raise ConfigError(
                    f"camera {self.camera_id}: door_line values are fractions of frame "
                    f"size and must lie in [0, 1], got {values}"
                )
            if values[:2] == values[2:]:
                raise ConfigError(f"camera {self.camera_id}: door_line endpoints are identical")
            self.door_line = (values[0], values[1], values[2], values[3])
        if self.inside_sign not in (1, -1):
            raise ConfigError(
                f"camera {self.camera_id}: inside_sign must be +1 or -1, got {self.inside_sign}"
            )

    @property
    def inference_uri(self) -> str:
        """The stream pipelines decode. Falls back to the main stream."""
        return self.analysis_uri or self.source_uri


@dataclass(slots=True)
class DatabaseConfig:
    dsn: str = "postgresql://argus:argus@localhost:5432/argus"


@dataclass(slots=True)
class ClipsConfig:
    dir: str = "var/clips"


@dataclass(slots=True)
class ReconnectConfig:
    initial_s: float = 0.5
    max_s: float = 30.0
    jitter: float = 0.25

    def __post_init__(self) -> None:
        if self.initial_s <= 0 or self.max_s <= 0 or not 0 <= self.jitter <= 1:
            raise ConfigError(
                f"invalid reconnect config: initial_s={self.initial_s} max_s={self.max_s} "
                f"jitter={self.jitter} (need positive times, jitter in [0,1])"
            )


@dataclass(slots=True)
class IngestConfig:
    stall_timeout_s: float = 5.0
    decode: str = "software"  # software | nvidia (probe falls back to software)
    reconnect: ReconnectConfig = field(default_factory=ReconnectConfig)
    # Pre-trigger window, now held as encoded packets rather than decoded frames
    # (ADR-0031). The name is unchanged because the meaning is unchanged: how far
    # back a clip can reach.
    ring_buffer_seconds: float = 15.0
    # Hard byte cap per camera on that buffer. The duration bound alone cannot
    # stop a bitrate spike or a very long GOP from growing it.
    ring_buffer_bytes: int = 128 * 1024 * 1024
    # Decoded frames kept for pipelines. Short on purpose.
    analysis_buffer_seconds: float = 2.0
    # Default sampled decode rate; CameraConfig.analysis_fps overrides per camera.
    analysis_fps: float = 8.0
    # Longest clip that may be requested. Guards the decode fallback path, where
    # a long range would otherwise mean gigabytes of decoded frames.
    max_clip_seconds: float = 30.0

    def __post_init__(self) -> None:
        if self.stall_timeout_s <= 0:
            raise ConfigError(f"stall_timeout_s must be positive, got {self.stall_timeout_s}")
        if self.ring_buffer_seconds <= 0:
            raise ConfigError(
                f"ring_buffer_seconds must be positive, got {self.ring_buffer_seconds}"
            )
        if self.ring_buffer_bytes <= 0:
            raise ConfigError(f"ring_buffer_bytes must be positive, got {self.ring_buffer_bytes}")
        if self.analysis_buffer_seconds <= 0:
            raise ConfigError(
                f"analysis_buffer_seconds must be positive, got {self.analysis_buffer_seconds}"
            )
        if self.analysis_fps <= 0:
            raise ConfigError(f"analysis_fps must be positive, got {self.analysis_fps}")
        if self.max_clip_seconds <= 0:
            raise ConfigError(f"max_clip_seconds must be positive, got {self.max_clip_seconds}")
        if self.decode not in ("software", "nvidia"):
            raise ConfigError(f"decode must be 'software' or 'nvidia', got {self.decode!r}")

    def fps_for(self, camera: CameraConfig) -> float:
        return camera.analysis_fps if camera.analysis_fps is not None else self.analysis_fps


@dataclass(slots=True)
class AppConfig:
    timezone: str = "Asia/Dhaka"
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    clips: ClipsConfig = field(default_factory=ClipsConfig)
    ingest: IngestConfig = field(default_factory=IngestConfig)
    cameras: list[CameraConfig] = field(default_factory=list)


_NESTED: dict[type, dict[str, type[Any]]] = {
    AppConfig: {
        "database": DatabaseConfig,
        "clips": ClipsConfig,
        "ingest": IngestConfig,
    },
    IngestConfig: {
        "reconnect": ReconnectConfig,
    },
}


def _coerce(raw: str, target: type[Any]) -> Any:
    if target is bool:
        if raw.lower() in ("1", "true", "yes", "on"):
            return True
        if raw.lower() in ("0", "false", "no", "off"):
            return False
        raise ConfigError(f"cannot parse bool from {raw!r}")
    if target is float:
        return float(raw)
    if target is int:
        return int(raw)
    return raw


def apply_env_overrides(config: AppConfig, env: dict[str, str] | None = None) -> AppConfig:
    env = env if env is not None else dict(os.environ)
    nested_types = _NESTED[type(config)]
    for key, value in env.items():
        if not key.startswith("ARGUS_") or "__" not in key:
            continue
        path = key[len("ARGUS_") :].lower().split("__")
        if len(path) > 2:
            raise ConfigError(f"env override {key} too deep")
        obj: Any = config
        nested_types = _NESTED[type(config)]
        for part in path[:-1]:
            field_type = nested_types.get(part)
            if field_type is None or not isinstance(getattr(obj, part), field_type):
                raise ConfigError(f"env override {key}: {part!r} is not a config section")
            obj = getattr(obj, part)
            nested_types = _NESTED.get(field_type, {})
        leaf = path[-1]
        hints = typing.get_type_hints(type(obj))
        if leaf not in hints:
            raise ConfigError(f"env override {key}: unknown field {leaf!r}")
        setattr(obj, leaf, _coerce(value, hints[leaf]))
    return config


def load_config(path: str | Path) -> AppConfig:
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"config file not found: {p}")
    raw = yaml.safe_load(p.read_text()) or {}
    try:
        cameras = [CameraConfig(**c) for c in raw.get("cameras", [])]
        ingest_raw = dict(raw.get("ingest", {}))
        reconnect = ReconnectConfig(**ingest_raw.pop("reconnect", {}))
        ingest = IngestConfig(reconnect=reconnect, **ingest_raw)
        config = AppConfig(
            timezone=raw.get("timezone", "Asia/Dhaka"),
            database=DatabaseConfig(**raw.get("database", {})),
            clips=ClipsConfig(**raw.get("clips", {})),
            ingest=ingest,
            cameras=cameras,
        )
    except TypeError as e:
        raise ConfigError(f"config file {p}: {e}") from e
    return apply_env_overrides(config)
