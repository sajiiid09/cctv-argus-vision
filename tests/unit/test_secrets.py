"""Credentials: where they come from, and everywhere they must not appear.

The failure this file exists to prevent is mundane and permanent: an RTSP
password in a git diff, a log line, a database row, or an exception message.
Once it has happened it cannot be un-happened, so each path is tested rather
than reviewed.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from argus.common.config import (
    AppConfig,
    CameraConfig,
    ConfigError,
    GateConfig,
    UiConfig,
    expand,
    load_config,
    load_secrets,
    redact,
)

SECRET = "hunter2"


def _write(tmp_path: Path, name: str, text: str, mode: int = 0o600) -> Path:
    path = tmp_path / name
    path.write_text(text)
    path.chmod(mode)
    return path


def _config_yaml(tmp_path: Path) -> Path:
    return _write(
        tmp_path,
        "dev.yaml",
        """
timezone: Asia/Dhaka
database:
  dsn: postgresql://argus:${DB_PASSWORD}@localhost:5432/argus
gate:
  tap_source: zkt
  host: 10.0.0.9
  password: ${ZKT_PASSWORD}
ui:
  role_passphrases:
    reviewer: ${UI_REVIEWER_PASSPHRASE}
cameras:
  - camera_id: gate_door
    role: gate
    door_id: g1
    source_uri: rtsp://viewer:${CAMERA_PASSWORD}@10.0.0.5/main
""",
        mode=0o644,
    )


def _secrets(tmp_path: Path, mode: int = 0o600) -> Path:
    return _write(
        tmp_path,
        "secrets.env",
        f"""
