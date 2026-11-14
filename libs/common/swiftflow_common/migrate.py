"""Ordered plain-SQL migrations per schema, under an advisory lock."""

from __future__ import annotations

from pathlib import Path

import asyncpg
import structlog

log = structlog.get_logger("migrate")


def discover(directory: Path) -> list[tuple[str, Path]]:
    """Return ``(version, path)`` pairs sorted by file name (``NNN_name.sql``)."""
    files = sorted(p for p in Path(directory).glob("*.sql") if p.is_file())
    return [(p.stem, p) for p in files]


async def apply_migrations(dsn: str, schema: str, directory: Path) -> list[str]:
    """Apply every unapplied migration file. Returns the versions applied."""
    applied_now: list[str] = []
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute("SELECT pg_advisory_lock(hashtext($1))", schema)
        try:
            await conn.execute(
                f'CREATE TABLE IF NOT EXISTS "{schema}".schema_migrations ('
                "version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
            )
            done = {r["version"] for r in await conn.fetch(f'SELECT version FROM "{schema}".schema_migrations')}
            for version, path in discover(directory):
                if version in done:
                    continue
                sql = path.read_text(encoding="utf-8")
                async with conn.transaction():
                    await conn.execute(f'SET LOCAL search_path TO "{schema}"')
                    await conn.execute(sql)
                    await conn.execute(f'INSERT INTO "{schema}".schema_migrations (version) VALUES ($1)', version)
                applied_now.append(version)
                log.info("migration_applied", schema=schema, version=version)
        finally:
            await conn.execute("SELECT pg_advisory_unlock(hashtext($1))", schema)
    finally:
        await conn.close()
    return applied_now
