"""Seed demo data. Safe to re-run.

1. Tenants and API clients go straight into the orders schema (as the order role).
2. Warehouses, products and stock go through the gateway with an admin token, exactly
   as a real client would create them.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import uuid
from pathlib import Path

import asyncpg
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from admin import ensure_client, ensure_tenant, load_env, order_dsn

GATEWAY = os.environ.get("GATEWAY_URL", "http://localhost:8080")

WAREHOUSES = {
    "Acme Outfitters": [("BLR-1", "Bengaluru North", "Bengaluru, IN"), ("DEL-2", "Delhi Central", "New Delhi, IN")],
    "Globex Supply": [("MUM-1", "Mumbai Port", "Mumbai, IN")],
}
# sku, name, price_cents, {warehouse: on_hand}
PRODUCTS = {
    "Acme Outfitters": [
        ("TEE-CLASSIC-S", "Classic tee, small", 1800, {"BLR-1": 80, "DEL-2": 40}),
        ("TEE-CLASSIC-M", "Classic tee, medium", 1800, {"BLR-1": 100, "DEL-2": 60}),
        ("TEE-CLASSIC-L", "Classic tee, large", 1800, {"BLR-1": 90, "DEL-2": 50}),
        ("HOODIE-ZIP-M", "Zip hoodie, medium", 4900, {"BLR-1": 35, "DEL-2": 20}),
        ("HOODIE-ZIP-L", "Zip hoodie, large", 4900, {"BLR-1": 30, "DEL-2": 12}),
        ("CAP-TRAIL", "Trail cap", 1200, {"BLR-1": 40, "DEL-2": 24}),
        ("BEANIE-WOOL", "Wool beanie", 1500, {"BLR-1": 55}),
        ("SOCK-MERINO-M", "Merino socks, medium", 900, {"BLR-1": 120, "DEL-2": 80}),
        ("SOCK-MERINO-L", "Merino socks, large", 900, {"BLR-1": 10, "DEL-2": 80}),
        ("BOTTLE-750", "Steel bottle 750 ml", 2200, {"BLR-1": 240}),
        ("BOTTLE-1L", "Steel bottle 1 l", 2600, {"DEL-2": 150}),
        ("PACK-DAY-20", "Day pack 20 l", 5900, {"BLR-1": 25, "DEL-2": 18}),
        ("PACK-TREK-45", "Trek pack 45 l", 11900, {"BLR-1": 8, "DEL-2": 6}),
        ("JACKET-RAIN-M", "Rain jacket, medium", 8900, {"BLR-1": 22, "DEL-2": 15}),
        ("JACKET-RAIN-L", "Rain jacket, large", 8900, {"BLR-1": 18, "DEL-2": 3}),
        ("GLOVE-LINER", "Liner gloves", 1400, {"DEL-2": 60}),
        ("LAMP-HEAD-300", "Headlamp 300 lm", 3200, {"BLR-1": 45, "DEL-2": 30}),
        ("MUG-ENAMEL", "Enamel mug", 1100, {"BLR-1": 70, "DEL-2": 70}),
        ("TOWEL-QUICK", "Quick-dry towel", 1600, {"BLR-1": 5}),
        ("LOAD-TEST-1", "Load test item", 100, {"BLR-1": 100}),
    ],
    "Globex Supply": [
        ("WIDGET-A", "Widget A", 500, {"MUM-1": 500}),
        ("WIDGET-B", "Widget B", 750, {"MUM-1": 300}),
        ("GASKET-12", "Gasket 12 mm", 120, {"MUM-1": 2000}),
        ("VALVE-34", "Valve 3/4 in", 2400, {"MUM-1": 90}),
    ],
}


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-").split("-")[0]


def demo_clients(env: dict[str, str]) -> list[tuple[str, str, str, str]]:
    out = []
    for entry in env.get("DEMO_CLIENTS", "").split(";"):
        parts = [p.strip() for p in entry.split(":", 3)]
        if len(parts) == 4 and all(parts):
            out.append((parts[0], parts[1], parts[2], parts[3]))
    return out


async def post(client: httpx.AsyncClient, path: str, token: str, body: dict, ok: tuple[int, ...] = (200, 201, 409)) -> None:
    resp = await client.post(
        path, json=body, headers={"Authorization": f"Bearer {token}", "Idempotency-Key": f"seed-{uuid.uuid4()}"}
    )
    if resp.status_code not in ok:
        raise SystemExit(f"{path} failed: {resp.status_code} {resp.text}")


async def main() -> None:
    env = load_env()
    clients = demo_clients(env)
    if not clients:
        raise SystemExit("DEMO_CLIENTS is empty; nothing to seed.")
    conn = await asyncpg.connect(order_dsn(env))
    try:
        for client_id, secret, role, tenant in clients:
            tenant_id = await ensure_tenant(conn, slugify(tenant), tenant)
            await ensure_client(conn, tenant_id, client_id, secret, role)
    finally:
        await conn.close()
    print(f"clients: {', '.join(c[0] for c in clients)}")

    async with httpx.AsyncClient(base_url=GATEWAY, timeout=20.0, trust_env=False) as http:
        for tenant in PRODUCTS:
            admin = next((c for c in clients if c[3] == tenant and c[2] == "admin"), None)
            if admin is None:
                continue
            resp = await http.post("/v1/auth/token", json={"client_id": admin[0], "client_secret": admin[1]})
            resp.raise_for_status()
            token = resp.json()["access_token"]
            for code, name, location in WAREHOUSES[tenant]:
                await post(http, "/v1/warehouses", token, {"code": code, "name": name, "location": location})
            for sku, name, price, _ in PRODUCTS[tenant]:
                await post(http, "/v1/products", token, {"sku": sku, "name": name, "price_cents": price})
            stock = (await http.get("/v1/inventory/stock", headers={"Authorization": f"Bearer {token}"})).json()
            have = {(r["sku"], c["warehouse_code"]) for r in stock["rows"] for c in r["cells"]}
            for sku, _, _, levels in PRODUCTS[tenant]:
                for code, qty in levels.items():
                    if (sku, code) not in have:  # never overwrite counts that already exist
                        await post(
                            http,
                            "/v1/inventory/adjustments",
                            token,
                            {"sku": sku, "warehouse_code": code, "mode": "set", "quantity": qty, "reason": "seed"},
                            ok=(200,),
                        )
            print(f"{tenant}: {len(WAREHOUSES[tenant])} warehouses, {len(PRODUCTS[tenant])} products")


if __name__ == "__main__":
    asyncio.run(main())
