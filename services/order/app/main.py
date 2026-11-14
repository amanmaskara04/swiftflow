"""Order API (:8001): token issuer + JWKS, and the order endpoints."""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated, Any
from urllib.parse import parse_qs

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StringConstraints, ValidationError, field_validator
from swiftflow_common.app import build_app, decode_cursor, encode_cursor
from swiftflow_common.auth import Principal, require_scope
from swiftflow_common.db import tenant_tx
from swiftflow_common.idempotency import mismatch, request_hash, validate_key
from swiftflow_common.problems import ProblemError, invalid_state, not_found, validation
from swiftflow_common.ratelimit import RateLimiter
from swiftflow_common.runtime import close_core, core_health, install_core, open_pool, open_redis

from . import domain, repo
from .config import MIGRATIONS, SCHEMA, settings
from .keys import KeyStore

hasher = PasswordHasher()
_DUMMY_HASH = hasher.hash("swiftflow-dummy-secret")  # keeps timing equal for unknown clients

Sku = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._\-]+$")]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    pool = await open_pool(settings.database_url, SCHEMA, MIGRATIONS)
    keys = KeyStore(pool, settings.jwt_private_key_pem)
    await keys.load()
    redis = open_redis(settings.redis_url)
    install_core(app, settings, pool=pool, redis=redis, namespace="order", key_provider=keys.get_key)
    app.state.keys = keys
    app.state.auth_limiter = RateLimiter(redis, rate=1.0, burst=20, prefix="rl-auth")
    yield
    await close_core(app)


app = build_app(settings, title="SwiftFlow Order", lifespan=lifespan)
app.include_router(core_health(app))


# ---------------------------------------------------------------- auth
class TokenRequest(BaseModel):
    grant_type: str = "client_credentials"
    client_id: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    client_secret: Annotated[str, StringConstraints(min_length=1, max_length=200)]


def _bad_credentials() -> ProblemError:
    return ProblemError(
        401, "/problems/unauthenticated", "Sign-in failed", "That client ID and secret don't match. Check both and try again."
    )


@app.post("/v1/auth/token")
async def token(request: Request) -> JSONResponse:
    raw = await request.body()
    ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
    try:
        if ctype == "application/x-www-form-urlencoded":
            data: Any = {k: v[0] for k, v in parse_qs(raw.decode("utf-8")).items()}
        else:
            data = json.loads(raw or b"{}")
        body = TokenRequest.model_validate(data)
    except (ValueError, ValidationError, UnicodeDecodeError):
        raise validation("client_id and client_secret are required.") from None
    if body.grant_type != "client_credentials":
        raise validation("Only the client_credentials grant is supported.")

    # Throttle guesses per client ID (independent of the per-tenant bucket).
    key = hashlib.sha256(body.client_id.encode()).hexdigest()[:32]
    await request.app.state.auth_limiter.enforce(request, f"client:{key}")

    async with request.app.state.pool.acquire() as conn:
        client = await conn.fetchrow(
            """
            SELECT c.client_id, c.secret_hash, c.role, c.scopes, c.revoked_at, t.id AS tenant_id, t.slug, t.name
            FROM api_clients c JOIN tenants t ON t.id = c.tenant_id WHERE c.client_id = $1
            """,
            body.client_id,
        )
    stored = client["secret_hash"] if client else _DUMMY_HASH

    def check() -> bool:
        try:
            return hasher.verify(stored, body.client_secret)
        except (VerificationError, InvalidHashError):
            return False

    ok = await asyncio.to_thread(check)  # argon2 is CPU-bound; keep the event loop free
    if not ok or client is None or client["revoked_at"] is not None:
        raise _bad_credentials()

    scopes = list(client["scopes"])
    access = request.app.state.keys.issue(
        issuer=settings.jwt_issuer,
        audience=settings.jwt_audience,
        ttl=settings.token_ttl_seconds,
        client_id=client["client_id"],
        tenant_id=str(client["tenant_id"]),
        tenant_slug=client["slug"],
        role=client["role"],
        scopes=scopes,
    )
    return JSONResponse(
        {
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": settings.token_ttl_seconds,
            "scope": " ".join(scopes),
            "role": client["role"],
            "tenant_id": str(client["tenant_id"]),
            "tenant_slug": client["slug"],
            "tenant_name": client["name"],
        },
        headers={"Cache-Control": "no-store"},
    )


@app.get("/.well-known/jwks.json")
async def jwks(request: Request) -> JSONResponse:
    return JSONResponse({"keys": request.app.state.keys.jwks}, headers={"Cache-Control": "public, max-age=300"})


