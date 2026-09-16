"""Async Postgres access + a minimal, ordered migration runner."""

from __future__ import annotations

import hashlib
import importlib.resources
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import psycopg
from psycopg import AsyncConnection


@dataclass(slots=True)
class Database:
    conn: AsyncConnection

    @classmethod
    async def connect(cls, dsn: str) -> Database:
        conn = await psycopg.AsyncConnection.connect(dsn, autocommit=True)
        return cls(conn=conn)

    async def close(self) -> None:
        await self.conn.close()

    async def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        async with self.conn.cursor() as cur:
            await cur.execute(sql, params)

    async def fetch_all(self, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        async with self.conn.cursor() as cur:
            await cur.execute(sql, params)
            return list(await cur.fetchall())

    async def fetch_one(self, sql: str, params: tuple[Any, ...] = ()) -> tuple[Any, ...] | None:
        async with self.conn.cursor() as cur:
            await cur.execute(sql, params)
            return await cur.fetchone()


async def apply_migrations(db: Database) -> list[str]:
    """Apply packaged schema/*.sql files in name order, once each.

    The _migration table records the sha256 of each applied file; a changed
    file is an error (write a new migration, do not edit an applied one).
    """
    await db.execute(
        "create table if not exists _migration ("
        "  name text primary key, sha256 text not null,"
        "  applied_at timestamptz not null default now())"
    )
    schema_root = importlib.resources.files("argus.store") / "schema"
    applied: list[str] = []
    files = {p.name: p for p in schema_root.iterdir() if p.name.endswith(".sql")}
    for entry_name in sorted(files):
        entry = files[entry_name]
        sql = entry.read_text()
        digest = hashlib.sha256(sql.encode()).hexdigest()
        row = await db.fetch_one("select sha256 from _migration where name = %s", (entry.name,))
        if row is not None:
            if row[0] != digest:
                raise RuntimeError(
                    f"migration {entry.name} changed after being applied; write a new migration"
                )
            continue
        async with db.conn.cursor() as cur:
            await cur.execute("begin")
            try:
                await cur.execute(sql)
                await cur.execute(
                    "insert into _migration (name, sha256) values (%s, %s)",
                    (entry.name, digest),
                )
                await cur.execute("commit")
            except Exception:
                await cur.execute("rollback")
                raise
        applied.append(entry.name)
    return applied


def config_hash(config_yaml: str) -> str:
    return hashlib.sha256(config_yaml.encode()).hexdigest()[:16]


def new_uuid() -> UUID:
    import uuid

    return uuid.uuid4()
