"""Console routes: tiers, headers, and what each page says out loud."""

from __future__ import annotations

import pytest
from argus.ui.app import humanise
from argus.ui.auth import COOKIE_NAME, Role, sign


@pytest.fixture(autouse=True)
def _wire(login, ids):
    """`tests/` is not an importable package, so the shared helper and the fake
    data's identifiers arrive as fixtures."""
    global _as, RUN_ID, CLIP_ID, CANDIDATE_ID
    _as = login
    RUN_ID, CLIP_ID, CANDIDATE_ID = ids["run"], ids["clip"], ids["candidate"]


def test_healthz_needs_no_login(client) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}


def test_an_unauthenticated_visitor_is_sent_to_the_login_page(client) -> None:
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/login"


def test_every_data_page_refuses_without_a_session(client) -> None:
    for path in ("/canteen", "/gate", "/violence", "/monitoring", f"/clip/{CLIP_ID}"):
        assert client.get(path).status_code == 401, path


def test_logging_in_needs_a_name_and_the_right_passphrase(client) -> None:
    bad = client.post("/login", data={"actor_name": "a human", "passphrase": "wrong"})
    assert "Wrong passphrase" in bad.text
    nameless = client.post("/login", data={"actor_name": "  ", "passphrase": "r-pass"})
    assert "Wrong passphrase" in nameless.text
    good = client.post(
        "/login", data={"actor_name": "a human", "passphrase": "r-pass"}, follow_redirects=False
    )
    assert good.status_code == 303
    assert COOKIE_NAME in good.cookies


def test_the_tiers_are_enforced(client) -> None:
    _as(client, Role.VIEWER)
    assert client.get("/canteen").status_code == 403
    assert client.get("/monitoring").status_code == 403
    _as(client, Role.REVIEWER)
    assert client.get("/gate").status_code == 200
    assert client.get("/monitoring").status_code == 403
    _as(client, Role.ADMIN)
    # Admin reaches everything, which is what "admin" means here.
    assert client.get("/monitoring").status_code == 200
    assert client.get("/canteen").status_code == 200


def test_a_tampered_cookie_is_not_a_session(client) -> None:
    _as(client, Role.ADMIN)
    token = client.cookies[COOKIE_NAME]
    body, _signature = token.split(".", 1)
    client.cookies.set(COOKIE_NAME, f"{body}.0000")
    assert client.get("/canteen").status_code == 401


def test_an_expired_session_is_not_a_session(client) -> None:
    import time

    from argus.ui.auth import Actor

    auth = client.app.state.auth
    stale = sign(auth, Actor("a human", Role.ADMIN), now=time.time() - 10**6)
    client.cookies.set(COOKIE_NAME, stale)
    assert client.get("/canteen").status_code == 401


def test_the_canteen_header_comes_from_the_stored_run(client) -> None:
    """ADR-0015: the page describes the run that produced its numbers, so
    editing the config afterwards cannot change what yesterday's page claims."""
    _as(client, Role.PAYROLL)
    body = client.get("/canteen").text
    assert "allowance 2 minutes per local day" in body
    assert "abc123" in body and str(RUN_ID) in body
    assert "Shadow mode" in body


def test_the_canteen_page_says_a_flagged_day_charges_nothing(client) -> None:
    _as(client, Role.PAYROLL)
    body = client.get("/canteen").text
    assert "flagged" in body and "charges nothing" in body


def test_the_audit_view_links_both_clips_and_names_a_missing_one(client) -> None:
    _as(client, Role.PAYROLL)
    body = client.get("/canteen/person/p1/2026-09-18").text
    assert f"/clip/{CLIP_ID}" in body
    assert "no exit clip" in body


def test_the_gate_page_keeps_no_face_and_false_apart(client) -> None:
    _as(client, Role.REVIEWER)
    body = client.get("/gate").text
    assert "No face" in body and "Different face" in body
    assert "camera problem" in body


def test_reviews_are_recorded_with_the_actor(client, app_and_data) -> None:
    _app, data, _config = app_and_data
    _as(client, Role.REVIEWER, name="a reviewer")
    response = client.post(
        f"/violence/{CANDIDATE_ID}/review",
        data={"decision": "dismissed", "reason": "horseplay"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert data.reviewed == [(CANDIDATE_ID, "dismissed", "a reviewer", "horseplay")]


def test_a_review_decision_must_be_one_of_two_words(client) -> None:
    _as(client, Role.REVIEWER)
    response = client.post(
        f"/violence/{CANDIDATE_ID}/review", data={"decision": "confirmed_violence"}
    )
    assert response.status_code == 400


def test_an_already_reviewed_candidate_is_not_an_error(client, app_and_data) -> None:
    """Somebody else got there first; the reviewer did nothing wrong, and their
    decision must not be overwritten."""
    _app, data, _config = app_and_data

    async def already_reviewed(db, candidate_id, *, state, actor, reason):
        return False

    data.review_violence = already_reviewed  # type: ignore[method-assign]
    _as(client, Role.REVIEWER)
    response = client.post(
        f"/violence/{CANDIDATE_ID}/review", data={"decision": "dismissed"}, follow_redirects=False
    )
    assert response.status_code == 303


def test_the_queue_says_why_it_is_empty(client, app_and_data) -> None:
    """An empty queue is not a success metric (AGENTS.md §9)."""
    _app, data, _config = app_and_data

    async def nothing(db, state="pending"):
        return []

    data.violence_queue = nothing  # type: ignore[method-assign]
    _as(client, Role.REVIEWER)
    body = client.get("/violence").text
    assert "No violence pipeline is running yet" in body


def test_monitoring_shows_review_latency_and_says_why(client) -> None:
    _as(client, Role.ADMIN)
    body = client.get("/monitoring").text
    assert "Review latency" in body and "rubber-stamping" in body


def test_durations_are_rendered_in_words() -> None:
    assert humanise(3600) == "1 hour"
    assert humanise(120) == "2 minutes"
    assert humanise(0) == "none"
    assert humanise(None) == "-"
    assert humanise(95) == "1m 35s"