# a comment
DB_PASSWORD={SECRET}
ZKT_PASSWORD={SECRET}
CAMERA_PASSWORD={SECRET}
UI_REVIEWER_PASSPHRASE="{SECRET}"
""",
        mode=mode,
    )


def test_a_secrets_file_must_be_unreadable_by_anyone_else(tmp_path) -> None:
    loose = _secrets(tmp_path, mode=0o644)
    with pytest.raises(ConfigError, match="must be 0600"):
        load_secrets(loose)


def test_the_refusal_does_not_print_the_secrets_it_refused(tmp_path) -> None:
    loose = _secrets(tmp_path, mode=0o644)
    with pytest.raises(ConfigError) as caught:
        load_secrets(loose)
    assert SECRET not in str(caught.value)


def test_a_missing_secrets_file_is_fine_and_silent(tmp_path) -> None:
    """CI and containers pass real environment variables."""
    assert load_secrets(tmp_path / "nothing.env") == {}


def test_quotes_are_stripped_and_comments_ignored(tmp_path) -> None:
    values = load_secrets(_secrets(tmp_path))
    assert values["UI_REVIEWER_PASSPHRASE"] == SECRET
    assert "#" not in "".join(values)


def test_the_environment_wins_over_the_file(tmp_path) -> None:
    """A stale local file that silently overrode the environment would be found
    in production rather than here."""
    values = load_secrets(_secrets(tmp_path))
    expanded = expand({"dsn": "${DB_PASSWORD}"}, values, env={"DB_PASSWORD": "from-env"})
    assert expanded == {"dsn": "from-env"}


def test_an_unset_variable_names_the_variable_and_not_the_string() -> None:
    """A half-substituted rtsp://admin:${PW}@10.0.0.5/ in an error message is a
    credential in a log."""
    with pytest.raises(ConfigError) as caught:
        expand({"uri": "rtsp://admin:${MISSING_PW}@10.0.0.5/main"}, {}, env={})
    message = str(caught.value)
    assert "MISSING_PW is not set" in message
    assert "10.0.0.5" not in message and "admin" not in message


def test_expansion_reaches_nested_values(tmp_path) -> None:
    values = load_secrets(_secrets(tmp_path))
    config = load_config(_config_yaml(tmp_path), secrets_path=values and _secrets(tmp_path), env={})
    assert config.gate.password == SECRET
    assert config.ui.role_passphrases["reviewer"] == SECRET
    assert config.cameras[0].source_uri == f"rtsp://viewer:{SECRET}@10.0.0.5/main"


def test_the_camera_row_keeps_the_template_not_the_password(tmp_path) -> None:
    config = load_config(_config_yaml(tmp_path), secrets_path=_secrets(tmp_path), env={})
    camera = config.cameras[0]
    assert camera.source_uri_template == "rtsp://viewer:${CAMERA_PASSWORD}@10.0.0.5/main"
    assert SECRET not in camera.source_uri_template


def test_no_repr_anywhere_in_the_config_prints_a_secret(tmp_path) -> None:
    config = load_config(_config_yaml(tmp_path), secrets_path=_secrets(tmp_path), env={})
    for rendered in (
        repr(config),
        str(config),
        repr(config.database),
        str(config.database),
        repr(config.gate),
        repr(config.ui),
        repr(config.cameras[0]),
        f"{config}",
        f"{config.cameras[0]}",
    ):
        assert SECRET not in rendered, rendered[:120]
    assert "***" in repr(config.database)
    assert "***:***@" in repr(config.cameras[0])


def test_a_startup_banner_cannot_leak_one(tmp_path, caplog) -> None:
    config = load_config(_config_yaml(tmp_path), secrets_path=_secrets(tmp_path), env={})
    log = logging.getLogger("argus.test")
    with caplog.at_level(logging.INFO):
        # The careless thing somebody will eventually write.
        log.info("starting with %s", config)
        log.info("camera %s", config.cameras[0])
        log.info("gate %r", config.gate)
    assert SECRET not in caplog.text


def test_the_store_writes_the_template(tmp_path) -> None:
    """The database refuses a credential-bearing URI too (0003), so this is the
    belt to that braces."""
    camera = CameraConfig(
        camera_id="gate_door",
        role="gate",
        source_uri=f"rtsp://viewer:{SECRET}@10.0.0.5/main",
        source_uri_template="rtsp://viewer:${CAMERA_PASSWORD}@10.0.0.5/main",
    )
    from argus.store.store import Store

    recorded: list[tuple] = []

    class _FakeDb:
        async def execute(self, sql: str, params: tuple = ()) -> None:
            recorded.append(params)

    import asyncio

    asyncio.run(Store(_FakeDb()).upsert_camera(camera))  # type: ignore[arg-type]
    assert recorded
    assert SECRET not in str(recorded[0])
    assert "${CAMERA_PASSWORD}" in str(recorded[0])


def test_redaction_covers_fields_and_uris() -> None:
    assert redact("password", SECRET) == "***"
    assert redact("gate_verify_threshold", "0.4") == "0.4"
    assert redact("source_uri", f"rtsp://u:{SECRET}@h/s") == "rtsp://***:***@h/s"
    assert redact("role_passphrases", {"admin": SECRET}) == {"admin": "***"}


def test_secrets_env_is_git_ignored_and_the_example_is_not() -> None:
    root = Path(__file__).resolve().parents[2]
    assert "config/secrets.env" in (root / ".gitignore").read_text()
    example = root / "config" / "secrets.env.example"
    assert example.is_file()
    text = example.read_text()
    # An example with a value in it is a credential somebody will commit.
    assert "PASSPHRASE=" in text
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        assert line.endswith("="), f"{line!r} has a value in it"


def test_a_lan_bound_console_needs_an_explicit_acknowledgement() -> None:
    """ADR-0028 binds the console to localhost because it authenticates a role."""
    with pytest.raises(ConfigError, match="not localhost"):
        UiConfig(bind_host="0.0.0.0")
    assert UiConfig(bind_host="0.0.0.0", acknowledged_lan_exposure=True)


def test_a_zkt_gate_without_a_host_is_refused() -> None:
    with pytest.raises(ConfigError, match=r"gate\.host is unset"):
        GateConfig(tap_source="zkt")


def test_an_empty_config_still_loads_with_safe_defaults(tmp_path) -> None:
    path = _write(tmp_path, "empty.yaml", "{}\n", mode=0o644)
    config = load_config(path, secrets_path=tmp_path / "none.env", env={})
    assert config == AppConfig() or config.timezone == "Asia/Dhaka"
    assert config.payroll.shadow_mode is True
    assert config.pipelines.enabled is False
    assert config.face.enabled is False
