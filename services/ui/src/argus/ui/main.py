"""Console entrypoint.

    python -m argus.ui --config config/dev.yaml

Bound to 127.0.0.1 by ADR-0028. Binding it to the factory LAN needs an explicit
acknowledgement in config *and* a new ADR, because the console authenticates a
role rather than a person.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from argus.common.config import AppConfig, ConfigError, load_config
from argus.store.db import Database, apply_migrations
from argus.ui.app import create_app
from fastapi import FastAPI

log = logging.getLogger("argus.ui")


async def _build(config: AppConfig) -> tuple[FastAPI, Database]:
    db = await Database.connect(config.database.dsn)
    await apply_migrations(db)
    return create_app(config, db), db


def main() -> None:
    parser = argparse.ArgumentParser(prog="argus-ui")
    parser.add_argument("--config", required=True)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    logging.basicConfig(
        level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    config = load_config(args.config)
    try:
        app, _db = asyncio.get_event_loop().run_until_complete(_build(config))
    except ConfigError as exc:
        log.error("configuration problem: %s", exc)
        sys.exit(2)

    import uvicorn

    if not config.ui.role_passphrases:
        log.warning(
            "no role passphrases configured: nobody can log in. Set UI_*_PASSPHRASE in "
            "config/secrets.env (ADR-0028)"
        )
    log.info(
        "console on http://%s:%d — localhost only (ADR-0028); every clip view is logged",
        config.ui.bind_host,
        config.ui.port,
    )
    uvicorn.run(app, host=config.ui.bind_host, port=config.ui.port, log_level="info")


if __name__ == "__main__":
    main()
