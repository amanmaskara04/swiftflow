"""Notification worker (:9103): the feed consumer and the webhook dispatcher."""

from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import asyncpg
import httpx
import structlog
from prometheus_client import Counter
from swiftflow_common.circuit_breaker import CircuitBreaker
from swiftflow_common.consumer import EventConsumer
from swiftflow_common.db import TxContext, system_tx
from swiftflow_common.events import ALL_TOPICS, EventEnvelope, dlq_topic
from swiftflow_common.kafka import ensure_topics, make_producer
from swiftflow_common.runtime import open_pool, open_redis, run_worker
from swiftflow_common.signing import sign

from .config import MIGRATIONS, SCHEMA, Settings
from .ssrf import UnsafeUrl, check_url

settings = Settings(service_name="notification-worker")
log = structlog.get_logger("notification-worker")

WEBHOOKS = Counter("swiftflow_webhook_deliveries_total", "Webhook delivery attempts", ["status"])

# Delay before attempt 2, 3, 4, 5, 6. After the sixth failure the delivery is FAILED.
RETRY_SCHEDULE = (5, 30, 120, 600, 3600)
LEASE_SECONDS = 60


def next_delay(attempts: int) -> int | None:
    """Seconds to wait after ``attempts`` failed tries, or None to give up."""
    return RETRY_SCHEDULE[attempts - 1] if 1 <= attempts <= len(RETRY_SCHEDULE) else None


def make_handler(redis: Any):
    async def handle(conn: asyncpg.Connection, env: EventEnvelope, ctx: TxContext, topic: str) -> None:
        inserted = await conn.fetchval(
            """
            INSERT INTO event_log (event_id, tenant_id, event_type, topic, aggregate_id, payload, occurred_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7) ON CONFLICT (event_id) DO NOTHING RETURNING 1
            """,
            env.event_id,
            env.tenant_id,
            env.event_type,
            topic,
            env.aggregate_id,
            env.payload,
            datetime.fromisoformat(env.occurred_at),
        )
        if not inserted:
            return
        subs = await conn.fetch(
            "SELECT id FROM webhook_subscriptions WHERE active AND event_types && $1::text[]", [env.event_type, "*"]
        )
        for s in subs:
            await conn.execute(
                "INSERT INTO webhook_deliveries (id, tenant_id, subscription_id, event_id, event_type, status, next_attempt_at) "
                "VALUES ($1, $2, $3, $4, $5, 'PENDING', now()) ON CONFLICT (subscription_id, event_id) DO NOTHING",
                uuid.uuid4(),
                env.tenant_id,
                s["id"],
                env.event_id,
                env.event_type,
            )
        message = json.dumps({**env.model_dump(), "topic": topic}, separators=(",", ":"))

        async def publish() -> None:  # live fan-out is best effort; the feed table is the record
            with suppress(Exception):
                await redis.publish(f"events:{env.tenant_id}", message)

        ctx.after_commit.append(publish)

    return handle


