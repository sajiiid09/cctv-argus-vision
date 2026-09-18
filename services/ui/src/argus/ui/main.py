"""Console entrypoint.

    python -m argus.ui --config config/dev.yaml

Bound to 127.0.0.1 by ADR-0028. Binding it to the factory LAN needs an explicit
acknowledgement in config *and* a new ADR, because the console authenticates a
role rather than a person.
"""

from __future__ import annotations

import argparse
import logging
import sys

from argus.common.config import ConfigError, load_config
from argus.ui.app import create_app

log = logging.getLogger("argus.ui")


def main() -> None:
    parser = argparse.ArgumentParser(prog="argus-ui")
    parser.add_argument("--config", required=True)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    logging.basicConfig(
        level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        log.error("configuration problem: %s", exc)
        sys.exit(2)

    import uvicorn

    # The database connection is opened in the app's lifespan, inside uvicorn's
    # own event loop. A connection made out here would belong to a loop uvicorn
    # never runs.
    app = create_app(config)

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
