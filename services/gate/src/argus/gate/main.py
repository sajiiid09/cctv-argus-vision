"""Gate entrypoint.

    python -m argus.gate --config config/dev.yaml
    python -m argus.gate --config config/dev.yaml --once --badge B-1
    python -m argus.gate --config config/dev.yaml --replay-since 2026-09-18T00:00:00Z

`--once --badge` injects one simulated tap and exits, which is what a demo
needs and what a human can run to see the whole path work. With no reader on the
LAN, that is also the only way to see it work at all.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import sys
from datetime import UTC, datetime, timedelta

from argus.common.clock import SystemClock
from argus.common.config import AppConfig, ConfigError, load_config
from argus.gate.runner import GateRunner
from argus.pipelines.gate.simulated import SimulatedTapSource
from argus.pipelines.gate.taps import TapSource
from argus.store.db import Database, apply_migrations

log = logging.getLogger("argus.gate")

VERSION = "0.1.0"


def build_source(config: AppConfig) -> TapSource:
    gate = config.gate
    if gate.tap_source == "simulated":
        return SimulatedTapSource(
            reader_id=gate.reader_id, path=gate.simulated_taps_path, loop=False
        )
    from argus.pipelines.gate.zkt import ZktTapSource

    log.warning(
        "using the ZKT source, which has never exchanged a byte with a real reader "
        "(AGENTS.md §7: what we cannot test yet). Expect to debug this on the box."
    )
    return ZktTapSource(
        host=gate.host or "",
        reader_id=gate.reader_id,
        port=gate.port,
        password=gate.password,
        poll_interval_s=gate.poll_interval_s,
    )


async def run(config: AppConfig, args: argparse.Namespace) -> int:
    db = await Database.connect(config.database.dsn)
    source = build_source(config)
    try:
        await apply_migrations(db)
        # gate_event references camera, so the camera this reader watches has to
        # exist before the first tap. Upserting it here means the gate can be
        # started on its own rather than only after ingest.
        from argus.store.store import Store

        store = Store(db)
        for cam in config.cameras:
            if cam.camera_id == config.gate.camera_id:
                await store.upsert_camera(cam)
        runner = GateRunner(
            db,
            source,
            camera_id=config.gate.camera_id,
            verifier=None,  # no enrolment thresholds yet (ADR-0010)
            threshold=config.face.gate_verify_threshold,
            dedupe_window_s=config.gate.dedupe_window_s,
            reader_gap_timeout_s=config.gate.reader_gap_timeout_s,
            verify_window_s=config.gate.verify_window_s,
            clock=SystemClock(),
        )
        log.info(
            "gate version=%s source=%s reader=%s camera=%s threshold=%s",
            VERSION,
            config.gate.tap_source,
            config.gate.reader_id,
            config.gate.camera_id,
            config.face.gate_verify_threshold
            if config.face.gate_verify_threshold is not None
            else "unset (every tap will be not_attempted)",
        )

        if args.replay_since:
            since = datetime.fromisoformat(args.replay_since)
            await runner.replay_since(since)
            return 0

        if args.once:
            if not isinstance(source, SimulatedTapSource):
                raise ConfigError("--once injects a simulated tap; set gate.tap_source simulated")
            tap = source.feed(args.badge)
            record = await runner.handle(tap)
            if record is not None:
                print(f"{record.badge_id} -> {record.outcome.value}: {record.reason}")
            return 0

        if config.gate.replay_lookback_hours:
            with contextlib.suppress(NotImplementedError):
                await runner.replay_since(
                    datetime.now(UTC) - timedelta(hours=config.gate.replay_lookback_hours)
                )

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop.set)

        async def health_loop() -> None:
            while not stop.is_set():
                await runner.check_reader_health()
                await asyncio.sleep(5.0)

        async def tap_loop() -> None:
            async for tap in source.taps():
                if stop.is_set():
                    return
                await runner.handle(tap)

        tasks = [
            asyncio.create_task(tap_loop(), name="gate-taps"),
            asyncio.create_task(health_loop(), name="gate-health"),
            asyncio.create_task(stop.wait(), name="gate-stop"),
        ]
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        return 0
    finally:
        await source.close()
        await db.close()


def main() -> None:
    parser = argparse.ArgumentParser(prog="argus-gate")
    parser.add_argument("--config", required=True)
    parser.add_argument("--once", action="store_true", help="inject one simulated tap and exit")
    parser.add_argument("--badge", default="B-1", help="badge id for --once")
    parser.add_argument("--replay-since", help="recover reader-logged taps since this time")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    logging.basicConfig(
        level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    config = load_config(args.config)
    try:
        sys.exit(asyncio.run(run(config, args)))
    except ConfigError as exc:
        log.error("configuration problem: %s", exc)
        sys.exit(2)


if __name__ == "__main__":
    main()