# ---------------------------------------------------------------- orders
class ItemIn(BaseModel):
    sku: Sku
    qty: int = Field(ge=1, le=1000)


class OrderIn(BaseModel):
    customer_ref: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] | None = None
    items: list[ItemIn] = Field(min_length=1, max_length=50)

    @field_validator("items")
    @classmethod
    def unique_skus(cls, items: list[ItemIn]) -> list[ItemIn]:
        if len({i.sku for i in items}) != len(items):
            raise ValueError("each SKU may appear only once in an order")
        return items


@app.post("/v1/orders", status_code=202)
async def create_order(body: OrderIn, request: Request, p: Principal = Depends(require_scope("orders:write"))) -> JSONResponse:
    key = validate_key(request.headers.get("idempotency-key"))
    payload = body.model_dump()
    h = request_hash("POST", "/v1/orders", payload)
    store = request.app.state.idempotency
    stored = await store.begin(p.tenant_id, key, h)  # fast path (Redis)
    if stored is not None:
        return stored.to_response()
    replayed = False
    try:
        async with tenant_tx(request.app.state.pool, p.tenant_id) as conn:
            order_id = uuid.uuid4()
            created = await conn.fetchval(
                """
                INSERT INTO orders (id, tenant_id, status, customer_ref, idempotency_key, request_hash)
                VALUES ($1, $2, 'PENDING', $3, $4, $5)
                ON CONFLICT (tenant_id, idempotency_key) DO NOTHING RETURNING id
                """,
                order_id,
                p.tenant_id,
                body.customer_ref or None,
                key,
                h,
            )
            if created is None:  # backstop: Redis lost the key, the database did not
                existing = await conn.fetchrow(f"SELECT {repo.ORDER_COLUMNS} FROM orders o WHERE o.idempotency_key = $1", key)
                if existing["request_hash"] != h:
                    raise mismatch()
                result = await repo.order_detail(conn, existing, with_history=False)
                replayed = True
            else:
                await conn.executemany(
                    "INSERT INTO order_items (id, order_id, tenant_id, sku, qty) VALUES ($1, $2, $3, $4, $5)",
                    [(uuid.uuid4(), order_id, p.tenant_id, i.sku, i.qty) for i in body.items],
                )
                row = await repo.get_order(conn, order_id)
                await repo.add_history(conn, row, domain.PENDING, event_type="OrderPlaced")
                await repo.emit(conn, "OrderPlaced", row, {"customer_ref": body.customer_ref or None, "items": payload["items"]})
                result = await repo.order_detail(conn, row, with_history=False)
    except BaseException:
        await store.abandon(p.tenant_id, key)
        raise
    await store.complete(p.tenant_id, key, h, 202, result)
    return JSONResponse(result, status_code=202, headers={"Idempotent-Replayed": "true"} if replayed else None)


@app.get("/v1/orders/stats")
async def stats(request: Request, p: Principal = Depends(require_scope("orders:read"))) -> dict:
    async with tenant_tx(request.app.state.pool, p.tenant_id) as conn:
        counts = await conn.fetch("SELECT status, count(*)::int AS n FROM orders GROUP BY status")
        minutes = await conn.fetch(
            """
            SELECT date_trunc('minute', created_at) AS minute, status, count(*)::int AS n
            FROM orders WHERE created_at > now() - interval '60 minutes' GROUP BY 1, 2 ORDER BY 1
            """
        )
        saga = await conn.fetchrow(
            """
            SELECT percentile_cont(0.5)  WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM finalized_at - created_at)) AS p50,
                   percentile_cont(0.95) WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM finalized_at - created_at)) AS p95
            FROM orders WHERE finalized_at IS NOT NULL AND created_at > now() - interval '60 minutes'
            """
        )
    per: dict[str, dict[str, int]] = {}
    for r in minutes:
        per.setdefault(repo.iso(r["minute"]), {})[r["status"]] = r["n"]
    return {
        "counts": {s: 0 for s in domain.STATUSES} | {r["status"]: r["n"] for r in counts},
        "per_minute": [{"minute": m, "counts": c, "total": sum(c.values())} for m, c in per.items()],
        "window_total": sum(sum(c.values()) for c in per.values()),
        "saga_p50_ms": round(saga["p50"] * 1000) if saga["p50"] is not None else None,
        "saga_p95_ms": round(saga["p95"] * 1000) if saga["p95"] is not None else None,
    }


