"""The console: routes, templates, and nothing that computes a number."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from argus.common.config import AppConfig
from argus.store.db import Database, apply_migrations
from argus.ui import queries
from argus.ui.audit import log_clip_access
from argus.ui.auth import (
    COOKIE_NAME,
    Actor,
    AuthConfig,
    Role,
    current_actor,
    require,
    sign,
)
from argus.ui.clips import ClipNotServable, clip_offset_s, range_response, resolve_clip
from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

log = logging.getLogger(__name__)

TEMPLATES = Path(__file__).parent / "templates"


def humanise(seconds: int | None) -> str:
    """Durations in words, because "3600" on a page about wages is careless."""
    if seconds is None:
        return "-"
    if seconds == 0:
        return "none"
    if seconds % 3600 == 0:
        hours = seconds // 3600
        return f"{hours} hour" if hours == 1 else f"{hours} hours"
    if seconds % 60 == 0:
        minutes = seconds // 60
        return f"{minutes} minute" if minutes == 1 else f"{minutes} minutes"
    return f"{seconds // 60}m {seconds % 60}s"


def create_app(config: AppConfig, db: Database | None = None, *, data: Any = queries) -> FastAPI:
    """`data` is the seam the route tests fake, which keeps them database-free.

    With no `db`, the connection is opened in the **lifespan**, which is the
    only place it can be: a psycopg connection belongs to the event loop that
    created it, and uvicorn runs its own. Connecting at import time and handing
    the result to uvicorn produces a connection bound to a loop that no longer
    exists, which fails on the first query rather than at startup.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owned = app.state.db is None
        if owned:
            app.state.db = await Database.connect(config.database.dsn)
            await apply_migrations(app.state.db)
        try:
            yield
        finally:
            if owned and app.state.db is not None:
                await app.state.db.close()

    app = FastAPI(title="Sparrow Vision console", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.config = config
    app.state.db = db
    app.state.data = data
    app.state.auth = AuthConfig(
        config.ui.role_passphrases, session_ttl_minutes=config.ui.session_ttl_minutes
    )
    templates = Jinja2Templates(directory=str(TEMPLATES))
    templates.env.filters["humanise"] = humanise
    app.state.templates = templates

    def page(request: Request, name: str, actor: Actor | None, **context: Any) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name=name,
            context={"actor": actor, "shadow_mode": True, **context},
        )

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/login", response_class=HTMLResponse)
    async def login_form(request: Request) -> HTMLResponse:
        return page(
            request,
            "login.html",
            None,
            configured=request.app.state.auth.configured,
            error=None,
        )

    @app.post("/login")
    async def login(
        request: Request, actor_name: str = Form(...), passphrase: str = Form(...)
    ) -> Response:
        auth: AuthConfig = request.app.state.auth
        role = auth.role_for(passphrase)
        if role is None or not actor_name.strip():
            # One message for both failures: which of the two went wrong is not
            # information a failed login has earned.
            return page(
                request,
                "login.html",
                None,
                configured=auth.configured,
                error="Wrong passphrase, or no name typed.",
            )
        actor = Actor(name=actor_name.strip(), role=role)
        response = RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
        response.set_cookie(
            COOKIE_NAME,
            sign(auth, actor),
            httponly=True,
            samesite="strict",
            # No TLS on localhost, and ADR-0028 binds the console there. Said
            # out loud rather than left looking like an oversight.
            secure=False,
        )
        log.info("login: %s", actor)
        return response

    @app.post("/logout")
    async def logout() -> Response:
        response = RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
        response.delete_cookie(COOKIE_NAME)
        return response

    @app.get("/", response_class=HTMLResponse)
    async def home(request: Request) -> Response:
        actor = current_actor(request)
        if actor is None:
            return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
        run = await request.app.state.data.latest_run(request.app.state.db)
        gate = await request.app.state.data.gate_outcome_counts(request.app.state.db)
        violence = await request.app.state.data.violence_counts(request.app.state.db)
        return page(request, "home.html", actor, run=run, gate=gate, violence=violence)

    @app.get("/canteen", response_class=HTMLResponse)
    async def canteen(
        request: Request, actor: Actor = Depends(require(Role.PAYROLL, Role.REVIEWER))
    ) -> HTMLResponse:
        data = request.app.state.data
        run = await data.latest_run(request.app.state.db)
        days = await data.days_for_run(request.app.state.db, run.pairing_run_id) if run else []
        return page(request, "canteen.html", actor, run=run, days=days)

    @app.get("/canteen/person/{person_id}/{local_day}", response_class=HTMLResponse)
    async def canteen_person(
        request: Request,
        person_id: str,
        local_day: str,
        actor: Actor = Depends(require(Role.PAYROLL, Role.REVIEWER)),
    ) -> HTMLResponse:
        data = request.app.state.data
        run = await data.latest_run(request.app.state.db, date.fromisoformat(local_day))
        if run is None:
            raise HTTPException(status_code=404, detail="no run covers that day")
        intervals = await data.intervals_for_person_day(
            request.app.state.db, run.pairing_run_id, person_id, date.fromisoformat(local_day)
        )
        return page(
            request,
            "person_day.html",
            actor,
            run=run,
            person_id=person_id,
            local_day=local_day,
            intervals=intervals,
        )

    @app.get("/gate", response_class=HTMLResponse)
    async def gate(
        request: Request, actor: Actor = Depends(require(Role.REVIEWER))
    ) -> HTMLResponse:
        data = request.app.state.data
        return page(
            request,
            "gate.html",
            actor,
            events=await data.gate_events(request.app.state.db),
            counts=await data.gate_outcome_counts(request.app.state.db),
            gaps=await data.reader_gaps(request.app.state.db),
        )

    @app.post("/gate/{gate_event_id}/review")
    async def gate_review(
        request: Request,
        gate_event_id: UUID,
        decision: str = Form(...),
        reason: str = Form(""),
        actor: Actor = Depends(require(Role.REVIEWER)),
    ) -> Response:
        if decision not in ("dismissed", "escalated"):
            raise HTTPException(status_code=400, detail="decision must be dismissed or escalated")
        await request.app.state.data.review_gate_event(
            request.app.state.db,
            gate_event_id,
            state=decision,
            actor=actor.name,
            reason=reason or None,
        )
        return RedirectResponse("/gate", status_code=status.HTTP_303_SEE_OTHER)

    @app.get("/violence", response_class=HTMLResponse)
    async def violence(
        request: Request, actor: Actor = Depends(require(Role.REVIEWER))
    ) -> HTMLResponse:
        data = request.app.state.data
        return page(
            request,
            "violence.html",
            actor,
            candidates=await data.violence_queue(request.app.state.db),
            counts=await data.violence_counts(request.app.state.db),
        )

    @app.post("/violence/{candidate_id}/review")
    async def violence_review(
        request: Request,
        candidate_id: UUID,
        decision: str = Form(...),
        reason: str = Form(""),
        actor: Actor = Depends(require(Role.REVIEWER)),
    ) -> Response:
        if decision not in ("dismissed", "escalated"):
            raise HTTPException(status_code=400, detail="decision must be dismissed or escalated")
        changed = await request.app.state.data.review_violence(
            request.app.state.db,
            candidate_id,
            state=decision,
            actor=actor.name,
            reason=reason or None,
        )
        if not changed:
            # Somebody else got there first. Not an error -- the reviewer did
            # nothing wrong -- and their decision must not be overwritten.
            log.info("candidate %s was already reviewed; nothing changed", candidate_id)
        return RedirectResponse("/violence", status_code=status.HTTP_303_SEE_OTHER)

    @app.get("/monitoring", response_class=HTMLResponse)
    async def monitoring(
        request: Request, actor: Actor = Depends(require(Role.ADMIN))
    ) -> HTMLResponse:
        metrics = await request.app.state.data.monitoring(
            request.app.state.db, datetime.now(UTC).date()
        )
        return page(request, "monitoring.html", actor, metrics=metrics)

    @app.get("/clip/{clip_id}")
    async def clip(
        request: Request,
        clip_id: UUID,
        actor: Actor = Depends(require(Role.REVIEWER, Role.PAYROLL)),
    ) -> Response:
        data = request.app.state.data
        db = request.app.state.db
        context = "canteen_audit" if actor.role is Role.PAYROLL else "review"
        row = await data.clip_row(db, clip_id)

        async def refuse(reason: str) -> None:
            # Log before responding, always. Never serve-then-log.
            await log_clip_access(
                db,
                actor=actor.name,
                role=actor.role.value,
                clip_id=clip_id if row else None,
                context=context,
                outcome="denied",
                reason=reason,
            )

        if row is None or row["deleted_at"] is not None:
            await refuse("no such clip")
            raise HTTPException(status_code=404, detail="no such clip")

        if actor.role is Role.PAYROLL and not await data.clip_is_payroll_evidence(db, clip_id):
            # ADR-0028: the payroll tier reaches a clip through the line that
            # references it, and no other way.
            await refuse("payroll tier: clip is not referenced by a dwell interval")
            raise HTTPException(status_code=404, detail="no such clip")

        try:
            path = resolve_clip(Path(request.app.state.config.clips.dir), row["rel_path"])
        except ClipNotServable as exc:
            await refuse(str(exc))
            # 404, not 403: a 403 confirms the file exists.
            raise HTTPException(status_code=404, detail="no such clip") from exc

        await log_clip_access(
            db,
            actor=actor.name,
            role=actor.role.value,
            clip_id=clip_id,
            context=context,
            outcome="served",
        )
        return range_response(path, request.headers.get("range"))

    @app.get("/clip/{clip_id}/offset")
    async def clip_offset(
        request: Request,
        clip_id: UUID,
        at: str,
        actor: Actor = Depends(require(Role.REVIEWER, Role.PAYROLL)),
    ) -> dict[str, float]:
        """Where in the clip a moment is, for a `#t=` link."""
        row = await request.app.state.data.clip_row(request.app.state.db, clip_id)
        if row is None:
            raise HTTPException(status_code=404, detail="no such clip")
        duration = (row["end_utc"] - row["keyframe_utc"]).total_seconds()
        return {
            "offset_s": clip_offset_s(datetime.fromisoformat(at), row["keyframe_utc"], duration)
        }

    return app
