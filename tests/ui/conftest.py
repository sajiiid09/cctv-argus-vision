"""Fixtures for the console tests: a fake data layer, an app, a client.

No database. `create_app` takes a `data` module and these tests pass a fake, so
the console's access rules stay in the fast tier -- a rule about who may watch
footage of a named worker should be checked on every commit, not only when
docker is up.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from argus.common.config import AppConfig, UiConfig
from argus.ui.app import create_app
from argus.ui.auth import COOKIE_NAME, Actor, AuthConfig, Role, sign
from argus.ui.queries import RunHeader
from fastapi.testclient import TestClient

T0 = datetime(2026, 9, 18, 7, 0, tzinfo=UTC)
CLIP_BODY = bytes(range(256)) * 8  # 2048 bytes of a "clip"
RUN_ID = uuid4()
CLIP_ID = uuid4()
CANDIDATE_ID = uuid4()
PASSPHRASES = {
    "viewer": "v-pass",
    "reviewer": "r-pass",
    "payroll": "p-pass",
    "admin": "a-pass",
}


@dataclass
class FakeData:
    """Stands in for argus.ui.queries. Records what the routes asked for."""

    payroll_evidence: bool = True
    clip_exists: bool = True
    deleted: bool = False
    access_log: list[tuple] = None  # type: ignore[assignment]
    reviewed: list[tuple] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.access_log = []
        self.reviewed = []

    async def latest_run(self, db, day=None):
        return RunHeader(
            pairing_run_id=RUN_ID,
            computed_at=T0,
            logic_version="pairing-1.0.0",
            policy_fingerprint="abc123",
            policy_description="allowance 2 minutes per local day; timezone Asia/Dhaka",
            shadow_mode=True,
            window_from_utc=T0,
            window_to_utc=T0,
        )

    async def days_for_run(self, db, run_id):
        return [
            {
                "person_id": "p1",
                "space_id": "canteen",
                "local_day": "2026-09-18",
                "total_dwell_s": 2700,
                "allowance_s": 120,
                "overage_s": 0,
                "day_state": "flagged",
                "flags": ["stream_gap"],
            }
        ]

    async def intervals_for_person_day(self, db, run_id, person_id, day):
        return [
            {
                "interval_id": uuid4(),
                "state": "resolved",
                "start_utc": T0,
                "end_utc": T0,
                "duration_s": 1800,
                "flags": [],
                "enter_clip_ref": "a.mp4",
                "exit_clip_ref": None,
                "enter_clip_id": CLIP_ID,
                "exit_clip_id": None,
                "enter_ts": T0,
                "exit_ts": None,
                "enter_keyframe": T0,
                "exit_keyframe": None,
            }
        ]

    async def gate_events(self, db, limit=100):
        return [
            {
                "gate_event_id": uuid4(),
                "badge_id": "B-1",
                "person_id": "p1",
                "face_verified": "false",
                "match_score": 0.12,
                "source": "simulated",
                "decided_at": T0,
                "review_state": "pending",
                "reviewed_by": None,
                "tapped_at": T0,
            }
        ]

    async def gate_outcome_counts(self, db):
        return {"true": 3, "false": 1, "no_face": 2, "not_attempted": 4}

    async def reader_gaps(self, db, limit=20):
        return [{"reader_id": "gate_reader_01", "from_utc": T0, "to_utc": None, "cause": "offline"}]

    async def violence_queue(self, db, state="pending"):
        return [
            {
                "candidate_id": CANDIDATE_ID,
                "camera_id": "floor_view",
                "at": T0,
                "trigger_score": 0.8,
                "clip_id": CLIP_ID,
                "review_state": "pending",
                "reviewed_by": None,
                "reviewed_at": None,
                "review_reason": None,
            }
        ]

    async def violence_counts(self, db):
        return {"pending": 1, "dismissed": 0, "escalated": 0}

    async def review_violence(self, db, candidate_id, *, state, actor, reason):
        self.reviewed.append((candidate_id, state, actor, reason))
        return True

    async def review_gate_event(self, db, gate_event_id, *, state, actor, reason):
        self.reviewed.append((gate_event_id, state, actor, reason))
        return True

    async def clip_row(self, db, clip_id):
        if not self.clip_exists:
            return None
        return {
            "clip_id": clip_id,
            "camera_id": "canteen_door_01",
            "rel_path": "canteen_door_01/a.mp4",
            "start_utc": T0,
            "end_utc": T0,
            "keyframe_utc": T0,
            "is_virtual": True,
            "deleted_at": T0 if self.deleted else None,
        }

    async def clip_is_payroll_evidence(self, db, clip_id):
        return self.payroll_evidence

    async def monitoring(self, db, day):
        return {
            "unknown_by_door": [{"door_id": "c1", "unknown": 2, "total": 10}],
            "gap_minutes": [{"camera_id": "canteen_door_01", "cause": "offline", "minutes": 3.5}],
            "flagged": {"flagged": 1, "days": 4, "overage_s": 600},
            "clip_access": [{"actor": "a human", "outcome": "served", "count": 2}],
            "review_latency": [{"state": "dismissed", "count": 1, "mean_s": 42.0}],
        }


@pytest.fixture
def app_and_data(tmp_path):
    config = AppConfig(ui=UiConfig(role_passphrases=dict(PASSPHRASES)))
    config.clips.dir = str(tmp_path)
    data = FakeData()

    class _NoDb:
        async def fetch_all(self, *args, **kwargs):
            return []

        async def fetch_one(self, *args, **kwargs):
            return None

        async def execute(self, *args, **kwargs):
            return None

    app = create_app(config, _NoDb(), data=data)
    app.state.auth = AuthConfig(dict(PASSPHRASES), secret="test-secret")
    return app, data, config


@pytest.fixture
def client(app_and_data):
    app, _data, _config = app_and_data
    return TestClient(app)


def _as(client: TestClient, role: Role, name: str = "a human") -> None:
    token = sign(client.app.state.auth, Actor(name=name, role=role))
    client.cookies.set(COOKIE_NAME, token)


def as_role(client: TestClient, role: Role, name: str = "a human") -> None:
    """Log in as a role without going through the form."""
    client.cookies.set(COOKIE_NAME, sign(client.app.state.auth, Actor(name=name, role=role)))


@pytest.fixture
def login():
    """`login(client, Role.REVIEWER)` -- handed over as a fixture because
    `tests/` is not an importable package."""
    return as_role


@pytest.fixture
def ids():
    """The identifiers the fake data layer answers for."""
    return {"run": RUN_ID, "clip": CLIP_ID, "candidate": CANDIDATE_ID}


@pytest.fixture
def clip_file(app_and_data):
    _app, _data, config = app_and_data
    path = Path(config.clips.dir) / "canteen_door_01" / "a.mp4"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(CLIP_BODY)
    return path