@app.get("/v1/orders")
async def list_orders(
    request: Request,
    status: Annotated[list[str] | None, Query()] = None,
    q: Annotated[str | None, Query(max_length=100)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: str | None = None,
    p: Principal = Depends(require_scope("orders:read")),
) -> dict:
    if status and any(s not in domain.STATUSES for s in status):
        raise validation("Unknown status filter.")
    after = decode_cursor(cursor, 2)
    after_ts, after_id = None, None
    if after:
        try:
            after_ts, after_id = datetime.fromisoformat(str(after[0])), uuid.UUID(str(after[1]))
        except ValueError:
            raise validation("The cursor is malformed.") from None
    prefix = contains = None
    if q and q.strip():
        escaped = q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        prefix, contains = escaped.lower() + "%", "%" + escaped + "%"
    async with tenant_tx(request.app.state.pool, p.tenant_id) as conn:
        rows = await conn.fetch(
            f"""
            SELECT {repo.ORDER_COLUMNS} FROM orders o
            WHERE ($1::text[] IS NULL OR o.status = ANY($1))
              AND ($2::text IS NULL OR o.id::text LIKE $2 OR o.customer_ref ILIKE $3)
              AND ($4::timestamptz IS NULL OR (o.created_at, o.id) < ($4, $5))
            ORDER BY o.created_at DESC, o.id DESC LIMIT $6
            """,
            status or None,
            prefix,
            contains,
            after_ts,
            after_id,
            limit + 1,
        )
    page = rows[:limit]
    nxt = encode_cursor([page[-1]["created_at"].isoformat(), str(page[-1]["id"])]) if len(rows) > limit else None
    return {"items": [repo.summary(r) for r in page], "next_cursor": nxt}


@app.get("/v1/orders/{order_id}")
async def get_order(order_id: uuid.UUID, request: Request, p: Principal = Depends(require_scope("orders:read"))) -> dict:
    async with tenant_tx(request.app.state.pool, p.tenant_id) as conn:
        row = await repo.get_order(conn, order_id)
        if row is None:  # also the answer for another tenant's order: existence is not revealed
            raise not_found("No order with that ID.")
        return await repo.order_detail(conn, row)


@app.post("/v1/orders/{order_id}/cancel", status_code=202)
async def cancel_order(order_id: uuid.UUID, request: Request, p: Principal = Depends(require_scope("orders:write"))) -> dict:
    validate_key(request.headers.get("idempotency-key"), required=False)
    async with tenant_tx(request.app.state.pool, p.tenant_id) as conn:
        row = await repo.get_order(conn, order_id, for_update=True)
        if row is None:
            raise not_found("No order with that ID.")
        if not domain.can_cancel(row["status"]):
            raise invalid_state(
                "Order cannot be cancelled",
                f"Order {order_id} is {row['status']}; only PENDING, RESERVED or CONFIRMED orders can be cancelled.",
            )
        pay = domain.payment_status_after_cancel(row["status"], row["payment_status"], row["payment_attempts"])
        await conn.execute(
            """
            UPDATE orders SET status = 'CANCELLED', failure_reason = 'customer_request', payment_status = $2,
                   next_payment_at = CASE WHEN $2 = 'REFUND_PENDING' THEN now() ELSE NULL END,
                   payment_attempts = 0, version = version + 1, updated_at = now(),
                   finalized_at = COALESCE(finalized_at, now())
            WHERE id = $1
            """,
            order_id,
            pay,
        )
        await repo.add_history(
            conn, row, domain.CANCELLED, from_status=row["status"], reason="customer_request", event_type="OrderCancelled"
        )
        await repo.emit(conn, "OrderCancelled", row, {"reason": "customer_request", "previous_status": row["status"]})
        return await repo.order_detail(conn, await repo.get_order(conn, order_id))


@app.post("/v1/orders/{order_id}/ship", status_code=202)
async def ship_order(order_id: uuid.UUID, request: Request, p: Principal = Depends(require_scope("admin"))) -> dict:
    validate_key(request.headers.get("idempotency-key"), required=False)
    async with tenant_tx(request.app.state.pool, p.tenant_id) as conn:
        row = await repo.get_order(conn, order_id, for_update=True)
        if row is None:
            raise not_found("No order with that ID.")
        if not domain.can_ship(row["status"]):
            raise invalid_state(
                "Order cannot be shipped", f"Order {order_id} is {row['status']}; only CONFIRMED orders can be shipped."
            )
        await conn.execute(
            "UPDATE orders SET status = 'SHIPPED', version = version + 1, updated_at = now() WHERE id = $1", order_id
        )
        await repo.add_history(conn, row, domain.SHIPPED, from_status=row["status"], event_type="OrderShipped")
        await repo.emit(conn, "OrderShipped", row, {})
        return await repo.order_detail(conn, await repo.get_order(conn, order_id))
