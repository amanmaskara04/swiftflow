"""Notification API (:8003): event feed, live stream (SSE), webhook subscriptions and deliveries."""

from __future__ import annotations

import asyncio
import json
import secrets
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import datetime
from typing import Annotated, Any

import asyncpg
import redis.asyncio as aioredis
from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field, StringConstraints
from swiftflow_common.app import build_app, decode_cursor, encode_cursor
from swiftflow_common.auth import Principal, require_scope
from swiftflow_common.db import tenant_tx
from swiftflow_common.events import EVENT_TYPES, TOPIC_ORDERS
from swiftflow_common.problems import ProblemError, invalid_state, not_found, validation
from swiftflow_common.runtime import close_core, core_health, install_core, open_pool, open_redis

from .config import MIGRATIONS, SCHEMA, settings
from .ssrf import UnsafeUrl, check_url

SUBSCRIBABLE = (*EVENT_TYPES, "*")


def channel(tenant_id: str) -> str:
    return f"events:{tenant_id}"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    pool = await open_pool(settings.database_url, SCHEMA, MIGRATIONS)
    install_core(app, settings, pool=pool, redis=open_redis(settings.redis_url), namespace="notification")
    app.state.streams = 0
    app.state.tenant_streams = {}
    yield
    await close_core(app)


app = build_app(settings, title="SwiftFlow Notification", lifespan=lifespan)
app.include_router(core_health(app))


def iso(value: Any) -> str | None:
    return value.isoformat().replace("+00:00", "Z") if value is not None else None


def envelope(row: asyncpg.Record) -> dict[str, Any]:
    return {
        "event_id": str(row["event_id"]),
        "event_type": row["event_type"],
        "schema_version": 1,
        "occurred_at": iso(row["occurred_at"]),
        "tenant_id": str(row["tenant_id"]),
        "aggregate_id": str(row["aggregate_id"]),
        "producer": "order" if row["topic"] == TOPIC_ORDERS else "inventory",
        "topic": row["topic"],
        "payload": row["payload"],
    }


