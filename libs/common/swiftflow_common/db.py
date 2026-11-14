"""asyncpg pool with JSON codecs and tenant-scoped transactions (RLS)."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import asyncpg

NIL_UUID = "00000000-0000-0000-0000-000000000000"


async def _init_connection(conn: asyncpg.Connection) -> None:
    for typ in ("json", "jsonb"):
        await conn.set_type_codec(typ, encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


async def create_pool(dsn: str, *, min_size: int = 1, max_size: int = 10, **kwargs: Any) -> asyncpg.Pool:
    return await asyncpg.create_pool(dsn, min_size=min_size, max_size=max_size, init=_init_connection, **kwargs)


class TxContext:
    """Passed alongside a connection; collects callbacks to run after commit."""

    def __init__(self) -> None:
        self.after_commit: list[Callable[[], Any]] = []

    async def run_after_commit(self) -> None:
        for cb in self.after_commit:
            res = cb()
            if hasattr(res, "__await__"):
                await res


@asynccontextmanager
async def tenant_tx(pool: asyncpg.Pool, tenant_id: str | uuid.UUID) -> AsyncIterator[asyncpg.Connection]:
    """A transaction that can only see and write ``tenant_id``'s rows.

    The setting is transaction-local (``is_local = true``), so it reverts when
    the transaction ends and never leaks to the next user of the connection.
    """
    tid = str(uuid.UUID(str(tenant_id)))  # validates; raises ValueError on garbage
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", tid)
        yield conn


@asynccontextmanager
async def system_tx(pool: asyncpg.Pool) -> AsyncIterator[asyncpg.Connection]:
    """A cross-tenant transaction for trusted background code (relay, dispatcher,
    payment processor). The nil UUID keeps the policy's cast from failing."""
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            "SELECT set_config('app.system', 'on', true), set_config('app.tenant_id', $1, true)",
            NIL_UUID,
        )
        yield conn


async def set_tenant(conn: asyncpg.Connection, tenant_id: str | uuid.UUID) -> None:
    """Set the tenant on a connection that is already inside a transaction."""
    await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(uuid.UUID(str(tenant_id))))


async def check_postgres(pool: asyncpg.Pool) -> None:
    async with pool.acquire(timeout=1.0) as conn:
        await conn.fetchval("SELECT 1", timeout=1.0)
