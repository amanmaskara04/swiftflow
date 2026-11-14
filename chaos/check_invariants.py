"""Invariant checker. Exits non-zero if any invariant is broken.

    python chaos/check_invariants.py [--grace 120] [--gateway http://localhost:8080]

Connects as the Postgres superuser on purpose: it reads all three schemas and bypasses
row-level security, which no service role can do.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import asyncpg
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from admin import load_env

CHECKS: list[tuple[str, str]] = [
    (
        "1. no stock row is oversold or negative",
        "SELECT count(*) FROM inventory.stock_levels WHERE reserved > on_hand OR reserved < 0 OR on_hand < 0",
    ),
    (
        "2. reserved equals the sum of ACTIVE reservations",
        """SELECT count(*) FROM inventory.stock_levels s
        LEFT JOIN (SELECT product_id, warehouse_id, sum(qty) q FROM inventory.reservations
                   WHERE status = 'ACTIVE' GROUP BY 1, 2) r USING (product_id, warehouse_id)
        WHERE s.reserved <> COALESCE(r.q, 0)""",
    ),
    (
        "3. no order is stuck in flight",
        "SELECT count(*) FROM orders.orders WHERE status IN ('PENDING','RESERVED') AND created_at < now() - make_interval(secs => $1)",
    ),
    (
        "4a. cancelled and rejected orders hold no stock",
        """SELECT count(*) FROM inventory.reservations r JOIN orders.orders o ON o.id = r.order_id
        WHERE r.status = 'ACTIVE' AND o.status IN ('CANCELLED','REJECTED')
          AND o.updated_at < now() - make_interval(secs => $1)""",
    ),
    (
        "4b. every confirmed order holds a reservation per line",
        """SELECT count(*) FROM orders.orders o
        WHERE o.status = 'CONFIRMED' AND o.updated_at < now() - make_interval(secs => $1)
          AND (SELECT count(*) FROM orders.order_items i WHERE i.order_id = o.id)
           <> (SELECT count(*) FROM inventory.reservations r WHERE r.order_id = o.id AND r.status = 'ACTIVE')""",
    ),
    (
        "4c. shipped orders' reservations are committed",
        """SELECT count(*) FROM inventory.reservations r JOIN orders.orders o ON o.id = r.order_id
        WHERE o.status = 'SHIPPED' AND r.status <> 'COMMITTED' AND o.updated_at < now() - make_interval(secs => $1)""",
    ),
    (
        "5a. no cancelled order still holds money",
        """SELECT count(*) FROM orders.orders WHERE status = 'CANCELLED'
        AND payment_status IN ('CHARGED','REFUND_PENDING') AND updated_at < now() - make_interval(secs => $1)""",
    ),
    (
        "5b. confirmed and shipped orders are charged exactly once (one payment id)",
        "SELECT count(*) FROM orders.orders WHERE status IN ('CONFIRMED','SHIPPED') AND (payment_status <> 'CHARGED' OR payment_id IS NULL)",
    ),
    (
        "6. no outbox row is stuck unpublished",
        """SELECT (SELECT count(*) FROM orders.outbox_events WHERE published_at IS NULL AND created_at < now() - interval '30 seconds')
            + (SELECT count(*) FROM inventory.outbox_events WHERE published_at IS NULL AND created_at < now() - interval '30 seconds')""",
    ),
    (
        "7. every tenant table has row-level security forced",
        """SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname IN ('orders','inventory','notifications') AND c.relkind = 'r'
          AND EXISTS (SELECT 1 FROM information_schema.columns k WHERE k.table_schema = n.nspname
                      AND k.table_name = c.relname AND k.column_name = 'tenant_id')
          AND c.relname NOT IN ('api_clients','outbox_events')
          AND NOT (c.relrowsecurity AND c.relforcerowsecurity)""",
    ),
]


async def run(grace: float, gateway: str) -> list[tuple[str, bool, str]]:
    env = load_env()
    dsn = env.get("ADMIN_DATABASE_URL") or f"postgresql://swiftflow:{env['POSTGRES_PASSWORD']}@localhost:5432/swiftflow"
    results: list[tuple[str, bool, str]] = []
    conn = await asyncpg.connect(dsn)
    try:
        for name, sql in CHECKS:
            bad = await (conn.fetchval(sql, grace) if "$1" in sql else conn.fetchval(sql))
            results.append((name, bad == 0, f"{bad} violations"))
        totals = await conn.fetchrow(
            "SELECT count(*) AS orders, count(*) FILTER (WHERE status = 'CONFIRMED') AS confirmed, "
            "count(*) FILTER (WHERE status = 'REJECTED') AS rejected, count(*) FILTER (WHERE status = 'CANCELLED') AS cancelled, "
            "count(*) FILTER (WHERE status = 'SHIPPED') AS shipped FROM orders.orders"
        )
    finally:
        await conn.close()
    # 5c. the payment provider's own record: no order was charged twice
    doubles, detail = 0, []
    async with httpx.AsyncClient(base_url=gateway, timeout=15, trust_env=False) as http:
        for entry in env.get("DEMO_CLIENTS", "").split(";"):
            parts = entry.split(":", 3)
            if len(parts) == 4 and parts[2] == "admin":
                tok = (await http.post("/v1/auth/token", json={"client_id": parts[0], "client_secret": parts[1]})).json()[
                    "access_token"
                ]
                pay = (await http.get("/v1/sim/payments", headers={"Authorization": f"Bearer {tok}"})).json()
                doubles += pay["double_charges"]
                detail.append(f"{parts[3]}: {pay['approved']} approved, {pay['refunded']} refunded")
    results.append(
        ("5c. the provider recorded no double charges", doubles == 0, f"{doubles} double charges ({'; '.join(detail)})")
    )
    results.append(("totals", True, ", ".join(f"{k} {v}" for k, v in dict(totals).items())))
    return results


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--grace", type=float, default=120.0, help="seconds an order may still be in flight")
    ap.add_argument("--gateway", default="http://localhost:8080")
    args = ap.parse_args()
    results = asyncio.run(run(args.grace, args.gateway))
    ok = True
    for name, passed, info in results:
        ok &= passed
        print(f"{'PASS' if passed else 'FAIL'}  {name}  [{info}]")
    print("\nALL INVARIANTS HOLD" if ok else "\nINVARIANT VIOLATED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
