from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from argus.common.clock import DHAKA, FixedClock, local_day, policy_timezone, to_local
from argus.common.config import (
    AppConfig,
    CameraConfig,
    ConfigError,
    apply_env_overrides,
    load_config,
)

ROOT = Path(__file__).resolve().parents[2]


class TestClock:
    def test_fixed_clock_requires_tz(self) -> None:
        with pytest.raises(ValueError):
            FixedClock(datetime(2026, 9, 16, 12, 0))

    def test_local_day_boundary_dhaka(self) -> None:
        tz = policy_timezone(DHAKA)
        # 18:00 UTC == 00:00 next day in Dhaka (UTC+6, no DST)
        ts = datetime(2026, 9, 16, 18, 0, tzinfo=UTC)
        assert local_day(ts, tz) == datetime(2026, 9, 17).date()
        # 17:59 UTC is still the previous local day
        ts_prev = ts - timedelta(minutes=1)
        assert local_day(ts_prev, tz) == datetime(2026, 9, 16).date()

    def test_to_local_rejects_naive(self) -> None:
        with pytest.raises(ValueError):
            to_local(datetime(2026, 9, 16, 12, 0), policy_timezone(DHAKA))

    def test_fixed_clock_advance(self) -> None:
        clock = FixedClock(datetime(2026, 9, 16, 6, 0, tzinfo=UTC))
        clock.advance(90)  # seconds
        assert clock.now_utc() == datetime(2026, 9, 16, 6, 1, 30, tzinfo=UTC)
        assert clock.monotonic() == 90.0


class TestConfig:
    def test_load_dev_config(self) -> None:
        cfg = load_config(ROOT / "config" / "dev.yaml")
        assert cfg.timezone == DHAKA
        assert cfg.database.dsn.startswith("postgresql://")
        ids = [c.camera_id for c in cfg.cameras]
        assert ids == ["canteen_door_01", "canteen_door_02", "gate_door", "floor_view"]
        door = cfg.cameras[0]
        assert door.is_virtual is True
        assert door.space_id == "canteen"
        assert door.role == "canteen_door"

    def test_env_override(self) -> None:
        cfg = AppConfig()
        cfg = apply_env_overrides(
            cfg,
            {"ARGUS_DATABASE__DSN": "postgresql://x", "ARGUS_INGEST__STALL_TIMEOUT_S": "9"},
        )
        assert cfg.database.dsn == "postgresql://x"
        assert cfg.ingest.stall_timeout_s == 9.0

    def test_env_override_unknown_field(self) -> None:
        with pytest.raises(ConfigError):
            apply_env_overrides(AppConfig(), {"ARGUS_DATABASE__NOPE": "1"})

    def test_bad_role_rejected(self) -> None:
        with pytest.raises(ConfigError):
            CameraConfig(camera_id="x", role="ceiling_fan", source_uri="rtsp://x")

    def test_canteen_door_needs_space(self) -> None:
        with pytest.raises(ConfigError, match="space_id"):
            CameraConfig(camera_id="x", role="canteen_door", source_uri="rtsp://x")

    def test_missing_file(self) -> None:
        with pytest.raises(ConfigError):
            load_config("/nonexistent/config.yaml")
