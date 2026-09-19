"""Serving a clip: the allow-list, the path checks, Range, and the audit trail.

These are the rules that decide whether somebody can watch footage of a named
worker, so they are checked the way a security control should be -- by trying
the thing that must not work.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from argus.ui.auth import Role
from argus.ui.clips import ClipNotServable, clip_offset_s, parse_range, resolve_clip
from fastapi import HTTPException

T0 = datetime(2026, 9, 18, 7, 0, tzinfo=UTC)
BODY = bytes(range(256)) * 8  # matches conftest.CLIP_BODY


@pytest.fixture(autouse=True)
def _wire(login, ids):
    global _as, CLIP_ID
    _as = login
    CLIP_ID = ids["clip"]


def test_the_whole_clip_is_served_when_nothing_is_asked_for(client, clip_file) -> None:
    _as(client, Role.REVIEWER)
    response = client.get(f"/clip/{CLIP_ID}")
    assert response.status_code == 200
    assert response.content == BODY
    assert response.headers["accept-ranges"] == "bytes"


def test_a_byte_range_comes_back_as_206_with_content_range(client, clip_file) -> None:
    _as(client, Role.REVIEWER)
    response = client.get(f"/clip/{CLIP_ID}", headers={"Range": "bytes=0-99"})
    assert response.status_code == 206
    assert response.content == BODY[:100]
    assert response.headers["content-range"] == f"bytes 0-99/{len(BODY)}"


def test_a_suffix_range_works_because_players_use_it(client, clip_file) -> None:
    _as(client, Role.REVIEWER)
    response = client.get(f"/clip/{CLIP_ID}", headers={"Range": "bytes=-128"})
    assert response.status_code == 206
    assert response.content == BODY[-128:]


def test_an_unsatisfiable_range_is_416(client, clip_file) -> None:
    _as(client, Role.REVIEWER)
    assert client.get(f"/clip/{CLIP_ID}", headers={"Range": "bytes=99999-"}).status_code == 416
    assert client.get(f"/clip/{CLIP_ID}", headers={"Range": "items=0-1"}).status_code == 416


def test_a_missing_clip_row_is_404(client) -> None:
    _as(client, Role.REVIEWER)
    assert client.get(f"/clip/{uuid4()}").status_code == 404


def test_a_deleted_clip_is_404(client, app_and_data, clip_file) -> None:
    _app, data, _config = app_and_data
    data.deleted = True
    _as(client, Role.REVIEWER)
    assert client.get(f"/clip/{CLIP_ID}").status_code == 404


def test_a_recorded_path_that_is_not_on_disk_is_404_not_500(client) -> None:
    _as(client, Role.REVIEWER)
    # no clip_file fixture: the row exists, the file does not
    assert client.get(f"/clip/{CLIP_ID}").status_code == 404


def test_the_payroll_tier_reaches_a_clip_only_through_its_line(
    client, app_and_data, clip_file
) -> None:
    """ADR-0028's scope. RISKS.md §10 says payroll cannot browse clips freely;
    SOUL.md says a payroll line must resolve to a clip in minutes. Both hold
    only if the check is on the clip's relationship to the line."""
    _app, data, _config = app_and_data
    _as(client, Role.PAYROLL)
    data.payroll_evidence = True
    assert client.get(f"/clip/{CLIP_ID}").status_code == 200
    data.payroll_evidence = False
    # 404, not 403: a 403 would confirm the clip exists.
    assert client.get(f"/clip/{CLIP_ID}").status_code == 404


def test_the_viewer_tier_reaches_no_clips_at_all(client, clip_file) -> None:
    _as(client, Role.VIEWER)
    assert client.get(f"/clip/{CLIP_ID}").status_code == 403


def test_a_stored_path_cannot_escape_the_clips_root(tmp_path) -> None:
    (tmp_path / "inside").mkdir()
    (tmp_path / "inside" / "a.mp4").write_bytes(b"x")
    assert resolve_clip(tmp_path, "inside/a.mp4").name == "a.mp4"
    with pytest.raises(ClipNotServable, match="not relative"):
        resolve_clip(tmp_path, "/etc/passwd")
    with pytest.raises(ClipNotServable, match="not relative"):
        resolve_clip(tmp_path, "inside/../../etc/passwd")


def test_a_symlink_planted_after_the_row_was_written_is_refused(tmp_path) -> None:
    """The second check exists for exactly this: the stored value is fine and
    the resolved path is not."""
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"secret")
    root = tmp_path / "clips"
    root.mkdir()
    (root / "sneaky.mp4").symlink_to(outside)
    with pytest.raises(ClipNotServable, match="outside the clips root"):
        resolve_clip(root, "sneaky.mp4")


def test_the_deep_link_offset_is_measured_from_the_actual_first_frame() -> None:
    """Clips snap back to the previous keyframe (ADR-0031), so an offset
    computed from the requested start lands early."""
    keyframe = T0
    event = T0 + timedelta(seconds=6)
    assert clip_offset_s(event, keyframe, 30.0) == pytest.approx(6.0)


def test_the_offset_is_clamped_at_both_ends() -> None:
    assert clip_offset_s(T0 - timedelta(seconds=5), T0, 30.0) == 0.0
    assert clip_offset_s(T0 + timedelta(seconds=90), T0, 30.0) == 30.0


def test_range_parsing_covers_the_cases_a_player_sends() -> None:
    assert parse_range(None, 1000) is None
    assert parse_range("bytes=0-499", 1000) == (0, 499)
    assert parse_range("bytes=500-", 1000) == (500, 999)
    assert parse_range("bytes=-200", 1000) == (800, 999)
    assert parse_range("bytes=0-99999", 1000) == (0, 999)
    with pytest.raises(HTTPException):
        parse_range("bytes=-", 1000)