class Dispatcher:
    """Delivers due webhooks: claim with a lease, send with no transaction open, record the result."""

    def __init__(self, pool: asyncpg.Pool, client: httpx.AsyncClient) -> None:
        self.pool = pool
        self.client = client
        self.breakers: dict[str, CircuitBreaker] = {}
        self.running = False

    async def claim(self, limit: int = 20) -> list[asyncpg.Record]:
        async with system_tx(self.pool) as conn:
            rows = await conn.fetch(
                """
                SELECT d.id, d.tenant_id, d.event_id, d.event_type, d.attempts, s.url, s.signing_secret, s.active,
                       e.event_type AS log_type, e.topic, e.aggregate_id, e.payload, e.occurred_at
                FROM webhook_deliveries d
                JOIN webhook_subscriptions s ON s.id = d.subscription_id
                LEFT JOIN event_log e ON e.event_id = d.event_id
                WHERE d.status = 'PENDING' AND d.next_attempt_at <= now()
                ORDER BY d.next_attempt_at LIMIT $1
                FOR UPDATE OF d SKIP LOCKED
                """,
                limit,
            )
            if rows:
                await conn.execute(
                    "UPDATE webhook_deliveries SET attempts = attempts + 1, "
                    "next_attempt_at = now() + make_interval(secs => $2) WHERE id = ANY($1::uuid[])",
                    [r["id"] for r in rows],
                    float(LEASE_SECONDS),
                )
            return rows

    def body_for(self, d: asyncpg.Record) -> bytes:
        if d["log_type"] is None:  # a test delivery has no logged event
            env = {
                "event_id": str(d["event_id"]),
                "event_type": d["event_type"],
                "schema_version": 1,
                "occurred_at": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                "tenant_id": str(d["tenant_id"]),
                "aggregate_id": str(d["event_id"]),
                "producer": "notification",
                "payload": {"message": "This is a test delivery from SwiftFlow."},
            }
        else:
            env = {
                "event_id": str(d["event_id"]),
                "event_type": d["log_type"],
                "schema_version": 1,
                "occurred_at": d["occurred_at"].isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                "tenant_id": str(d["tenant_id"]),
                "aggregate_id": str(d["aggregate_id"]),
                "producer": "order" if d["topic"] == "orders.events" else "inventory",
                "payload": d["payload"],
            }
        return json.dumps(env, separators=(",", ":")).encode("utf-8")

    async def send(self, d: asyncpg.Record) -> tuple[bool, int | None, str | None]:
        if not d["active"]:
            return False, None, "endpoint is disabled"
        try:  # re-check at delivery time: DNS may have changed since the endpoint was added
            url = await check_url(d["url"], allowed_hosts=settings.allowed_hosts, allow_http=settings.webhook_allow_http)
        except UnsafeUrl as exc:
            return False, None, f"blocked: {exc}"
        host = urlsplit(url).netloc
        breaker = self.breakers.setdefault(host, CircuitBreaker(f"webhook:{host}", failure_threshold=5, reset_timeout=30.0))
        if not breaker.allow():
            return False, None, "circuit open for this host"
        body = self.body_for(d)
        try:
            resp = await self.client.post(
                url,
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "SwiftFlow-Webhooks/1",
                    "SwiftFlow-Event": d["event_type"],
                    "SwiftFlow-Delivery": str(d["id"]),
                    "SwiftFlow-Signature": sign(d["signing_secret"], body),
                },
            )
        except httpx.HTTPError as exc:
            breaker.record_failure()
            return False, None, type(exc).__name__
        if 200 <= resp.status_code < 300:
            breaker.record_success()
            return True, resp.status_code, None
        breaker.record_failure() if resp.status_code >= 500 else breaker.record_success()
        return False, resp.status_code, f"endpoint returned {resp.status_code}"

    async def deliver(self, d: asyncpg.Record) -> None:
        attempts = d["attempts"] + 1
        try:
            ok, code, error = await self.send(d)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            ok, code, error = False, None, type(exc).__name__
        async with system_tx(self.pool) as conn:
            if ok:
                await conn.execute(
                    "UPDATE webhook_deliveries SET status = 'DELIVERED', delivered_at = now(), next_attempt_at = NULL, "
                    "last_status_code = $2, last_error = NULL WHERE id = $1",
                    d["id"],
                    code,
                )
                WEBHOOKS.labels("delivered").inc()
                return
            delay = next_delay(attempts)
            if delay is None:
                await conn.execute(
                    "UPDATE webhook_deliveries SET status = 'FAILED', next_attempt_at = NULL, last_status_code = $2, "
                    "last_error = $3 WHERE id = $1",
                    d["id"],
                    code,
                    error,
                )
                WEBHOOKS.labels("failed").inc()
            else:
                await conn.execute(
                    "UPDATE webhook_deliveries SET next_attempt_at = now() + make_interval(secs => $4), "
                    "last_status_code = $2, last_error = $3 WHERE id = $1",
                    d["id"],
                    code,
                    error,
                    float(delay),
                )
                WEBHOOKS.labels("retry").inc()

    async def run(self, stop: asyncio.Event) -> None:
        self.running = True
        try:
            while not stop.is_set():
                try:
                    rows = await self.claim()
                    if rows:
                        await asyncio.gather(*(self.deliver(r) for r in rows))
                    else:
                        await asyncio.sleep(0.25)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.warning("dispatcher_error", error=f"{type(exc).__name__}: {exc}")
                    await asyncio.sleep(1.0)
        finally:
            self.running = False


async def start(stop: asyncio.Event, status: dict[str, Any]) -> list[Any]:
    pool = await open_pool(settings.database_url, SCHEMA, MIGRATIONS)
    redis = open_redis(settings.redis_url)
    await ensure_topics(settings.kafka_bootstrap, [*ALL_TOPICS, *(dlq_topic(t) for t in ALL_TOPICS)])
    producer = make_producer(settings.kafka_bootstrap, "notification-worker")
    await producer.start()
    consumer = EventConsumer(
        name="notification-feed",
        topics=list(ALL_TOPICS),
        bootstrap=settings.kafka_bootstrap,
        pool=pool,
        handler=make_handler(redis),
        dlq_producer=producer,
    )
    client = httpx.AsyncClient(
        timeout=settings.webhook_timeout_seconds, follow_redirects=False, trust_env=False, limits=httpx.Limits(max_connections=50)
    )
    dispatcher = Dispatcher(pool, client)
    status["kafka"] = lambda: "up" if consumer.connected else "down"
    status["dispatcher"] = lambda: "up" if dispatcher.running else "down"
    return [consumer.run(stop), dispatcher.run(stop)]


if __name__ == "__main__":
    asyncio.run(run_worker(settings, settings.worker_port, start))
