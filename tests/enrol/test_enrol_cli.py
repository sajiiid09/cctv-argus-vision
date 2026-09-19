"""The enrolment commands end to end, with the mock face backends.

What these check is the discipline around the model, not the model: consent
enforced, an actor recorded, images at 0700, purge taking templates and images
together, and a duplicate identity refused.
"""

from __future__ import annotations

import stat
from datetime import UTC, date, datetime
from pathlib import Path

import numpy as np
import pytest
from argus.backends.mock import MockFaceDetector, MockFaceEmbedder
from argus.enrol import commands
from argus.enrol.consent import ConsentMissing
from argus.enrol.images import ensure_root, read_image, store_image
from argus.enrol.templates import DuplicateIdentity, EnrolmentError

pytestmark = pytest.mark.postgres

TODAY = date(2026, 9, 18)
NOW = datetime(2026, 9, 18, 6, 0, tzinfo=UTC)


def _write_face(path: Path, *, cx: int = 160, shade: int = 190, radius: int = 16) -> Path:
    """A PNG with one bright blob, which is a face to the mock detector.

    Radius 16, like the rig's people: the mock's clustering splits a blob wider
    than twice its cluster radius, so a bigger disk arrives as several "faces"
    and enrolment refuses it as a group photograph. Which is the refusal
    working, but it makes for a confusing fixture.
    """
    import av

    frame = np.full((180, 320, 3), 44, dtype=np.uint8)
    ys, xs = np.ogrid[:180, :320]
    frame[(xs - cx) ** 2 + (ys - 90) ** 2 <= radius**2] = shade
    with av.open(str(path), "w") as container:
        stream = container.add_stream("png", rate=1)
        stream.width, stream.height, stream.pix_fmt = 320, 180, "rgb24"
        container.mux(stream.encode(av.VideoFrame.from_ndarray(frame, format="rgb24")))
        container.mux(stream.encode(None))
    return path


async def _person(store, person_id: str = "p1", *, consent_to: date = date(2026, 12, 31)):
    await commands.person_add(
        store.db,
        person_id=person_id,
        employee_ref=f"HR-{person_id}",
        display_name=None,
        line_id=None,
        seat_id=None,
        active_from=date(2026, 1, 1),
        by="a human",
        dry_run=False,
    )
    await commands.consent_record(
        store.db,
        person_id=person_id,
        purpose="canteen",
        granted_on=date(2026, 9, 1),
        expires_on=consent_to,
        evidence_note=None,
        by="a human",
        dry_run=False,
    )
    return person_id


async def _enrol(store, person_id: str, directory: Path, root: Path, **kw):
    return await commands.enrol_from_images(
        store.db,
        person_id=person_id,
        directory=directory,
        root=root,
        detector=MockFaceDetector(),
        embedder=MockFaceEmbedder(),
        today=TODAY,
        match_threshold=kw.pop("match_threshold", 0.9),
        min_det_score=0.5,
        by=kw.pop("by", "a human"),
        force=kw.pop("force", False),
        dry_run=kw.pop("dry_run", False),
    )


async def test_enrolment_writes_a_template_an_image_and_an_audit_row(store, tmp_path) -> None:
    person = await _person(store)
    photos = tmp_path / "photos"
    photos.mkdir()
    _write_face(photos / "one.png")
    root = tmp_path / "var" / "enrol"

    report = await _enrol(store, person, photos, root)
    assert report.changed
    templates = await store.db.fetch_all(
        "select person_id, model_ref, dim, enrolled_by, source from face_template"
    )
    assert templates == [(person, "mock-embed@000000000000", 64, "a human", "images")]
    images = await store.db.fetch_all("select person_id, added_by from enrol_image")
    assert images == [(person, "a human")]
    actions = await store.db.fetch_all("select actor, action from enrolment_action order by at")
    assert ("a human", "enrol") in actions


async def test_enrolment_without_consent_is_refused(store, tmp_path) -> None:
    await commands.person_add(
        store.db,
        person_id="p-nocons",
        employee_ref="HR-nocons",
        display_name=None,
        line_id=None,
        seat_id=None,
        active_from=date(2026, 1, 1),
        by="a human",
        dry_run=False,
    )
    photos = tmp_path / "photos"
    photos.mkdir()
    _write_face(photos / "one.png")
    with pytest.raises(ConsentMissing):
        await _enrol(store, "p-nocons", photos, tmp_path / "var" / "enrol")
    assert await store.db.fetch_all("select count(*) from face_template") == [(0,)]


async def test_dry_run_writes_nothing_at_all(store, tmp_path) -> None:
    person = await _person(store)
    photos = tmp_path / "photos"
    photos.mkdir()
    _write_face(photos / "one.png")
    root = tmp_path / "var" / "enrol"
    report = await _enrol(store, person, photos, root, dry_run=True)
    assert not report.changed
    assert "not written" in report.render()
    assert await store.db.fetch_all("select count(*) from face_template") == [(0,)]
    assert not (root / person).exists()


