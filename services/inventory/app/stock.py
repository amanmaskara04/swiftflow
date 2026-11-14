"""Stock reservation, release and commit. Every function runs on a connection
that is already inside a tenant-scoped transaction.

The oversell guarantee is the conditional UPDATE in ``_take``: Postgres evaluates
``on_hand - reserved >= qty`` under the row lock and re-evaluates it after waiting
for a concurrent writer, so two orders can never both take the last unit. The
``CHECK (reserved <= on_hand)`` constraint is the backstop. See ADR 0003.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

import asyncpg
from prometheus_client import Counter

RESERVATION_CONFLICTS = Counter(
    "swiftflow_reservation_conflicts_total", "Reservation attempts that found the stock already taken"
)
RESERVATIONS = Counter("swiftflow_reservations_total", "Order reservation outcomes", ["result"])


@dataclass
class StockChange:
    sku: str
    warehouse_code: str
    on_hand: int
    reserved: int
    cause: str
    product_id: str = ""

    def payload(self) -> dict[str, Any]:
        return {
            "sku": self.sku,
            "warehouse_code": self.warehouse_code,
            "on_hand": self.on_hand,
            "reserved": self.reserved,
            "available": self.on_hand - self.reserved,
            "cause": self.cause,
        }


@dataclass
class ReserveResult:
    ok: bool
    items: list[dict[str, Any]] = field(default_factory=list)
    total_cents: int = 0
    changes: list[StockChange] = field(default_factory=list)
    # failure details
    reason: str = ""
    sku: str = ""
    requested: int = 0
    available: int = 0


class _Insufficient(Exception):
    def __init__(self, sku: str, requested: int) -> None:
        self.sku = sku
        self.requested = requested


def pick_order(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Warehouses to try for one line: most availability first, code as tie-break."""
    return sorted(candidates, key=lambda c: (-c["available"], c["code"]))


async def _take(conn: asyncpg.Connection, stock_id: Any, qty: int) -> asyncpg.Record | None:
    return await conn.fetchrow(
        """
        UPDATE stock_levels
        SET    reserved = reserved + $1, version = version + 1, updated_at = now()
        WHERE  id = $2 AND on_hand - reserved >= $1
        RETURNING on_hand, reserved, version
        """,
        qty,
        stock_id,
    )


async def reserve_order(conn: asyncpg.Connection, tenant_id: str, order_id: str, items: list[dict[str, Any]]) -> ReserveResult:
    """Reserve every line of an order, or none of them."""
    skus = [i["sku"] for i in items]
    products = {
        r["sku"]: r for r in await conn.fetch("SELECT id, sku, price_cents FROM products WHERE sku = ANY($1::text[])", skus)
    }
    for item in items:
        if item["sku"] not in products:
            RESERVATIONS.labels("unknown_sku").inc()
            return ReserveResult(False, reason="unknown_sku", sku=item["sku"], requested=item["qty"], available=0)

    # Lock rows in a fixed order (product id) so two multi-item orders cannot deadlock.
    ordered = sorted(items, key=lambda i: str(products[i["sku"]]["id"]))
    result = ReserveResult(True)
    try:
        async with conn.transaction():  # savepoint: a failed line undoes the earlier ones
            for item in ordered:
                product = products[item["sku"]]
                qty = int(item["qty"])
                rows = await conn.fetch(
                    """
                    SELECT s.id, s.warehouse_id, w.code, s.on_hand - s.reserved AS available
                    FROM stock_levels s JOIN warehouses w ON w.id = s.warehouse_id
                    WHERE s.product_id = $1 AND s.on_hand - s.reserved >= $2
                    """,
                    product["id"],
                    qty,
                )
                taken = None
                for cand in pick_order([dict(r) for r in rows]):
                    taken = await _take(conn, cand["id"], qty)
                    if taken is not None:
                        await conn.execute(
                            """
                            INSERT INTO reservations (id, tenant_id, order_id, product_id, warehouse_id, qty, status)
                            VALUES ($1, $2, $3, $4, $5, $6, 'ACTIVE')
                            """,
                            uuid.uuid4(),
                            tenant_id,
                            order_id,
                            product["id"],
                            cand["warehouse_id"],
                            qty,
                        )
                        result.items.append(
                            {
                                "sku": item["sku"],
                                "qty": qty,
                                "warehouse_id": str(cand["warehouse_id"]),
                                "warehouse_code": cand["code"],
                                "unit_price_cents": int(product["price_cents"]),
                            }
                        )
                        result.total_cents += qty * int(product["price_cents"])
                        result.changes.append(
                            StockChange(
                                item["sku"], cand["code"], taken["on_hand"], taken["reserved"], "reservation", str(product["id"])
                            )
                        )
                        break
                    RESERVATION_CONFLICTS.inc()  # someone else took it between our read and write
                if taken is None:
                    raise _Insufficient(item["sku"], qty)
    except _Insufficient as exc:
        available = await conn.fetchval(
            "SELECT COALESCE(SUM(on_hand - reserved), 0) FROM stock_levels WHERE product_id = $1",
            products[exc.sku]["id"],
        )
        RESERVATIONS.labels("insufficient_stock").inc()
        return ReserveResult(False, reason="insufficient_stock", sku=exc.sku, requested=exc.requested, available=int(available))
    RESERVATIONS.labels("reserved").inc()
    return result


