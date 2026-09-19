"""The ADR-0028 seam. All of it, in one file.

ADR-0028 records option (a): a per-role passphrase plus a typed actor name,
bound to localhost. The limitation is written down rather than glossed -- this
authenticates a **role**, not a person, and the actor name in the audit log is
self-asserted, so the log is evidence of what a role did and must never be
described as per-person accountability.

Routes never read the cookie. They receive an `Actor` from `require(...)`, which
means replacing this with real accounts (option b) is a rewrite of this file and
not of every route.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from fastapi import Depends, HTTPException, Request, status

log = logging.getLogger(__name__)

COOKIE_NAME = "argus_session"


class Role(StrEnum):
    """RISKS.md §10's four tiers."""

    VIEWER = "viewer"
    REVIEWER = "reviewer"
    PAYROLL = "payroll"
    ADMIN = "admin"


@dataclass(frozen=True, slots=True)
class Actor:
    # Typed by whoever logged in, and therefore self-asserted. ADR-0028 says so
    # out loud; nothing here should imply otherwise.
    name: str
    role: Role

    def __str__(self) -> str:
        return f"{self.name} ({self.role.value})"


class AuthConfig:
    """Passphrases and session settings, kept away from the routes."""

    def __init__(
        self,
        role_passphrases: dict[str, str],
        *,
        session_ttl_minutes: int = 120,
        secret: str | None = None,
    ) -> None:
        self.role_passphrases = {
            role: passphrase for role, passphrase in role_passphrases.items() if passphrase
        }
        self.session_ttl_minutes = session_ttl_minutes
        # A per-process secret by default: restarting the console logs everyone
        # out, which is the right default for a tool bound to localhost.
        self.secret = secret or secrets.token_urlsafe(32)

    def role_for(self, passphrase: str) -> Role | None:
        """Constant-time comparison against every configured passphrase.

        Every candidate is compared even after a match, so the time taken does
        not reveal which role was tried.
        """
        found: Role | None = None
        for role, expected in self.role_passphrases.items():
            if hmac.compare_digest(passphrase, expected):
                found = Role(role)
        return found

    @property
    def configured(self) -> bool:
        return bool(self.role_passphrases)


def sign(config: AuthConfig, actor: Actor, *, now: float | None = None) -> str:
    payload = {
        "name": actor.name,
        "role": actor.role.value,
        "exp": int((now or time.time()) + config.session_ttl_minutes * 60),
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    body = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    signature = hmac.new(config.secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{signature}"


def unsign(config: AuthConfig, token: str, *, now: float | None = None) -> Actor | None:
    try:
        body, signature = token.split(".", 1)
    except ValueError:
        return None
    expected = hmac.new(config.secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return None
    padding = "=" * (-len(body) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(body + padding))
    except Exception:
        return None
    if float(payload.get("exp", 0)) < (now or time.time()):
        return None
    try:
        return Actor(name=str(payload["name"]), role=Role(payload["role"]))
    except (KeyError, ValueError):
        return None


def current_actor(request: Request) -> Actor | None:
    """The only way a route learns who is asking."""
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    config: AuthConfig = request.app.state.auth
    return unsign(config, token)


def require(*roles: Role) -> Callable[..., Actor]:
    """FastAPI dependency: this route needs one of these tiers."""

    def dependency(request: Request) -> Actor:
        actor = current_actor(request)
        if actor is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="log in to continue"
            )
        if roles and actor.role is not Role.ADMIN and actor.role not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"this page needs one of {[r.value for r in roles]}",
            )
        return actor

    return dependency


def actor_dependency(*roles: Role) -> object:
    return Depends(require(*roles))
