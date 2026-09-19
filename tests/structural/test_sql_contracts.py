"""The schema restates decisions; these tests keep the two copies honest.

Every rule here is enforced twice on purpose -- once in Python, once as a
constraint -- because a rule enforced in one place is enforced in one place, and
derived rows outlive the process that computed them. What this file checks is
that the two statements of each rule still say the same thing.

It parses the SQL as text, so it needs no database and runs in CI's fast job.
"""

from __future__ import annotations

import re
from pathlib import Path

from argus.payroll.types import PairingState
from argus.store.store import GAP_CAUSES

SCHEMA_DIR = (
    Path(__file__).resolve().parents[2]
    / "packages"
    / "argus_store"
    / "src"
    / "argus"
    / "store"
    / "schema"
)


def _schema_files() -> list[Path]:
    files = sorted(SCHEMA_DIR.glob("*.sql"))
    assert files, f"no migrations found under {SCHEMA_DIR}"
    return files


def _sql() -> str:
    return "\n".join(p.read_text() for p in _schema_files())


def _strip_comments(sql: str) -> str:
    return "\n".join(line.split("--", 1)[0] for line in sql.splitlines())


def _in_list(column: str, sql: str | None = None) -> set[str]:
    """Values of the LAST `check (<column> in (...))` in migration order.

    Last, not first: a migration may drop and re-add a check to widen the
    vocabulary (0002 does exactly that to stream_gap.cause).
    """
    text = _strip_comments(sql if sql is not None else _sql())
    matches = re.findall(rf"check\s*\(\s*{column}\s+in\s*\(([^)]*)\)", text, re.IGNORECASE)
    assert matches, f"no `check ({column} in (...))` found in the schema"
    return set(re.findall(r"'([^']+)'", matches[-1]))


def _table(name: str) -> str:
    match = re.search(rf"create table {name} \((.*?)\n\);", _sql(), re.DOTALL)
    assert match, f"table {name} not found in the schema"
    return match.group(1)


def test_pairing_state_matches_the_sql_check() -> None:
    """dwell_interval.state and PairingState are one vocabulary, both ways.

    A state the database refuses is a run that dies half-written; a state the
    database accepts but payroll never produces is a column nobody can read.
    """
    assert _in_list("state", _table("dwell_interval")) == {s.value for s in PairingState}


def test_gap_causes_match_the_sql_check() -> None:
    """argus.store.GAP_CAUSES and stream_gap's check are one vocabulary.

    Scoped to stream_gap deliberately: reader_gap has its own, smaller
    vocabulary, so a test that matched "the last cause check in the file" would
    silently start comparing against the wrong table.
    """
    match = re.findall(
        r"add constraint stream_gap_cause_check\s*check \(cause in \(([^)]*)\)",
        _strip_comments(_sql()),
    )
    assert match, "stream_gap_cause_check not found (0002 replaces 0001's inline check)"
    assert set(re.findall(r"'([^']+)'", match[-1])) == set(GAP_CAUSES)


def test_reader_gap_has_its_own_causes() -> None:
    """A reader outage is not a camera outage, and it zeroes nothing."""
    assert _in_list("cause", _table("reader_gap")) == {
        "offline",
        "refused",
        "replay_window",
        "clock_anomaly",
        "crash",
    }


def test_gate_verification_keeps_four_outcomes() -> None:
    """no_face must never collapse into false (ARCHITECTURE.md §7.2).

    Not seeing a face is a camera problem; seeing a different face is a
    buddy-punching signal. Three values would destroy the distinction the gate
    pipeline exists to make, and `not_attempted` is the fourth because a
    replayed tap was never compared against anything.
    """
    assert _in_list("face_verified") == {"true", "false", "no_face", "not_attempted"}


def test_review_states_have_no_auto_dismiss() -> None:
    for table in ("gate_event", "violence_candidate"):
        assert _in_list("review_state", _table(table)) == {"pending", "dismissed", "escalated"}


def test_day_state_vocabulary() -> None:
    for table in ("dwell_day", "payroll_line"):
        assert _in_list("day_state", _table(table)) == {"clean", "flagged"}


def test_flagged_day_charges_nothing() -> None:
    """ADR-0023, as a constraint and not only as code."""
    body = _strip_comments(_table("dwell_day"))
    assert re.search(r"check\s*\(day_state = 'clean' or overage_s = 0\)", body)
    assert re.search(r"overage_s >= 0 and overage_s <= total_dwell_s", body)


def test_shadow_mode_is_not_a_column_value() -> None:
    """ADR-0005: a run or a line that claims not to be shadow cannot be stored."""
    for table in ("pairing_run", "payroll_line"):
        assert re.search(r"check\s*\(shadow_mode\)", _strip_comments(_table(table)))


def test_anonymous_tables_have_no_path_to_a_person() -> None:
    """SOUL.md "two separate numbers", as a failing test rather than a comment.

    Occupancy measures presence at a coordinate and violence produces a clip for
    a human. Neither may carry identity, and adding the column would look like a
    small refactor to anyone who had not read the document.
    """
    for table in ("occupancy_sample", "violence_candidate", "violence_review_action"):
        body = _strip_comments(_table(table))
        assert "person_id" not in body, f"{table} must not carry person_id"
        assert "references person" not in body, f"{table} must not reference person"
        for payroll_table in ("dwell_interval", "dwell_day", "payroll_line", "face_template"):
            assert f"references {payroll_table}" not in body


def test_derived_payroll_rows_are_append_only() -> None:
    """Recomputation writes a new run; nothing is ever edited in place."""
    sql = _sql()
    for table in ("pairing_run", "dwell_interval", "dwell_day"):
        assert re.search(rf"create trigger {table}_append_only_trigger", sql), table
    assert "payroll_line_no_export" in sql
    assert "payroll_line_no_delete" in sql


def test_export_is_refused_by_the_database() -> None:
    """RISKS.md §11: a trigger, so enabling export is a migration somebody writes."""
    sql = _strip_comments(_sql())
    assert "exported_at is not null" in sql
    assert "AGENTS.md §2.1" in _sql()


def test_face_template_requires_consent_and_a_model_ref() -> None:
    body = _strip_comments(_table("face_template"))
    assert re.search(r"consent_id\s+uuid not null references consent_record", body)
    assert re.search(r"model_ref\s+text not null", body)


def test_clip_paths_are_relative() -> None:
    for table in ("clip", "enrol_image"):
        body = _strip_comments(_table(table))
        assert "rel_path not like '/%'" in body and r"rel_path !~ '\.\.'" in body


def test_camera_uris_cannot_carry_credentials() -> None:
    """Credentials live in config/secrets.env, never in a row (RISKS.md §2)."""
    sql = _strip_comments(_sql())
    assert sql.count("!~ '://[^/@]*:[^/@]*@'") >= 2


def test_applied_migrations_are_never_edited() -> None:
    """0001 is hash-pinned by the runner; every change is a new file.

    The runner raises on a changed file, so an edit shows up as a hard failure on
    someone else's box. This test makes it show up here instead.
    """
    names = [p.name for p in _schema_files()]
    assert names == sorted(names)
    assert names[0] == "0001_init.sql"
    # 0001 still declares superseded_by and always will: it was applied, so it is
    # frozen. ADR-0032's retirement is a later migration rather than an edit, and
    # tests/store/test_schema_guards.py checks the live column is actually gone.
    first = _strip_comments(_schema_files()[0].read_text())
    later = _strip_comments("\n".join(f.read_text() for f in _schema_files()[1:]))
    assert "superseded_by" in first
    assert "drop column superseded_by" in later
    assert "duplicate_of" in later
