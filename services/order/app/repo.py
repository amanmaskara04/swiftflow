"""SQL for orders. Every function takes a connection that is already inside a
tenant-scoped (or system) transaction; all values are bound parameters."""

from __future__ import annotations

from typing import Any

import asyncpg
from swiftflow_common.outbox import add_event

from .config import PRODUCER


def iso(value: Any) -> str | None:
    return value.isoformat().replace("+00:00", "Z") if value is not None else None


def summary(row: asyncpg.Record) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "status": row["status"],
        "customer_ref": row["customer_ref"],
        "total_cents": row["total_cents"],
        "item_count": row["item_count"],
        "failure_reason": row["failure_reason"],
        "payment_status": row["payment_status"],
        "created_at": iso(row["created_at"]),
        "updated_at": iso(row["updated_at"]),
    }


ORDER_COLUMNS = """
  o.id, o.tenant_id, o.status, o.customer_ref, o.total_cents, o.failure_reason, o.payment_id, o.payment_status,
  o.payment_attempts, o.request_hash, o.created_at, o.updated_at,
  (SELECT COALESCE(SUM(i.qty), 0) FROM order_items i WHERE i.order_id = o.id)::int AS item_count
"""


async def get_order(conn: asyncpg.Connection, order_id: Any, *, for_update: bool = False) -> asyncpg.Record | None:
    return await conn.fetchrow(
        f"SELECT {ORDER_COLUMNS} FROM orders o WHERE o.id = $1" + (" FOR UPDATE OF o" if for_update else ""), order_id
    )


async def order_detail(conn: asyncpg.Connection, row: asyncpg.Record, *, with_history: bool = True) -> dict[str, Any]:
    items = await conn.fetch(
        "SELECT sku, qty, warehouse_code, unit_price_cents FROM order_items WHERE order_id = $1 ORDER BY sku", row["id"]
    )
    out = summary(row)
    out["payment_id"] = row["payment_id"]
    out["items"] = [dict(i) for i in items]
    if with_history:
        hist = await conn.fetch(
            "SELECT from_status, to_status, reason, event_type, at FROM order_history WHERE order_id = $1 ORDER BY id",
            row["id"],
        )
        out["history"] = [
            {
                "from_status": h["from_status"],
                "to_status": h["to_status"],
                "reason": h["reason"],
                "event_type": h["event_type"],
                "at": iso(h["at"]),
            }
            for h in hist
        ]
    return out


async def add_history(
    conn: asyncpg.Connection,
    order: asyncpg.Record | dict,
    to_status: str,
    *,
    reason: str | None = None,
    event_type: str | None = None,
    from_status: str | None = None,
) -> None:
    await conn.execute(
        "INSERT INTO order_history (order_id, tenant_id, from_status, to_status, reason, event_type) "
        "VALUES ($1, $2, $3, $4, $5, $6)",
        order["id"],
        order["tenant_id"],
        from_status,
        to_status,
        reason,
        event_type,
    )


async def emit(conn: asyncpg.Connection, event_type: str, order: asyncpg.Record | dict, payload: dict[str, Any]) -> None:
    await add_event(
        conn,
        event_type,
        tenant_id=str(order["tenant_id"]),
        aggregate_id=str(order["id"]),
        producer=PRODUCER,
        payload={"order_id": str(order["id"]), **payload},
    )