async def test_the_same_photograph_twice_is_idempotent(store, tmp_path) -> None:
    person = await _person(store)
    photos = tmp_path / "photos"
    photos.mkdir()
    _write_face(photos / "one.png")
    root = tmp_path / "var" / "enrol"
    await _enrol(store, person, photos, root)
    await _enrol(store, person, photos, root)
    # The image is stored once (its name is its hash); the template is created
    # again, which is honest -- a second enrolment is a second decision.
    images = await store.db.fetch_all("select count(*) from enrol_image")
    assert images == [(1,)]
    files = list((root / person).iterdir())
    assert len(files) == 1


async def test_images_are_stored_at_0700_and_0600(store, tmp_path) -> None:
    """ADR-0027's permission is only defensible if the mode is what it says."""
    person = await _person(store)
    photos = tmp_path / "photos"
    photos.mkdir()
    path = _write_face(photos / "one.png")
    root = tmp_path / "var" / "enrol"
    stored = store_image(root, person, read_image(path), suffix=".png")
    assert stat.S_IMODE((root / person).stat().st_mode) == 0o700
    assert stat.S_IMODE(stored.stat().st_mode) == 0o600


async def test_a_loose_enrolment_root_is_tightened_and_said_out_loud(tmp_path, caplog) -> None:
    """Refusing would leave the photographs readable, which is worse. Tighten,
    and tell somebody it changed."""
    root = tmp_path / "loose"
    root.mkdir(mode=0o755)
    with caplog.at_level("WARNING"):
        ensure_root(root)
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert "tightened" in caplog.text


async def test_a_photograph_of_two_people_is_refused(store, tmp_path) -> None:
    """Guessing which face is the person being enrolled is how somebody ends up
    enrolled as somebody else."""
    person = await _person(store)
    photos = tmp_path / "photos"
    photos.mkdir()
    import av

    frame = np.full((180, 320, 3), 44, dtype=np.uint8)
    ys, xs = np.ogrid[:180, :320]
    for cx in (80, 240):
        frame[(xs - cx) ** 2 + (ys - 90) ** 2 <= 16**2] = 190
    with av.open(str(photos / "two.png"), "w") as container:
        stream = container.add_stream("png", rate=1)
        stream.width, stream.height, stream.pix_fmt = 320, 180, "rgb24"
        container.mux(stream.encode(av.VideoFrame.from_ndarray(frame, format="rgb24")))
        container.mux(stream.encode(None))

    with pytest.raises(EnrolmentError, match="no usable face"):
        await _enrol(store, person, photos, tmp_path / "var" / "enrol")
    assert await store.db.fetch_all("select count(*) from face_template") == [(0,)]


async def test_one_face_cannot_become_two_people(store, tmp_path) -> None:
    first = await _person(store, "p-first")
    second = await _person(store, "p-second")
    photos = tmp_path / "photos"
    photos.mkdir()
    _write_face(photos / "one.png")
    root = tmp_path / "var" / "enrol"
    await _enrol(store, first, photos, root)
    with pytest.raises(DuplicateIdentity, match="two identities"):
        await _enrol(store, second, photos, root)
    # ...unless a human says so explicitly
    report = await _enrol(store, second, photos, root, force=True)
    assert report.changed
    forced = await store.db.fetch_all(
        "select detail->>'forced' from enrolment_action where action = 'enrol' order by at"
    )
    assert forced[-1] == ("true",)


async def test_without_a_measured_threshold_the_duplicate_check_is_skipped_loudly(
    store, tmp_path, caplog
) -> None:
    """ADR-0010: no default thresholds. So this check cannot run, and says so
    rather than pretending two ids for one person are detectable."""
    first = await _person(store, "p-a")
    second = await _person(store, "p-b")
    photos = tmp_path / "photos"
    photos.mkdir()
    _write_face(photos / "one.png")
    root = tmp_path / "var" / "enrol"
    await _enrol(store, first, photos, root, match_threshold=None)
    with caplog.at_level("WARNING"):
        await _enrol(store, second, photos, root, match_threshold=None)
    assert "threshold is measured" in caplog.text


async def test_purge_takes_templates_and_images_together(store, tmp_path) -> None:
    """ADR-0027 names the easy mistake: deleting the photographs and leaving the
    embeddings behind."""
    person = await _person(store, "p-purge", consent_to=date(2026, 9, 1))  # already expired
    photos = tmp_path / "photos"
    photos.mkdir()
    _write_face(photos / "one.png")
    root = tmp_path / "var" / "enrol"
    # enrol against a consent that was live when granted
    await store.db.execute(
        "update consent_record set expires_on = '2026-12-31' where person_id = %s", (person,)
    )
    await _enrol(store, person, photos, root)
    await store.db.execute(
        "update consent_record set expires_on = '2026-09-01' where person_id = %s", (person,)
    )

    planned = await commands.purge_expired(
        store.db, root=root, today=TODAY, by="a human", dry_run=True
    )
    assert "nothing deleted" in planned.render()
    assert await store.db.fetch_all("select count(*) from face_template") == [(1,)]

    done = await commands.purge_expired(
        store.db, root=root, today=TODAY, by="a human", dry_run=False
    )
    assert done.changed
    assert await store.db.fetch_all("select count(*) from face_template") == [(0,)]
    assert await store.db.fetch_all("select count(*) from enrol_image") == [(0,)]
    assert not (root / person).exists()
    audit = await store.db.fetch_all(
        "select action, detail->>'files' from enrolment_action where action = 'purge'"
    )
    assert audit == [("purge", "1")]


