"""Configuration: one file/env mechanism on every platform (ARCHITECTURE.md §5.4).

YAML file plus ``ARGUS_SECTION__KEY`` env overrides, coerced to field types.
No ad-hoc CLI arguments that only exist on one machine.

**Credentials never live in the YAML.** Values may contain ``${VAR}``, resolved
from the environment or from ``config/secrets.env`` (which must be mode 600 or
loading fails). The camera rows keep the *un-expanded* template, so an RTSP
password cannot reach the database, a log line or a git diff. Every config
object with a secret in it redacts its own repr.
"""

from __future__ import annotations

import os
import re
import stat
import typing
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml

SECRETS_PATH = "config/secrets.env"
_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
# Field names whose values are never printed.
_SECRET_FIELD = re.compile(r"password|secret|passphrase|token|credential|dsn", re.IGNORECASE)
# user:password@ inside a URI. The password is the point, but the username is
# worth hiding too: it is half of a credential.
_CREDENTIAL_URI = re.compile(r"(?<=://)[^/@\s]+:[^/@\s]+@")


class ConfigError(Exception):
    pass


def load_secrets(path: str | Path = SECRETS_PATH) -> dict[str, str]:
    """Read KEY=VALUE pairs from a mode-600 file, or return nothing.

    Absent is fine and silent: CI and containers pass real environment
    variables, and the file is a developer convenience. Present but readable by
    anyone else is fatal -- a world-readable secrets file is worse than an
    environment variable, because it looks like protection.

    No shell semantics. No `export`, no interpolation, no command substitution:
    this is a list of values, not a script.
    """
    p = Path(path)
    if not p.is_file():
        return {}
    mode = stat.S_IMODE(p.stat().st_mode)
    if mode & 0o177:
        raise ConfigError(
            f"{p} is mode {mode:o}; a secrets file must be 0600 (chmod 600 {p}). "
            "A world-readable secrets file is worse than an environment variable "
            "because it looks like protection."
        )
    values: dict[str, str] = {}
    for line_number, line in enumerate(p.read_text().splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            raise ConfigError(f"{p}:{line_number}: expected KEY=VALUE")
        key, _, value = stripped.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def expand(raw: Any, secrets: dict[str, str], *, env: dict[str, str] | None = None) -> Any:
    """Resolve ${VAR} in every string leaf.

    The environment wins over the file: containers and CI pass the real thing,
    and a stale local file that silently overrode it would be found in
    production rather than here.
    """
    environ = env if env is not None else dict(os.environ)

    def resolve(text: str) -> str:
        def one(match: re.Match[str]) -> str:
            name = match.group(1)
            if name in environ:
                return environ[name]
            if name in secrets:
                return secrets[name]
            # Name the VARIABLE, never the surrounding string: a
            # half-substituted rtsp://admin:${PW}@10.0.0.5/ in an error message
            # is a credential in a log.
            raise ConfigError(
                f"{name} is not set (put it in {SECRETS_PATH} at mode 600, or in the environment)"
            )

        return _VAR.sub(one, text)

    if isinstance(raw, str):
        return resolve(raw)
    if isinstance(raw, dict):
        return {key: expand(value, secrets, env=environ) for key, value in raw.items()}
    if isinstance(raw, list):
        return [expand(value, secrets, env=environ) for value in raw]
    return raw


def redact(name: str, value: Any) -> Any:
    """What a repr shows instead of a secret.

    Secrecy is inherited by a dict's values: `role_passphrases` is the field
    that makes them secret, and its keys are role names -- redacting per key
    would print every passphrase under a harmless-looking name.
    """
    if isinstance(value, dict):
        inherited = _SECRET_FIELD.search(name)
        return {key: redact(name if inherited else key, item) for key, item in value.items()}
    if not isinstance(value, str) or not value:
        return value
    if _SECRET_FIELD.search(name):
        return "***"
    return _CREDENTIAL_URI.sub("***:***@", value)


class RedactedRepr:
    """Mixin: a repr that cannot print a credential.

    Both ``__repr__`` and ``__str__``, because ``f"{config}"`` calls ``__str__``
    and only falls back to ``__repr__`` when ``__str__`` is undefined -- so
    defining one and not the other leaves the leak open on the path people
    actually use.
    """

    def __repr__(self) -> str:
        rendered = ", ".join(
            f"{f.name}={redact(f.name, getattr(self, f.name))!r}"
            for f in fields(self)  # type: ignore[arg-type]
        )
        return f"{type(self).__name__}({rendered})"

    __str__ = __repr__


@dataclass(slots=True, repr=False)
class CameraConfig(RedactedRepr):
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
    # The URI as written in config, ${VAR} and all. This is what is stored in the
    # camera row: an expanded one would put a password in the database, and the
    # schema refuses it anyway.
    source_uri_template: str = ""
    analysis_uri_template: str | None = None

    def __post_init__(self) -> None:
        if not self.source_uri_template:
            self.source_uri_template = self.source_uri
        if self.analysis_uri_template is None:
            self.analysis_uri_template = self.analysis_uri
        valid_roles = {"gate", "canteen_door", "floor"}
        if self.role not in valid_roles:
            raise ConfigError(
                f"camera {self.camera_id}: role must be one of {sorted(valid_roles)}, "
                f"got {self.role!r}"
            )
        if self.role == "canteen_door" and not self.space_id:
            raise ConfigError(
                f"camera {self.camera_id}: canteen_door cameras need space_id "
                "(pairing is per canteen space, ARCHITECTURE.md §7.3)"
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


@dataclass(slots=True, repr=False)
class DatabaseConfig(RedactedRepr):
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


@dataclass(slots=True, repr=False)
class UiConfig(RedactedRepr):
    """The operator console (ADR-0028).

    Bound to localhost, because the console authenticates a *role* and not a
    person. Binding it anywhere else needs `acknowledged_lan_exposure` set
    deliberately -- and a new ADR, since it changes who can reach a page full of
    clips.
    """

    bind_host: str = "127.0.0.1"
    port: int = 8080
    session_ttl_minutes: int = 120
    clip_access_dedupe_s: int = 60
    acknowledged_lan_exposure: bool = False
    # Filled from config/secrets.env via ${VAR} expansion, never written in YAML.
    role_passphrases: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        local = {"127.0.0.1", "localhost", "::1"}
        if self.bind_host not in local and not self.acknowledged_lan_exposure:
            raise ConfigError(
                f"ui.bind_host is {self.bind_host!r}, which is not localhost. The console "
                "authenticates a role, not a person (ADR-0028): exposing it needs "
                "ui.acknowledged_lan_exposure set deliberately and an ADR to say why."
            )
        if self.session_ttl_minutes <= 0:
            raise ConfigError("ui.session_ttl_minutes must be positive")
        unknown = sorted(set(self.role_passphrases) - {"viewer", "reviewer", "payroll", "admin"})
        if unknown:
            raise ConfigError(f"ui.role_passphrases has unknown roles: {unknown}")


@dataclass(slots=True, repr=False)
class GateConfig(RedactedRepr):
    """The badge reader, and how long we wait for a face after a tap.

    ``tap_source`` defaults to ``simulated`` because no reader has been on a LAN
    we can reach: the simulated source keeps the whole gate path real and says
    in every row that the tap was simulated. Switching to ``zkt`` is a config
    change and a day of hardware work, in that order.
    """

    tap_source: str = "simulated"
    reader_id: str = "gate_reader_01"
    camera_id: str = "gate_door"
    host: str | None = None
    port: int = 4370
    password: str | None = None
    simulated_taps_path: str | None = None
    poll_interval_s: float = 1.0
    dedupe_window_s: float = 5.0
    reader_gap_timeout_s: float = 30.0
    verify_window_s: float = 3.0
    replay_lookback_hours: int = 24

    def __post_init__(self) -> None:
        if self.tap_source not in ("simulated", "zkt"):
            raise ConfigError(
                f"gate.tap_source must be 'simulated' or 'zkt', got {self.tap_source!r}"
            )
        if self.tap_source == "zkt" and not self.host:
            raise ConfigError("gate.tap_source is 'zkt' but gate.host is unset")
        if self.dedupe_window_s <= 0 or self.verify_window_s <= 0:
            raise ConfigError("gate dedupe and verify windows must be positive")


@dataclass(slots=True)
class PayrollConfig:
    """Pairing policy inputs, and one field that cannot be set.

    ``shadow_mode`` refuses to be false. Leaving shadow mode is a human
    sign-off (AGENTS.md §2.1) and an ADR, not a YAML key -- and today it would
    mean nothing anyway, because there is no export to enable (ADR-0005/0006).
    A config that cannot express the dangerous state is better than a config
    that can and is trusted not to.

    There is deliberately no ``timezone`` here: the policy timezone is
    ``AppConfig.timezone``, and two places to set it is two places to disagree.
    """

    shadow_mode: bool = True
    allowance_s: int = 3600
    min_dwell_s: int = 60
    max_dwell_s: int = 10800
    duplicate_window_s: float = 3.0
    identity_threshold: float = 0.0
    clock_skew_tolerance_s: float = 60.0
    # How far before the window's start events are loaded, so an interval that
    # began earlier is not truncated into a spurious UNPAIRED_ENTER. None means
    # max_dwell_s, which is the longest an interval can legitimately be.
    lead_in_s: int | None = None

    def __post_init__(self) -> None:
        if not self.shadow_mode:
            raise ConfigError(
                "payroll.shadow_mode cannot be set false. Writing to payroll is a "
                "human sign-off (AGENTS.md §2.1) plus an ADR plus a code change -- "
                "and there is no export to enable, so `false` would claim something "
                "that is not true (ADR-0005)."
            )
        if self.allowance_s < 0 or self.min_dwell_s < 0:
            raise ConfigError("payroll allowance_s and min_dwell_s must not be negative")
        if self.max_dwell_s <= self.min_dwell_s:
            raise ConfigError(
                f"payroll.max_dwell_s ({self.max_dwell_s}) must exceed min_dwell_s "
                f"({self.min_dwell_s})"
            )
        if self.lead_in_s is not None and self.lead_in_s < 0:
            raise ConfigError("payroll.lead_in_s must not be negative")


@dataclass(slots=True)
class FaceConfig:
    """Face identity settings, with two thresholds that must never be one.

    ADR-0010: the 1:N canteen question ("who is this?") and the 1:1 gate
    question ("is this the badge holder?") are different problems, and sharing a
    constant between them is how a verification threshold silently becomes an
    identification threshold. Neither has a default: with faces enabled and no
    measured number in config, this refuses to construct. A vendor default
    becoming a production threshold is exactly what ADR-0010 forbids.
    """

    enabled: bool = False
    detector: str | None = None  # forced backend name; None = registry preference
    embedder: str | None = None
    canteen_match_threshold: float | None = None  # 1:N
    gate_verify_threshold: float | None = None  # 1:1
    near_tie_margin: float = 0.05
    min_det_score: float = 0.5
    min_box_px: float = 24.0
    min_sharpness: float = 5.0
    max_frontality: float = 0.6
    best_of_k: int = 3
    enrol_dir: str = "var/enrol"

    def __post_init__(self) -> None:
        if not self.enabled:
            return
        missing = [
            name
            for name in ("canteen_match_threshold", "gate_verify_threshold")
            if getattr(self, name) is None
        ]
        if missing:
            raise ConfigError(
                f"face.enabled is true but {', '.join(missing)} is unset. Face "
                "thresholds come from measured data, never from a default "
                "(ADR-0010): with no measurement, run with face.enabled false and "
                "every crossing stays unknown, which is the fail-open outcome."
            )
        if self.canteen_match_threshold == self.gate_verify_threshold:
            raise ConfigError(
                "face.canteen_match_threshold and face.gate_verify_threshold are "
                "identical. 1:N identification and 1:1 verification are different "
                "problems and must not share a constant (ADR-0010). If they really "
                "measured the same, set values that differ and record why."
            )
        if self.near_tie_margin < 0:
            raise ConfigError("face.near_tie_margin must not be negative")
        if self.best_of_k < 1:
            raise ConfigError("face.best_of_k must be at least 1")


@dataclass(slots=True)
class CanteenConfig:
    """Doorway geometry and clip windows for the canteen pipeline."""

    person_score: float = 0.5
    # Hysteresis band as a fraction of the frame diagonal. A zero band means one
    # jittery detection is a crossing, and a crossing is a deduction.
    band: float = 0.03
    min_samples_per_side: int = 2
    # Hard ceiling on a track's life, generous enough for someone walking in
    # from the edge of frame. Separate from the sample window below, which is
    # what "we keep geometry, not history" actually means: a 3-second ceiling
    # used to drop the tracks of slow walkers before they reached the line, and
    # the rig replay lost three crossings in ten to it.
    track_max_age_s: float = 20.0
    track_max_gap_s: float = 0.6
    track_sample_window_s: float = 4.0
    clip_pre_s: float = 6.0
    clip_post_s: float = 4.0
    clip_timeout_s: float = 12.0
    # Must match PayrollConfig.duplicate_window_s: the pipeline dedupes in
    # process and payroll dedupes again on read, and the two reading different
    # numbers is a divergence nobody would notice.
    duplicate_window_s: float = 3.0
    aim_check_interval_s: float = 30.0
    aim_max_offset_px: float = 4.0
    aim_consecutive: int = 3
    # Sustained frame dropping means the analyser is not measuring what it
    # claims to; above this fraction the pipeline opens an `overload` gap.
    max_drop_rate: float = 0.5
    drop_rate_window: int = 200

    def __post_init__(self) -> None:
        if not 0 < self.band < 0.5:
            raise ConfigError(f"canteen.band must be in (0, 0.5), got {self.band}")
        if self.min_samples_per_side < 1:
            raise ConfigError("canteen.min_samples_per_side must be at least 1")
        if self.clip_pre_s <= 0 or self.clip_post_s < 0:
            raise ConfigError("canteen clip window must be positive before the crossing")
        if self.duplicate_window_s <= 0:
            raise ConfigError("canteen.duplicate_window_s must be positive")
        if not 0 < self.max_drop_rate <= 1:
            raise ConfigError("canteen.max_drop_rate must be in (0, 1]")


@dataclass(slots=True)
class SeatConfig:
    """One seat, as a region of the frame in fractions.

    Seat regions are configuration, not code: PLAN.md M5 requires that adding a
    seat needs no code change, because a factory floor is re-laid out more often
    than software is released.
    """

    seat_id: str
    region: tuple[float, float, float, float]  # x1, y1, x2, y2 as fractions
    line_id: str | None = None

    def __post_init__(self) -> None:
        values = [float(v) for v in self.region]
        if len(values) != 4:
            raise ConfigError(f"seat {self.seat_id}: region must be [x1, y1, x2, y2]")
        if any(v < 0.0 or v > 1.0 for v in values):
            raise ConfigError(
                f"seat {self.seat_id}: region values are fractions of the frame, got {values}"
            )
        if values[0] >= values[2] or values[1] >= values[3]:
            raise ConfigError(f"seat {self.seat_id}: region must have positive area")
        self.region = (values[0], values[1], values[2], values[3])


@dataclass(slots=True)
class OccupancyConfig:
    """Anonymous per-seat occupancy (M5, ADR-0019).

    Per-seat is stored and the aggregate is shown; the per-seat view sits behind
    the admin tier. Nothing here carries a person, and a test asserts no
    occupancy code path can reach a payroll table -- seats are assigned, so
    per-seat data is closer to identified data than its schema suggests, and
    that is exactly why the boundary is enforced rather than remembered.
    """

    enabled: bool = False
    camera_id: str | None = None
    # Deliberately slow. Occupancy is a management indicator, not an event
    # stream, and a fast cadence would invite using it as one.
    sample_interval_s: float = 30.0
    # Majority vote over this many samples before a seat changes state, so a
    # person leaning out of frame for one sample is not "absent".
    smoothing_samples: int = 3
    min_overlap: float = 0.25
    seats: list[SeatConfig] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.sample_interval_s <= 0:
            raise ConfigError("occupancy.sample_interval_s must be positive")
        if self.smoothing_samples < 1:
            raise ConfigError("occupancy.smoothing_samples must be at least 1")
        if not 0 < self.min_overlap <= 1:
            raise ConfigError("occupancy.min_overlap must be in (0, 1]")
        seat_ids = [seat.seat_id for seat in self.seats]
        duplicates = sorted({s for s in seat_ids if seat_ids.count(s) > 1})
        if duplicates:
            raise ConfigError(f"occupancy seats repeated: {duplicates}")


@dataclass(slots=True)
class ViolenceConfig:
    """The cheap, recall-biased trigger (M6, ADR-0012).

    A trigger and a human, never a classifier. The weights are PROVISIONAL and
    describe the rig; the honest false-positive rate on a real factory floor is
    unmeasurable before a site pilot and is the dominant error source
    (ARCHITECTURE.md §9.2).
    """

    enabled: bool = False
    camera_id: str | None = None
    trigger_threshold: float = 0.6
    proximity_weight: float = 0.4
    extension_weight: float = 0.3
    energy_weight: float = 0.3
    # Metres-ish, in shoulder widths: two people closer than this are "close".
    proximity_shoulders: float = 1.5
    clip_pre_s: float = 6.0
    clip_post_s: float = 4.0
    # One candidate per cooldown, so a scuffle is one queue item and not forty.
    cooldown_s: float = 30.0

    def __post_init__(self) -> None:
        if not 0 < self.trigger_threshold <= 1:
            raise ConfigError("violence.trigger_threshold must be in (0, 1]")
        total = self.proximity_weight + self.extension_weight + self.energy_weight
        if abs(total - 1.0) > 1e-6:
            raise ConfigError(
                f"violence feature weights must sum to 1.0, got {total:.3f}: a weighted score "
                "whose weights do not sum to one is not comparable to a threshold"
            )
        if self.cooldown_s <= 0:
            raise ConfigError("violence.cooldown_s must be positive")


@dataclass(slots=True)
class PipelinesConfig:
    """Analysis inside the ingest process (ADR-0033). Off by default.

    Off means a box with no model artefacts runs ingest exactly as before, and
    CI's smoke test is unaffected. On means every canteen_door camera gets a
    pipeline -- and if a backend cannot be constructed, that is an error and a
    gap, because the system claimed it was measuring.
    """

    enabled: bool = False
    detector_backend: str | None = None
    pose_backend: str | None = None
    queue_depth: int = 2
    canteen: CanteenConfig = field(default_factory=CanteenConfig)
    occupancy: OccupancyConfig = field(default_factory=OccupancyConfig)
    violence: ViolenceConfig = field(default_factory=ViolenceConfig)

    def __post_init__(self) -> None:
        if self.queue_depth < 1:
            raise ConfigError("pipelines.queue_depth must be at least 1")


@dataclass(slots=True, repr=False)
class AppConfig(RedactedRepr):
    timezone: str = "Asia/Dhaka"
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    clips: ClipsConfig = field(default_factory=ClipsConfig)
    ingest: IngestConfig = field(default_factory=IngestConfig)
    pipelines: PipelinesConfig = field(default_factory=PipelinesConfig)
    payroll: PayrollConfig = field(default_factory=PayrollConfig)
    face: FaceConfig = field(default_factory=FaceConfig)
    gate: GateConfig = field(default_factory=GateConfig)
    ui: UiConfig = field(default_factory=UiConfig)
    cameras: list[CameraConfig] = field(default_factory=list)

    def __post_init__(self) -> None:
        """One dedupe window, checked where it can actually diverge.

        The defaults agreeing is not the property that matters: a YAML file
        setting one of the two is. The pipeline dedupes crossings in process and
        payroll dedupes the same events again on read, so two different windows
        mean the second pass can drop an event the first kept -- and nothing
        would report that, because both halves are behaving as configured.
        """
        if self.pipelines.canteen.duplicate_window_s != self.payroll.duplicate_window_s:
            raise ConfigError(
                "pipelines.canteen.duplicate_window_s "
                f"({self.pipelines.canteen.duplicate_window_s}) and "
                f"payroll.duplicate_window_s ({self.payroll.duplicate_window_s}) differ. "
                "They dedupe the same crossings at two stages and must be one number."
            )


_NESTED: dict[type, dict[str, type[Any]]] = {
    AppConfig: {
        "database": DatabaseConfig,
        "clips": ClipsConfig,
        "ingest": IngestConfig,
        "pipelines": PipelinesConfig,
        "payroll": PayrollConfig,
        "face": FaceConfig,
        "gate": GateConfig,
        "ui": UiConfig,
    },
    IngestConfig: {
        "reconnect": ReconnectConfig,
    },
    PipelinesConfig: {
        "canteen": CanteenConfig,
        "occupancy": OccupancyConfig,
        "violence": ViolenceConfig,
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


def load_config(
    path: str | Path, *, secrets_path: str | Path = SECRETS_PATH, env: dict[str, str] | None = None
) -> AppConfig:
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"config file not found: {p}")
    raw_text = yaml.safe_load(p.read_text()) or {}
    secrets = load_secrets(secrets_path)
    raw = expand(raw_text, secrets, env=env)
    try:
        cameras = []
        for expanded, original in zip(
            raw.get("cameras", []), raw_text.get("cameras", []), strict=True
        ):
            camera = dict(expanded)
            camera["source_uri_template"] = original.get("source_uri", "")
            camera["analysis_uri_template"] = original.get("analysis_uri")
            cameras.append(CameraConfig(**camera))
        ingest_raw = dict(raw.get("ingest", {}))
        reconnect = ReconnectConfig(**ingest_raw.pop("reconnect", {}))
        ingest = IngestConfig(reconnect=reconnect, **ingest_raw)
        pipelines_raw = dict(raw.get("pipelines", {}))
        canteen = CanteenConfig(**pipelines_raw.pop("canteen", {}))
        occupancy_raw = dict(pipelines_raw.pop("occupancy", {}))
        seats = [SeatConfig(**seat) for seat in occupancy_raw.pop("seats", [])]
        occupancy = OccupancyConfig(seats=seats, **occupancy_raw)
        violence = ViolenceConfig(**pipelines_raw.pop("violence", {}))
        pipelines = PipelinesConfig(
            canteen=canteen, occupancy=occupancy, violence=violence, **pipelines_raw
        )
        config = AppConfig(
            timezone=raw.get("timezone", "Asia/Dhaka"),
            database=DatabaseConfig(**raw.get("database", {})),
            clips=ClipsConfig(**raw.get("clips", {})),
            ingest=ingest,
            pipelines=pipelines,
            payroll=PayrollConfig(**raw.get("payroll", {})),
            face=FaceConfig(**raw.get("face", {})),
            gate=GateConfig(**raw.get("gate", {})),
            ui=UiConfig(**raw.get("ui", {})),
            cameras=cameras,
        )
    except TypeError as e:
        raise ConfigError(f"config file {p}: {e}") from e
    return apply_env_overrides(config)