async def _settle(conn: asyncpg.Connection, order_id: str, *, commit: bool) -> tuple[list[dict[str, Any]], list[StockChange]]:
    """Release (cancel) or commit (ship) every ACTIVE reservation of an order.
    Only ACTIVE rows change, so a repeat is a no-op."""
    rows = await conn.fetch(
        """
        SELECT r.id, r.product_id, r.warehouse_id, r.qty, p.sku, w.code
        FROM reservations r
        JOIN products p ON p.id = r.product_id
        JOIN warehouses w ON w.id = r.warehouse_id
        WHERE r.order_id = $1 AND r.status = 'ACTIVE'
        ORDER BY r.product_id, r.warehouse_id
        FOR UPDATE OF r
        """,
        order_id,
    )
    items: list[dict[str, Any]] = []
    changes: list[StockChange] = []
    for r in rows:
        if commit:
            level = await conn.fetchrow(
                """
                UPDATE stock_levels
                SET on_hand = on_hand - $1, reserved = reserved - $1, version = version + 1, updated_at = now()
                WHERE product_id = $2 AND warehouse_id = $3
                RETURNING on_hand, reserved
                """,
                r["qty"],
                r["product_id"],
                r["warehouse_id"],
            )
        else:
            level = await conn.fetchrow(
                """
                UPDATE stock_levels
                SET reserved = reserved - $1, version = version + 1, updated_at = now()
                WHERE product_id = $2 AND warehouse_id = $3
                RETURNING on_hand, reserved
                """,
                r["qty"],
                r["product_id"],
                r["warehouse_id"],
            )
        await conn.execute(
            "UPDATE reservations SET status = $2, updated_at = now() WHERE id = $1",
            r["id"],
            "COMMITTED" if commit else "RELEASED",
        )
        items.append({"sku": r["sku"], "warehouse_id": str(r["warehouse_id"]), "qty": r["qty"]})
        changes.append(
            StockChange(
                r["sku"], r["code"], level["on_hand"], level["reserved"], "commit" if commit else "release", str(r["product_id"])
            )
        )
    return items, changes


async def release_order(conn: asyncpg.Connection, order_id: str) -> tuple[list[dict[str, Any]], list[StockChange]]:
    return await _settle(conn, order_id, commit=False)


async def commit_order(conn: asyncpg.Connection, order_id: str) -> tuple[list[dict[str, Any]], list[StockChange]]:
    return await _settle(conn, order_id, commit=True)


def cache_key(tenant_id: str, sku: str) -> str:
    return f"avail:{tenant_id}:{sku}"
