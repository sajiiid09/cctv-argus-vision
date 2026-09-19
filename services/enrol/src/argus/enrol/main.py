"""Enrolment CLI.

    argus-enrol person add     --person p1 --employee-ref HR-1 --by "a human"
    argus-enrol consent record --person p1 --expires 2026-12-31 --by "a human"
    argus-enrol enrol from-images --person p1 --dir ./photos --by "a human"
    argus-enrol badge assign   --person p1 --badge B-1 --by "a human"
    argus-enrol list [--with-templates|--stale|--expiring-within 30]
    argus-enrol purge --expired --by "a human" [--dry-run] [--yes]

`--by` is required on everything that writes, and has no default. `--dry-run`
prints what would happen and touches nothing.

`enrol from-rtsp` is deliberately absent. `from-images` covers a handful of
consenting volunteers, and a third RTSP connection per camera is a hazard on a
client budget nobody has measured yet (ADR-0031, RISKS.md §6).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from argus.common.config import ConfigError, load_config
from argus.enrol import commands
from argus.enrol.consent import ConsentError
from argus.enrol.images import ImageError, ensure_root
from argus.enrol.templates import EnrolmentError
from argus.store.db import Database, apply_migrations

log = logging.getLogger("argus.enrol")

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_REFUSED = 5


def _face_backends(config: Any) -> tuple[Any, Any]:
    """The detector and embedder templates will be created with.

    Asked for by name from config if set, otherwise the registry's preference
    order. A missing artefact fails here, at startup, with the model's own
    message -- not halfway through a roster.
    """
    from argus.backends.registry import get_face_detector, get_face_embedder

    detector = get_face_detector(config.face.detector)
    embedder = get_face_embedder(config.face.embedder)
    return detector, embedder


async def _dispatch(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    root = Path(config.face.enrol_dir)
    today = date.today()
    now = datetime.now(UTC)

    db = await Database.connect(config.database.dsn)
    try:
        await apply_migrations(db)
        if args.command == "person":
            report = await commands.person_add(
                db,
                person_id=args.person,
                employee_ref=args.employee_ref,
                display_name=args.name,
                line_id=args.line,
                seat_id=args.seat,
                active_from=date.fromisoformat(args.active_from)
                if args.active_from
                else today,
                by=args.by,
                dry_run=args.dry_run,
            )
        elif args.command == "consent" and args.consent_action == "record":
            report = await commands.consent_record(
                db,
                person_id=args.person,
                purpose=args.purpose,
                granted_on=date.fromisoformat(args.granted) if args.granted else today,
                expires_on=date.fromisoformat(args.expires),
                evidence_note=args.evidence,
                by=args.by,
                dry_run=args.dry_run,
            )
        elif args.command == "consent" and args.consent_action == "revoke":
            report = await commands.consent_revoke(
                db, person_id=args.person, by=args.by, when=now, dry_run=args.dry_run
            )
        elif args.command == "enrol":
            ensure_root(root)
            detector, embedder = _face_backends(config)
            report = await commands.enrol_from_images(
                db,
                person_id=args.person,
                directory=Path(args.dir),
                root=root,
                detector=detector,
                embedder=embedder,
                today=today,
                match_threshold=config.face.canteen_match_threshold,
                min_det_score=config.face.min_det_score,
                by=args.by,
                force=args.force,
                dry_run=args.dry_run,
            )
        elif args.command == "badge" and args.badge_action == "assign":
            report = await commands.badge_assign(
                db,
                person_id=args.person,
                badge_id=args.badge,
                when=now,
                by=args.by,
                dry_run=args.dry_run,
            )
        elif args.command == "badge" and args.badge_action == "unassign":
            report = await commands.badge_unassign(
                db, badge_id=args.badge, when=now, by=args.by, dry_run=args.dry_run
            )
        elif args.command == "list":
            model_ref = None
            if args.stale:
                try:
                    _detector, embedder = _face_backends(config)
                    model_ref = embedder.model_ref
                except Exception as exc:  # no artefacts is a normal state here
                    log.warning("no embedder available: %s", exc)
            report = await commands.list_people(
                db,
                live_model_ref=model_ref,
                with_templates=args.with_templates,
                stale=args.stale,
                expiring_within=args.expiring_within,
                today=today,
            )
        elif args.command == "purge":
            report = await commands.purge_expired(
                db, root=root, today=today, by=args.by, dry_run=args.dry_run
            )
        else:  # pragma: no cover - argparse rejects anything else first
            raise ConfigError(f"unknown command {args.command}")
        print(report.render())
        return EXIT_OK
    finally:
        await db.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="argus-enrol")
    parser.add_argument("--config", required=True)
    parser.add_argument("--log-level", default="INFO")
    sub = parser.add_subparsers(dest="command", required=True)

    def writing(p: argparse.ArgumentParser) -> None:
        # No default, ever. An inferred actor is a fake audit trail.
        p.add_argument("--by", required=True, help="the human responsible; no default")
        p.add_argument("--dry-run", action="store_true")

    person = sub.add_parser("person", help="add somebody to the roster")
    person.add_argument("person_action", choices=["add"])
    person.add_argument("--person", required=True)
    person.add_argument("--employee-ref", required=True)
    person.add_argument("--name")
    person.add_argument("--line")
    person.add_argument("--seat")
    person.add_argument("--active-from")
    writing(person)

    consent = sub.add_parser("consent", help="record or withdraw consent")
    consent.add_argument("consent_action", choices=["record", "revoke"])
    consent.add_argument("--person", required=True)
    consent.add_argument("--purpose", default="canteen and gate face recognition")
    consent.add_argument("--granted")
    consent.add_argument("--expires")
    consent.add_argument("--evidence")
    writing(consent)

    enrol = sub.add_parser("enrol", help="create face templates from photographs")
    enrol.add_argument("enrol_action", choices=["from-images"])
    enrol.add_argument("--person", required=True)
    enrol.add_argument("--dir", required=True)
    enrol.add_argument(
        "--force",
        action="store_true",
        help="enrol even if this face already belongs to somebody else",
    )
    writing(enrol)

    badge = sub.add_parser("badge", help="assign or unassign a badge")
    badge.add_argument("badge_action", choices=["assign", "unassign"])
    badge.add_argument("--person")
    badge.add_argument("--badge", required=True)
    writing(badge)

    listing = sub.add_parser("list", help="who is enrolled, and how healthily")
    listing.add_argument("--with-templates", action="store_true")
    listing.add_argument("--stale", action="store_true", help="templates from another model")
    listing.add_argument("--expiring-within", type=int, metavar="DAYS")

    purge = sub.add_parser("purge", help="delete templates and images without live consent")
    purge.add_argument("--expired", action="store_true", required=True)
    purge.add_argument("--yes", action="store_true", help="required unless --dry-run")
    writing(purge)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    logging.basicConfig(
        level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    if args.command == "consent" and args.consent_action == "record" and not args.expires:
        parser.error("consent record needs --expires: consent without an end is not consent")
    if args.command == "badge" and args.badge_action == "assign" and not args.person:
        parser.error("badge assign needs --person")
    if args.command == "purge" and not args.dry_run and not args.yes:
        parser.error("purge deletes biometric data: pass --yes, or --dry-run to see the plan")

    try:
        sys.exit(asyncio.run(_dispatch(args)))
    except (ConsentError, EnrolmentError, ImageError) as exc:
        log.error("%s", exc)
        sys.exit(EXIT_REFUSED)
    except ConfigError as exc:
        log.error("configuration problem: %s", exc)
        sys.exit(EXIT_USAGE)


if __name__ == "__main__":
    main()