# ---------------------------------------------------------------- event feed
@app.get("/v1/events")
async def list_events(
    request: Request,
    aggregate_id: uuid.UUID | None = None,
    type: Annotated[str | None, Query(max_length=64)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: str | None = None,
    p: Principal = Depends(require_scope("orders:read")),
) -> dict:
    after = decode_cursor(cursor, 2)
    after_ts = after_id = None
    if after:
        try:
            after_ts, after_id = datetime.fromisoformat(str(after[0])), uuid.UUID(str(after[1]))
        except ValueError:
            raise validation("The cursor is malformed.") from None
    async with tenant_tx(request.app.state.pool, p.tenant_id) as conn:
        rows = await conn.fetch(
            """
            SELECT event_id, tenant_id, event_type, topic, aggregate_id, payload, occurred_at, received_at
            FROM event_log
            WHERE ($1::uuid IS NULL OR aggregate_id = $1)
              AND ($2::text IS NULL OR event_type = $2)
              AND ($3::timestamptz IS NULL OR (received_at, event_id) < ($3, $4))
            ORDER BY received_at DESC, event_id DESC LIMIT $5
            """,
            aggregate_id,
            type,
            after_ts,
            after_id,
            limit + 1,
        )
    page = rows[:limit]
    nxt = encode_cursor([page[-1]["received_at"].isoformat(), str(page[-1]["event_id"])]) if len(rows) > limit else None
    return {"items": [envelope(r) for r in page], "next_cursor": nxt}


@app.get("/v1/events/stream")
async def stream(request: Request, p: Principal = Depends(require_scope("orders:read"))) -> StreamingResponse:
    """Server-Sent Events for this tenant, fed by Redis pub/sub from the worker.
    The token comes in the Authorization header, never in the URL."""
    per_tenant: dict[str, int] = request.app.state.tenant_streams
    if request.app.state.streams >= 500 or per_tenant.get(p.tenant_id, 0) >= 20:
        raise ProblemError(
            429, "/problems/rate-limited", "Too many live connections", "Close another console tab, then try again.",
            headers={"Retry-After": "5"},
        )
    client = aioredis.from_url(settings.redis_url, decode_responses=True)
    pubsub = client.pubsub()
    await pubsub.subscribe(channel(p.tenant_id))

    async def gen() -> AsyncIterator[str]:
        request.app.state.streams += 1
        per_tenant[p.tenant_id] = per_tenant.get(p.tenant_id, 0) + 1
        idle = 0.0
        try:
            yield "retry: 2000\n\n: connected\n\n"
            while True:
                msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if msg is not None:
                    idle = 0.0
                    data = msg["data"]
                    try:
                        etype = json.loads(data).get("event_type", "message")
                    except ValueError:
                        continue
                    yield f"event: {etype}\ndata: {data}\n\n"
                else:
                    idle += 1.0
                    if idle >= 15:
                        idle = 0.0
                        yield ": keep-alive\n\n"
                    if await request.is_disconnected():
                        break
        except asyncio.CancelledError:
            raise
        finally:
            request.app.state.streams -= 1
            per_tenant[p.tenant_id] = max(0, per_tenant.get(p.tenant_id, 1) - 1)
            with suppress(Exception):
                await pubsub.unsubscribe()
                await pubsub.aclose()
                await client.aclose()

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


# ---------------------------------------------------------------- subscriptions
class SubscriptionIn(BaseModel):
    url: Annotated[str, StringConstraints(strip_whitespace=True, min_length=8, max_length=2000)]
    event_types: list[str] = Field(min_length=1, max_length=20)


def sub_out(row: asyncpg.Record, *, secret: bool = False) -> dict[str, Any]:
    out = {
        "id": str(row["id"]),
        "url": row["url"],
        "event_types": list(row["event_types"]),
        "active": row["active"],
        "created_at": iso(row["created_at"]),
    }
    if secret:
        out["signing_secret"] = row["signing_secret"]
    return out


@app.post("/v1/webhooks/subscriptions", status_code=201)
async def create_subscription(
    body: SubscriptionIn, request: Request, p: Principal = Depends(require_scope("webhooks:write"))
) -> dict:
    if any(t not in SUBSCRIBABLE for t in body.event_types):
        raise validation(
            "Unknown event type.", [{"loc": ["body", "event_types"], "msg": "unknown event type", "type": "value_error"}]
        )
    try:
        url = await check_url(body.url, allowed_hosts=settings.allowed_hosts, allow_http=settings.webhook_allow_http)
    except UnsafeUrl as exc:
        raise validation(str(exc), [{"loc": ["body", "url"], "msg": str(exc), "type": "value_error"}]) from None
    async with tenant_tx(request.app.state.pool, p.tenant_id) as conn:
        if await conn.fetchval("SELECT count(*) FROM webhook_subscriptions") >= settings.max_subscriptions_per_tenant:
            raise ProblemError(
                409,
                "/problems/conflict",
                "Endpoint limit reached",
                f"A tenant can have at most {settings.max_subscriptions_per_tenant} endpoints.",
            )
        row = await conn.fetchrow(
            "INSERT INTO webhook_subscriptions (id, tenant_id, url, event_types, signing_secret) VALUES ($1,$2,$3,$4,$5) "
            "RETURNING id, url, event_types, signing_secret, active, created_at",
            uuid.uuid4(),
            p.tenant_id,
            url,
            sorted(set(body.event_types)),
            "whsec_" + secrets.token_urlsafe(32),
        )
    return sub_out(row, secret=True)  # the only time the secret is returned


@app.get("/v1/webhooks/subscriptions")
async def list_subscriptions(request: Request, p: Principal = Depends(require_scope("webhooks:read"))) -> dict:
    async with tenant_tx(request.app.state.pool, p.tenant_id) as conn:
        rows = await conn.fetch(
            "SELECT id, url, event_types, active, created_at FROM webhook_subscriptions ORDER BY created_at DESC"
        )
    return {"items": [sub_out(r) for r in rows], "next_cursor": None}


@app.get("/v1/webhooks/subscriptions/{sub_id}")
async def get_subscription(sub_id: uuid.UUID, request: Request, p: Principal = Depends(require_scope("webhooks:read"))) -> dict:
    async with tenant_tx(request.app.state.pool, p.tenant_id) as conn:
        row = await conn.fetchrow(
            "SELECT id, url, event_types, active, created_at FROM webhook_subscriptions WHERE id = $1", sub_id
        )
    if row is None:
        raise not_found("No endpoint with that ID.")
    return sub_out(row)


@app.delete("/v1/webhooks/subscriptions/{sub_id}", status_code=204)
async def delete_subscription(
    sub_id: uuid.UUID, request: Request, p: Principal = Depends(require_scope("webhooks:write"))
) -> Response:
    async with tenant_tx(request.app.state.pool, p.tenant_id) as conn:
        await conn.execute("DELETE FROM webhook_deliveries WHERE subscription_id = $1", sub_id)
        deleted = await conn.fetchval("DELETE FROM webhook_subscriptions WHERE id = $1 RETURNING id", sub_id)
    if deleted is None:
        raise not_found("No endpoint with that ID.")
    return Response(status_code=204)


@app.post("/v1/webhooks/subscriptions/{sub_id}/test", status_code=202)
async def test_subscription(sub_id: uuid.UUID, request: Request, p: Principal = Depends(require_scope("webhooks:write"))) -> dict:
    async with tenant_tx(request.app.state.pool, p.tenant_id) as conn:
        if await conn.fetchval("SELECT 1 FROM webhook_subscriptions WHERE id = $1", sub_id) is None:
            raise not_found("No endpoint with that ID.")
        row = await conn.fetchrow(
            "INSERT INTO webhook_deliveries (id, tenant_id, subscription_id, event_id, event_type, status, next_attempt_at) "
            f"VALUES ($1,$2,$3,$4,'WebhookTest','PENDING', now()) RETURNING {DELIVERY_COLUMNS}",
            uuid.uuid4(),
            p.tenant_id,
            sub_id,
            uuid.uuid4(),
        )
    return delivery_out(row)


# ---------------------------------------------------------------- deliveries
DELIVERY_COLUMNS = (
    "id, subscription_id, event_id, event_type, status, attempts, next_attempt_at, last_status_code, "
    "last_error, created_at, delivered_at"
)


def delivery_out(r: asyncpg.Record) -> dict[str, Any]:
    return {
        "id": str(r["id"]),
        "subscription_id": str(r["subscription_id"]),
        "event_id": str(r["event_id"]),
        "event_type": r["event_type"],
        "status": r["status"],
        "attempts": r["attempts"],
        "next_attempt_at": iso(r["next_attempt_at"]),
        "last_status_code": r["last_status_code"],
        "last_error": r["last_error"],
        "created_at": iso(r["created_at"]),
        "delivered_at": iso(r["delivered_at"]),
    }


@app.get("/v1/webhooks/deliveries")
async def list_deliveries(
    request: Request,
    status: Annotated[str | None, Query(pattern="^(PENDING|DELIVERED|FAILED)$")] = None,
    subscription_id: uuid.UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: str | None = None,
    p: Principal = Depends(require_scope("webhooks:read")),
) -> dict:
    after = decode_cursor(cursor, 2)
    after_ts = after_id = None
    if after:
        try:
            after_ts, after_id = datetime.fromisoformat(str(after[0])), uuid.UUID(str(after[1]))
        except ValueError:
            raise validation("The cursor is malformed.") from None
    async with tenant_tx(request.app.state.pool, p.tenant_id) as conn:
        rows = await conn.fetch(
            f"""
            SELECT {DELIVERY_COLUMNS} FROM webhook_deliveries
            WHERE ($1::text IS NULL OR status = $1)
              AND ($2::uuid IS NULL OR subscription_id = $2)
              AND ($3::timestamptz IS NULL OR (created_at, id) < ($3, $4))
            ORDER BY created_at DESC, id DESC LIMIT $5
            """,
            status,
            subscription_id,
            after_ts,
            after_id,
            limit + 1,
        )
    page = rows[:limit]
    nxt = encode_cursor([page[-1]["created_at"].isoformat(), str(page[-1]["id"])]) if len(rows) > limit else None
    return {"items": [delivery_out(r) for r in page], "next_cursor": nxt}


@app.post("/v1/webhooks/deliveries/{delivery_id}/retry", status_code=202)
async def retry_delivery(
    delivery_id: uuid.UUID, request: Request, p: Principal = Depends(require_scope("webhooks:write"))
) -> dict:
    async with tenant_tx(request.app.state.pool, p.tenant_id) as conn:
        row = await conn.fetchrow("SELECT status FROM webhook_deliveries WHERE id = $1 FOR UPDATE", delivery_id)
        if row is None:
            raise not_found("No delivery with that ID.")
        if row["status"] != "FAILED":
            raise invalid_state(
                "Delivery cannot be retried", f"Only FAILED deliveries can be retried; this one is {row['status']}."
            )
        updated = await conn.fetchrow(
            "UPDATE webhook_deliveries SET status = 'PENDING', attempts = 0, next_attempt_at = now(), last_error = NULL "
            f"WHERE id = $1 RETURNING {DELIVERY_COLUMNS}",
            delivery_id,
        )
    return delivery_out(updated)
