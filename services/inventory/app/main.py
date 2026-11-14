"""Inventory API (:8002): catalogue, adjustments, availability, stock grid."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Annotated, Any, Literal

import asyncpg
from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StringConstraints
from swiftflow_common.app import build_app, decode_cursor, encode_cursor
from swiftflow_common.auth import Principal, require_scope
from swiftflow_common.db import tenant_tx
from swiftflow_common.idempotency import run_idempotent
from swiftflow_common.outbox import add_event
from swiftflow_common.problems import ProblemError, not_found
from swiftflow_common.runtime import close_core, core_health, install_core, open_pool, open_redis

from .config import MIGRATIONS, PRODUCER, SCHEMA, settings
from .stock import StockChange, cache_key

Sku = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._\-]+$")]
Code = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=32, pattern=r"^[A-Za-z0-9._\-]+$")]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    pool = await open_pool(settings.database_url, SCHEMA, MIGRATIONS)
    install_core(app, settings, pool=pool, redis=open_redis(settings.redis_url), namespace="inventory")
    yield
    await close_core(app)


app = build_app(settings, title="SwiftFlow Inventory", lifespan=lifespan)
app.include_router(core_health(app))


class WarehouseIn(BaseModel):
    code: Code
    name: Name
    location: Annotated[str, StringConstraints(max_length=200)] | None = None


class ProductIn(BaseModel):
    sku: Sku
    name: Name
    price_cents: int = Field(ge=0, le=100_000_000)


class AdjustmentIn(BaseModel):
    sku: Sku
    warehouse_code: Code
    mode: Literal["set", "delta"]
    quantity: int = Field(ge=-1_000_000, le=1_000_000)
    reason: Annotated[str, StringConstraints(max_length=200)] | None = None


def _conflict(title: str, detail: str) -> ProblemError:
    return ProblemError(409, "/problems/conflict", title, detail)


def _iso(value: Any) -> str | None:
    return value.isoformat().replace("+00:00", "Z") if value is not None else None


async def _invalidate(request: Request, tenant_id: str, skus: list[str]) -> None:
    with suppress(Exception):
        await request.app.state.redis.delete(*(cache_key(tenant_id, s) for s in skus))


# ---------------------------------------------------------------- warehouses
@app.post("/v1/warehouses", status_code=201)
async def create_warehouse(body: WarehouseIn, request: Request, p: Principal = Depends(require_scope("admin"))) -> dict:
    async with tenant_tx(request.app.state.pool, p.tenant_id) as conn:
        try:
            row = await conn.fetchrow(
                "INSERT INTO warehouses (id, tenant_id, code, name, location) VALUES ($1,$2,$3,$4,$5) "
                "RETURNING id, code, name, location, created_at",
                uuid.uuid4(),
                p.tenant_id,
                body.code,
                body.name,
                body.location,
            )
        except asyncpg.UniqueViolationError:
            raise _conflict("Warehouse already exists", f"A warehouse with code {body.code} already exists.") from None
    return {
        "id": str(row["id"]),
        "code": row["code"],
        "name": row["name"],
        "location": row["location"],
        "created_at": _iso(row["created_at"]),
    }


@app.get("/v1/warehouses")
async def list_warehouses(request: Request, p: Principal = Depends(require_scope("inventory:read"))) -> dict:
    async with tenant_tx(request.app.state.pool, p.tenant_id) as conn:
        rows = await conn.fetch("SELECT id, code, name, location, created_at FROM warehouses ORDER BY code LIMIT 500")
    return {
        "items": [
            {
                "id": str(r["id"]),
                "code": r["code"],
                "name": r["name"],
                "location": r["location"],
                "created_at": _iso(r["created_at"]),
            }
            for r in rows
        ],
        "next_cursor": None,
    }


# ---------------------------------------------------------------- products
@app.post("/v1/products", status_code=201)
async def create_product(body: ProductIn, request: Request, p: Principal = Depends(require_scope("admin"))) -> dict:
    async with tenant_tx(request.app.state.pool, p.tenant_id) as conn:
        try:
            row = await conn.fetchrow(
                "INSERT INTO products (id, tenant_id, sku, name, price_cents) VALUES ($1,$2,$3,$4,$5) "
                "RETURNING id, sku, name, price_cents, created_at",
                uuid.uuid4(),
                p.tenant_id,
                body.sku,
                body.name,
                body.price_cents,
            )
        except asyncpg.UniqueViolationError:
            raise _conflict("Product already exists", f"A product with SKU {body.sku} already exists.") from None
    return {
        "id": str(row["id"]),
        "sku": row["sku"],
        "name": row["name"],
        "price_cents": row["price_cents"],
        "created_at": _iso(row["created_at"]),
    }


@app.get("/v1/products")
async def list_products(
    request: Request,
    q: Annotated[str | None, Query(max_length=64)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: str | None = None,
    p: Principal = Depends(require_scope("inventory:read")),
) -> dict:
    after = decode_cursor(cursor, 1)
    like = None
    if q:
        like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    async with tenant_tx(request.app.state.pool, p.tenant_id) as conn:
        rows = await conn.fetch(
            """
            SELECT id, sku, name, price_cents, created_at FROM products
            WHERE ($1::text IS NULL OR sku ILIKE $1 OR name ILIKE $1)
              AND ($2::text IS NULL OR sku > $2)
            ORDER BY sku LIMIT $3
            """,
            like,
            str(after[0]) if after else None,
            limit + 1,
        )
    page = rows[:limit]
    return {
        "items": [
            {
                "id": str(r["id"]),
                "sku": r["sku"],
                "name": r["name"],
                "price_cents": r["price_cents"],
                "created_at": _iso(r["created_at"]),
            }
            for r in page
        ],
        "next_cursor": encode_cursor([page[-1]["sku"]]) if len(rows) > limit else None,
    }


# ---------------------------------------------------------------- adjustments
@app.post("/v1/inventory/adjustments")
async def adjust(body: AdjustmentIn, request: Request, p: Principal = Depends(require_scope("admin"))) -> JSONResponse:
    if_match = request.headers.get("if-match")
    expected: int | None = None
    if if_match is not None:
        try:
            expected = int(if_match.strip().strip('"'))
        except ValueError:
            raise ProblemError(422, "/problems/validation", "Validation failed", "If-Match must be a version number.") from None

    async def work() -> tuple[int, dict]:
        async with tenant_tx(request.app.state.pool, p.tenant_id) as conn:
            product = await conn.fetchrow("SELECT id FROM products WHERE sku = $1", body.sku)
            warehouse = await conn.fetchrow("SELECT id FROM warehouses WHERE code = $1", body.warehouse_code)
            if product is None:
                raise not_found(f"No product with SKU {body.sku}.")
            if warehouse is None:
                raise not_found(f"No warehouse with code {body.warehouse_code}.")
            await conn.execute(
                "INSERT INTO stock_levels (id, tenant_id, product_id, warehouse_id, on_hand) VALUES ($1,$2,$3,$4,0) "
                "ON CONFLICT (product_id, warehouse_id) DO NOTHING",
                uuid.uuid4(),
                p.tenant_id,
                product["id"],
                warehouse["id"],
            )
            level = await conn.fetchrow(
                "SELECT id, on_hand, reserved, version FROM stock_levels WHERE product_id = $1 AND warehouse_id = $2 FOR UPDATE",
                product["id"],
                warehouse["id"],
            )
            if expected is not None and expected != level["version"]:
                raise ProblemError(
                    409,
                    "/problems/version-conflict",
                    "Stock changed",
                    "Someone changed this stock while you were editing. Reload to see the latest count.",
                    current_version=level["version"],
                )
            new_on_hand = body.quantity if body.mode == "set" else level["on_hand"] + body.quantity
            if new_on_hand < 0:
                raise ProblemError(422, "/problems/validation", "Validation failed", "On-hand stock cannot be negative.")
            if new_on_hand < level["reserved"]:
                raise _conflict(
                    "Count is below reserved stock",
                    f"{level['reserved']} units are reserved for open orders, so on hand cannot be set to {new_on_hand}.",
                )
            updated = await conn.fetchrow(
                "UPDATE stock_levels SET on_hand = $1, version = version + 1, updated_at = now() WHERE id = $2 "
                "RETURNING on_hand, reserved, version",
                new_on_hand,
                level["id"],
            )
            await conn.execute(
                "INSERT INTO adjustments (id, tenant_id, stock_level_id, mode, quantity, on_hand_before, on_hand_after, reason, actor) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)",
                uuid.uuid4(),
                p.tenant_id,
                level["id"],
                body.mode,
                body.quantity,
                level["on_hand"],
                new_on_hand,
                body.reason,
                p.client_id,
            )
            change = StockChange(body.sku, body.warehouse_code, updated["on_hand"], updated["reserved"], "adjustment")
            await add_event(
                conn,
                "StockLevelChanged",
                tenant_id=p.tenant_id,
                aggregate_id=str(product["id"]),
                producer=PRODUCER,
                payload=change.payload(),
                key=f"{p.tenant_id}:{body.sku}",
            )
        await _invalidate(request, p.tenant_id, [body.sku])
        return 200, {**change.payload(), "version": updated["version"]}

    return await run_idempotent(
        request,
        request.app.state.idempotency,
        p.tenant_id,
        {**body.model_dump(), "if_match": expected},
        work,
        required=True,
    )


# ---------------------------------------------------------------- read models
@app.get("/v1/inventory/stock")
async def stock_grid(request: Request, p: Principal = Depends(require_scope("inventory:read"))) -> dict:
    async with tenant_tx(request.app.state.pool, p.tenant_id) as conn:
        warehouses = await conn.fetch("SELECT code, name, location FROM warehouses ORDER BY code")
        products = await conn.fetch("SELECT id, sku, name, price_cents FROM products ORDER BY sku LIMIT 2000")
        levels = await conn.fetch(
            "SELECT s.product_id, w.code, s.on_hand, s.reserved, s.version "
            "FROM stock_levels s JOIN warehouses w ON w.id = s.warehouse_id"
        )
    by_product: dict[Any, list[dict]] = {}
    oversold = 0
    for lv in levels:
        if lv["reserved"] > lv["on_hand"]:
            oversold += 1
        by_product.setdefault(lv["product_id"], []).append(
            {
                "warehouse_code": lv["code"],
                "on_hand": lv["on_hand"],
                "reserved": lv["reserved"],
                "available": lv["on_hand"] - lv["reserved"],
                "version": lv["version"],
            }
        )
    rows = []
    totals = {"on_hand": 0, "reserved": 0, "available": 0}
    for pr in products:
        cells = sorted(by_product.get(pr["id"], []), key=lambda c: c["warehouse_code"])
        on_hand = sum(c["on_hand"] for c in cells)
        reserved = sum(c["reserved"] for c in cells)
        rows.append(
            {
                "sku": pr["sku"],
                "name": pr["name"],
                "price_cents": pr["price_cents"],
                "cells": cells,
                "total_on_hand": on_hand,
                "total_reserved": reserved,
                "total_available": on_hand - reserved,
            }
        )
        totals["on_hand"] += on_hand
        totals["reserved"] += reserved
        totals["available"] += on_hand - reserved
    return {"warehouses": [dict(w) for w in warehouses], "rows": rows, "totals": totals, "oversold_rows": oversold}


@app.get("/v1/inventory/{sku}/availability")
async def availability(sku: Sku, request: Request, p: Principal = Depends(require_scope("inventory:read"))) -> dict:
    redis = request.app.state.redis
    key = cache_key(p.tenant_id, sku)
    with suppress(Exception):
        cached = await redis.get(key)
        if cached:
            return {**json.loads(cached), "cached": True}
    async with tenant_tx(request.app.state.pool, p.tenant_id) as conn:
        if await conn.fetchval("SELECT 1 FROM products WHERE sku = $1", sku) is None:
            raise not_found(f"No product with SKU {sku}.")
        rows = await conn.fetch(
            "SELECT w.code, s.on_hand, s.reserved, s.version FROM stock_levels s "
            "JOIN warehouses w ON w.id = s.warehouse_id JOIN products p ON p.id = s.product_id "
            "WHERE p.sku = $1 ORDER BY w.code",
            sku,
        )
    data = {
        "sku": sku,
        "total_available": sum(r["on_hand"] - r["reserved"] for r in rows),
        "warehouses": [
            {
                "code": r["code"],
                "on_hand": r["on_hand"],
                "reserved": r["reserved"],
                "available": r["on_hand"] - r["reserved"],
                "version": r["version"],
            }
            for r in rows
        ],
    }
    with suppress(Exception):
        await redis.set(key, json.dumps(data), ex=settings.availability_cache_ttl)
    return {**data, "cached": False}
