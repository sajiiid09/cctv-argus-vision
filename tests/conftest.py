"""Shared fixtures. Docker-aware: tests that need Postgres or the rig skip
loudly (never silently) when the services cannot be reached.

Bring-up on a dev box:  docker compose -f infra/compose.dev.yaml up -d
Footage:                uv run python rig/synthetic/generate.py
Serve the rig:          uv run python rig/bin/rig_serve.py &
"""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for p in (
    ROOT / "tests",
    ROOT / "tests" / "golden",
    ROOT / "rig" / "bin",
    ROOT / "rig" / "synthetic",
):
    sys.path.insert(0, str(p))

from argus.common.config import CameraConfig  # noqa: E402
from argus.ingest.streams import RTSPSource  # noqa: E402
from argus.store.db import Database, apply_migrations  # noqa: E402
from argus.store.store import Store  # noqa: E402


def _default_dsn() -> str:
    return os.environ.get(
        "ARGUS_TEST_DSN", f"postgresql://argus:argus@localhost:{_pg_port()}/argus"
    )


def _pg_port() -> int:
    return int(os.environ.get("ARGUS_PG_HOST_PORT", "5432"))


TEST_DSN = _default_dsn()


def _port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _try_compose_up(service: str) -> bool:
    try:
        subprocess.run(
            ["docker", "compose", "-f", "infra/compose.dev.yaml", "up", "-d", service],
            check=True,
            capture_output=True,
            timeout=120,
        )
        return True
    except (subprocess.SubprocessError, FileNotFoundError):
        return False


async def _wait_port(port: int, seconds: float = 30.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while loop.time() < deadline:
        if await asyncio.to_thread(_port_open, "localhost", port):
            return True
        await asyncio.sleep(0.5)
    return False


@pytest.fixture(scope="session")
async def postgres_db():
    port = _pg_port()
    if not await asyncio.to_thread(_port_open, "localhost", port):
        _try_compose_up("postgres")
    if not await _wait_port(port, seconds=60):
        pytest.skip(
            "Postgres unreachable: docker compose -f infra/compose.dev.yaml up -d postgres"
            f" (host port {port})"
        )
    db = None
    last_error: Exception | None = None
    for _ in range(20):  # port open != ready to accept queries (CI services)
        try:
            db = await Database.connect(TEST_DSN)
            break
        except Exception as e:
            last_error = e
            await asyncio.sleep(1.0)
    if db is None:
        pytest.skip(f"Postgres accepting connections failed: {last_error}")
    try:
        yield db
    finally:
        await db.close()


async def _truncate_everything(db) -> None:
    """Empty every table the schema declares, derived rather than listed.

    A hand-maintained list goes stale the moment a migration lands, and the
    symptom is last test's rows leaking into next test's assertions -- which
    shows up as an order-dependent failure somewhere else entirely.
    """
    rows = await db.fetch_all(
        "select table_name from information_schema.tables"
        " where table_schema = 'public' and table_type = 'BASE TABLE'"
        " and table_name <> '_migration'"
    )
    names = sorted(r[0] for r in rows)
    if not names:
        return
    await db.execute(f"truncate table {', '.join(names)} restart identity cascade")


@pytest.fixture
async def store(postgres_db):
    await apply_migrations(postgres_db)
    await _truncate_everything(postgres_db)
    yield Store(postgres_db)


@pytest.fixture(scope="session")
async def rig():
    """Mediamtx + generated footage + two served streams, with fault control."""
    if not await asyncio.to_thread(_port_open, "localhost", 8554):
        _try_compose_up("mediamtx")
    if not await _wait_port(8554, seconds=60):
        pytest.skip("mediamtx unreachable: docker compose -f infra/compose.dev.yaml up -d mediamtx")

    from generate import generate

    footage = ROOT / "rig" / "footage" / "canteen_door_01.mp4"
    if not footage.exists():
        generate(["canteen_door_01", "canteen_door_02"])

    import rig_rigctl as rigctl

    for name in ("canteen_door_01", "canteen_door_02"):
        spec = rigctl.find(name)
        rigctl.send(spec, __import__("signal").SIGKILL)
    await asyncio.sleep(0.5)
    for name in ("canteen_door_01", "canteen_door_02"):
        rigctl.find(name)  # ensure manifests resolve
        spec = rigctl.find(name)
        rigctl.spawn(spec)
    if not await _wait_publisher("canteen_door_01", seconds=20):
        pytest.skip("rig failed to publish (ffmpeg missing or footage bad)")
    yield rigctl
    for name in ("canteen_door_01", "canteen_door_02"):
        spec = rigctl.find(name)
        rigctl.send(spec, __import__("signal").SIGKILL)


async def _wait_publisher(stream: str, seconds: float) -> bool:
    import av

    deadline = asyncio.get_running_loop().time() + seconds
    while asyncio.get_running_loop().time() < deadline:
        try:
            container = await asyncio.to_thread(
                av.open,
                f"rtsp://localhost:8554/{stream}",
                options={"rtsp_transport": "tcp"},
                timeout=3.0,
            )
            container.close()
            return True
        except Exception:
            await asyncio.sleep(0.5)
    return False


def make_door_camera(camera_id: str = "canteen_door_01", uri: str | None = None) -> CameraConfig:
    return CameraConfig(
        camera_id=camera_id,
        role="canteen_door",
        space_id="canteen",
        door_id="c1",
        direction_hint="east",
        source_uri=uri or f"rtsp://localhost:8554/{camera_id}",
        is_virtual=True,
    )


async def insert_person(
    db,
    person_id: str = "p1",
    *,
    employee_ref: str | None = None,
    active_from: str = "2026-01-01",
) -> str:
    """A roster row, because doorway_event.person_id is a real foreign key.

    Evidence attributed to a person_id that is not on the roster is invisible in
    a report and impossible to dispute, so the database refuses it (0004).
    Unknown stays expressible as null.
    """
    await db.execute(
        "insert into person (person_id, employee_ref, active_from) values (%s, %s, %s)"
        " on conflict (person_id) do nothing",
        (person_id, employee_ref or f"hr-{person_id}", active_from),
    )
    return person_id


@pytest.fixture
async def person(store):
    return await insert_person(store.db)


@pytest.fixture
async def source(store):
    """One RTSPSource bound to the persisted store; auto-stopped."""
    from argus.common.config import IngestConfig, ReconnectConfig

    cfg = IngestConfig(
        stall_timeout_s=2.0,
        reconnect=ReconnectConfig(initial_s=0.3, max_s=2.0, jitter=0.2),
        ring_buffer_seconds=10.0,
    )
    await store.upsert_camera(make_door_camera())
    src = RTSPSource(make_door_camera(), cfg, gaps=store)
    yield src
    src.stop()
