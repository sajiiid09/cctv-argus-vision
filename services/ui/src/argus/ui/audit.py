"""Who looked at which clip, including when they were refused.

Once per (actor, clip) per 60 seconds, in one statement: a `<video>` element
issues many Range requests for one viewing, and the dedupe window is what makes
the log readable. It is done in SQL rather than in a process cache because a
cache dies with the process and is not shared between workers, and because an
`update` to a counter would be an edit to an audit row.

Denied attempts are logged too, and logged **before** the response. An attempt
to reach a clip outside the allow-list is the interesting signal, and RISKS.md
§10 says this log is built regardless of how good the authentication is.
"""

from __future__ import annotations

import logging
from uuid import UUID, uuid4

from argus.store.db import Database

log = logging.getLogger(__name__)


async def log_clip_access(
    db: Database,
    *,
    actor: str,
    role: str,
    clip_id: UUID | None,
    context: str,
    outcome: str,
    reason: str | None = None,
    dedupe_s: int = 60,
) -> bool:
    """Record a view. Returns whether a row was actually written."""
    rows = await db.fetch_all(
        "insert into clip_access_log (access_id, actor, role, clip_id, context, reason, outcome)"
        " select %s, %s, %s, %s, %s, %s, %s"
        " where not exists ("
        "   select 1 from clip_access_log"
        "   where actor = %s and clip_id is not distinct from %s and outcome = %s"
        f"     and at > now() - interval '{int(dedupe_s)} seconds')"
        " returning access_id",
        (uuid4(), actor, role, clip_id, context, reason, outcome, actor, clip_id, outcome),
    )
    if outcome == "denied":
        log.warning(
            "clip access denied: actor=%s role=%s clip=%s (%s)", actor, role, clip_id, reason
        )
    return bool(rows)