async def test_purge_leaves_live_consent_alone(store, tmp_path) -> None:
    person = await _person(store, "p-keep")
    photos = tmp_path / "photos"
    photos.mkdir()
    _write_face(photos / "one.png")
    root = tmp_path / "var" / "enrol"
    await _enrol(store, person, photos, root)
    report = await commands.purge_expired(
        store.db, root=root, today=TODAY, by="a human", dry_run=False
    )
    assert "nothing to purge" in report.render()
    assert await store.db.fetch_all("select count(*) from face_template") == [(1,)]


async def test_withdrawn_consent_makes_a_template_purgeable(store, tmp_path) -> None:
    person = await _person(store, "p-withdraw")
    photos = tmp_path / "photos"
    photos.mkdir()
    _write_face(photos / "one.png")
    root = tmp_path / "var" / "enrol"
    await _enrol(store, person, photos, root)
    await commands.consent_revoke(store.db, person_id=person, by="a human", when=NOW, dry_run=False)
    report = await commands.purge_expired(
        store.db, root=root, today=TODAY, by="a human", dry_run=False
    )
    assert report.changed
    assert await store.db.fetch_all("select count(*) from face_template") == [(0,)]


async def test_badges_have_one_holder_and_a_history(store) -> None:
    first = await _person(store, "p-badge-a")
    second = await _person(store, "p-badge-b")
    await commands.badge_assign(
        store.db, person_id=first, badge_id="B-1", when=NOW, by="a human", dry_run=False
    )
    await commands.badge_unassign(
        store.db, badge_id="B-1", when=NOW.replace(hour=7), by="a human", dry_run=False
    )
    await commands.badge_assign(
        store.db,
        person_id=second,
        badge_id="B-1",
        when=NOW.replace(hour=8),
        by="a human",
        dry_run=False,
    )
    rows = await store.db.fetch_all(
        "select person_id, assigned_to is null from badge_assignment order by assigned_from"
    )
    assert rows == [(first, False), (second, True)]


async def test_list_reports_templates_consent_and_staleness(store, tmp_path) -> None:
    person = await _person(store, "p-list")
    photos = tmp_path / "photos"
    photos.mkdir()
    _write_face(photos / "one.png")
    await _enrol(store, person, photos, tmp_path / "var" / "enrol")

    listing = await commands.list_people(
        store.db,
        live_model_ref="mock-embed@000000000000",
        with_templates=True,
        stale=False,
        expiring_within=None,
        today=TODAY,
    )
    assert "templates=1" in listing.render()
    assert "consent_to=2026-12-31" in listing.render()

    stale = await commands.list_people(
        store.db,
        live_model_ref="a-different-model@abcdef",
        with_templates=False,
        stale=True,
        expiring_within=None,
        today=TODAY,
    )
    assert "re-enrol" in stale.render()


async def test_the_cli_requires_an_actor_on_every_write() -> None:
    """An inferred actor is a fake audit trail (RISKS.md §10)."""
    from argus.enrol.main import build_parser

    parser = build_parser()
    writing = [
        ["--config", "c.yaml", "person", "add", "--person", "p1", "--employee-ref", "HR-1"],
        ["--config", "c.yaml", "consent", "record", "--person", "p1", "--expires", "2026-12-31"],
        ["--config", "c.yaml", "enrol", "from-images", "--person", "p1", "--dir", "."],
        ["--config", "c.yaml", "badge", "assign", "--person", "p1", "--badge", "B-1"],
        ["--config", "c.yaml", "purge", "--expired"],
    ]
    for argv in writing:
        with pytest.raises(SystemExit) as exit_info:
            parser.parse_args(argv)
        assert exit_info.value.code == 2, argv
    # ...and reading needs no actor
    parsed = parser.parse_args(["--config", "c.yaml", "list"])
    assert parsed.command == "list"


async def test_purge_needs_yes_or_dry_run() -> None:
    """It deletes biometric data. A typo should not be enough."""
    from argus.enrol.main import build_parser, main

    parser = build_parser()
    args = parser.parse_args(["--config", "c.yaml", "purge", "--expired", "--by", "a human"])
    assert not args.yes and not args.dry_run

    import sys
    from unittest import mock

    with (
        mock.patch.object(
            sys, "argv", ["argus-enrol", "--config", "c.yaml", "purge", "--expired", "--by", "x"]
        ),
        pytest.raises(SystemExit) as exit_info,
    ):
        main()
    assert exit_info.value.code == 2


async def test_from_rtsp_is_not_a_command() -> None:
    """Cut deliberately: a third RTSP client per camera is a hazard on a client
    budget nobody has measured (ADR-0031, RISKS.md §6)."""
    from argus.enrol.main import build_parser

    with pytest.raises(SystemExit):
        build_parser().parse_args(
            ["--config", "c.yaml", "enrol", "from-rtsp", "--person", "p1", "--by", "x"]
        )
