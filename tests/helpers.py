"""Shared test helpers.

A module rather than a conftest fixture because `tests/ui/conftest.py` shadows
`tests/conftest.py` for modules in that directory, and a roster row is needed in
both. `tests/` is on sys.path (see tests/conftest.py), so `import helpers` works
from anywhere in the suite.
"""

from __future__ import annotations

from typing import Any


async def insert_person(
    db: Any,
    person_id: str = "p1",
    *,
    employee_ref: str | None = None,
    active_from: str = "2026-01-01",
) -> str:
    """A roster row, because doorway_event.person_id is a real foreign key.

    Evidence attributed to a person_id that is not on the roster is invisible in
    a report and impossible to dispute, so the database refuses it (0004).
    Unknown stays expressible as null.
    """
    await db.execute(
        "insert into person (person_id, employee_ref, active_from) values (%s, %s, %s)"
        " on conflict (person_id) do nothing",
        (person_id, employee_ref or f"hr-{person_id}", active_from),
    )
    return person_id
