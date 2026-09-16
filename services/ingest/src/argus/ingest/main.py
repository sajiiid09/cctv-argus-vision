"""Ingest entrypoint. Same command on every platform (AGENTS.md §5):

python -m argus.ingest --config config/dev.yaml
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
from pathlib import Path

from argus.common.clock import SystemClock
from argus.common.config import AppConfig, load_config
from argus.ingest.streams import RTSPSource
from argus.store.db import Database, apply_migrations, config_hash
from argus.store.store import Store

log = logging.getLogger("argus.ingest")

VERSION = "0.1.0"


async def run(config: AppConfig, config_path: str) -> None:
    clock = SystemClock()
    db = await Database.connect(config.database.dsn)
    try:
        applied = await apply_migrations(db)
        if applied:
            log.info("applied migrations: %s", applied)
        store = Store(db)
        for cam in config.cameras:
            await store.upsert_camera(cam)
        run_id = await store.record_ingest_run(VERSION, config_hash(Path(config_path).read_text()))
        log.info(
            "ingest run=%s version=%s config=%s cameras=%d decode=%s tz=%s",
            run_id,
            VERSION,
            config_hash(Path(config_path).read_text()),
            len(config.cameras),
            config.ingest.decode,
            config.timezone,
        )
        sources = [
            RTSPSource(cam, config.ingest, clock=clock, gaps=store) for cam in config.cameras
        ]
        tasks = [asyncio.create_task(s.run(), name=f"source-{s.camera.camera_id}") for s in sources]

        async def status_logger() -> None:
            while True:
                for s in sources:
                    log.info("status %s", s.status.as_line())
                await asyncio.sleep(30)

        tasks.append(asyncio.create_task(status_logger(), name="status"))
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop_event.set)
        stop_task = asyncio.create_task(stop_event.wait(), name="stop")
        await asyncio.wait([*tasks, stop_task], return_when=asyncio.FIRST_COMPLETED)
        for s in sources:
            s.stop()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        log.info("ingest stopped; final status:")
        for s in sources:
            log.info("status %s", s.status.as_line())
    finally:
        await db.close()


def main() -> None:
    parser = argparse.ArgumentParser(prog="argus-ingest")
    parser.add_argument("--config", required=True, help="path to YAML config")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    config = load_config(args.config)
    asyncio.run(run(config, args.config))


if __name__ == "__main__":
    main()
