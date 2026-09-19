"""Pairing runner.

    python -m argus.pairing --config config/dev.yaml --once --day 2026-09-18
    python -m argus.pairing --config config/dev.yaml --once --from ... --to ...

`--day` is a **local** day in the policy timezone, converted once, here.

`--watch` is the agreed cut. Its shape, for whoever adds it: subscribe to the
`argus_events` channel via `argus.store.bus.EventBus`, filter for
`doorway_event`, debounce a few seconds, and re-run the affected local day. It
is about forty lines and it is not the reason anything is blocked.

Exit codes: 0 ok, 2 config or policy error, 3 database error, 4 the window was
empty (not a failure -- a distinct code so a wrapper can tell).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from argus.common.clock import SystemClock
from argus.common.config import ConfigError, load_config
from argus.pairing.load import LoadError, Window, canteen_spaces, load_window
from argus.pairing.persist import summarise, write_run
from argus.pairing.policy import lead_in_seconds, policy_from_config
from argus.payroll import PolicyError, run_pairing
from argus.store.db import Database, apply_migrations, config_hash

log = logging.getLogger("argus.pairing")

VERSION = "0.1.0"

EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_DATABASE = 3
EXIT_EMPTY = 4


def local_day_window(day: date, timezone: str) -> tuple[datetime, datetime]:
    """The UTC span of one local day.

    Pay periods and the canteen allowance are local-day concepts and the store
    is UTC, so the conversion happens once, in one place (ARCHITECTURE.md §7.1).
    """
    zone = ZoneInfo(timezone)
    start_local = datetime.combine(day, datetime.min.time(), tzinfo=zone)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(UTC), end_local.astimezone(UTC)


async def run_once(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    policy = policy_from_config(config)
    lead_in = lead_in_seconds(config, policy)

    if args.day is not None:
        from_utc, to_utc = local_day_window(date.fromisoformat(args.day), config.timezone)
    else:
        from_utc = datetime.fromisoformat(args.from_utc)
        to_utc = datetime.fromisoformat(args.to_utc)
        if from_utc.tzinfo is None or to_utc.tzinfo is None:
            raise ConfigError("--from and --to must carry a timezone offset")
    if to_utc <= from_utc:
        raise ConfigError(f"window end {to_utc.isoformat()} is not after its start")

    db = await Database.connect(config.database.dsn)
    try:
        await apply_migrations(db)
        spaces = tuple(args.space) if args.space else await canteen_spaces(db)
        window = Window(from_utc=from_utc, to_utc=to_utc, lead_in_s=lead_in, space_ids=spaces)
        log.info(
            "pairing version=%s logic=%s policy=%s window=%s..%s lead_in=%ds spaces=%s "
            "shadow_mode=true",
            VERSION,
            run_pairing.__module__,
            policy.fingerprint(),
            from_utc.isoformat(),
            to_utc.isoformat(),
            lead_in,
            ",".join(spaces) or "all",
        )
        log.info("policy: %s", policy.describe())

        evidence = await load_window(db, window)
        if not evidence.events:
            log.info("no events in the window; nothing to pair")
            return EXIT_EMPTY

        result = run_pairing(evidence.events, evidence.gaps, policy, SystemClock())
        for line in summarise(result):
            log.info("%s", line)

        if args.dry_run:
            log.info("--dry-run: nothing written, no run recorded")
            return EXIT_OK

        run_id = await write_run(
            db,
            result,
            evidence,
            policy,
            code_version=VERSION,
            config_hash=config_hash(Path(args.config).read_text()),
            actor=args.actor,
            reason=args.reason,
        )
        log.info("wrote pairing run %s", run_id)
        return EXIT_OK
    finally:
        await db.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="argus-pairing")
    parser.add_argument("--config", required=True, help="path to YAML config")
    parser.add_argument(
        "--once", action="store_true", help="compute one window and exit (the only mode)"
    )
    parser.add_argument("--day", help="local day in the policy timezone, YYYY-MM-DD")
    parser.add_argument("--from", dest="from_utc", help="window start, ISO 8601 with offset")
    parser.add_argument("--to", dest="to_utc", help="window end, ISO 8601 with offset")
    parser.add_argument("--space", action="append", help="limit to one space (repeatable)")
    parser.add_argument(
        "--dry-run", action="store_true", help="print the summary and write nothing at all"
    )
    parser.add_argument("--actor", help="who asked for this run, recorded on it")
    parser.add_argument("--reason", help="why, recorded on the run")
    parser.add_argument("--log-level", default="INFO")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if not args.once:
        parser.error("--once is required; --watch is not built (see the module docstring)")
    if (args.day is None) == (args.from_utc is None):
        parser.error("give either --day or both --from and --to")
    if args.day is None and args.to_utc is None:
        parser.error("--from requires --to")

    try:
        sys.exit(asyncio.run(run_once(args)))
    except (ConfigError, PolicyError, ValueError) as exc:
        log.error("configuration problem: %s", exc)
        sys.exit(EXIT_CONFIG)
    except LoadError as exc:
        log.error("evidence problem: %s", exc)
        sys.exit(EXIT_DATABASE)
    except OSError as exc:
        log.error("database problem: %s", exc)
        sys.exit(EXIT_DATABASE)


if __name__ == "__main__":
    main()
