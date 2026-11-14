"""Transactional outbox: ``add_event`` inside the business transaction, and the
``OutboxRelay`` that publishes committed rows to Kafka."""

from __future__ import annotations

import asyncio
from typing import Any

import asyncpg
import structlog
from prometheus_client import Counter, Gauge

from .db import system_tx
from .events import EVENT_TOPICS, build_envelope
from .kafka import encode_headers
from .observability import current_trace_headers

log = structlog.get_logger("outbox")

OUTBOX_PUBLISHED = Counter("swiftflow_outbox_published_total", "Outbox rows published", ["service", "topic"])
OUTBOX_PENDING = Gauge("swiftflow_outbox_pending", "Unpublished outbox rows", ["service"])
OUTBOX_OLDEST_AGE = Gauge("swiftflow_outbox_oldest_unpublished_seconds", "Age of the oldest unpublished outbox row", ["service"])


async def add_event(
    conn: asyncpg.Connection,
    event_type: str,
    *,
    tenant_id: str,
    aggregate_id: str,
    producer: str,
    payload: dict[str, Any],
    key: str | None = None,
    topic: str | None = None,
) -> dict[str, Any]:
    """Validate against the contract and insert an outbox row on ``conn`` (which
    must be inside the caller's transaction). Returns the envelope."""
    envelope = build_envelope(event_type, tenant_id=tenant_id, aggregate_id=aggregate_id, producer=producer, payload=payload)
    topic = topic or EVENT_TOPICS[event_type]
    headers = current_trace_headers()
    await conn.execute(
        """
        INSERT INTO outbox_events (event_id, tenant_id, topic, message_key, event_type, envelope, headers)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        """,
        envelope["event_id"],
        str(tenant_id),
        topic,
        key or str(aggregate_id),
        event_type,
        envelope,
        headers,
    )
    return envelope


class OutboxRelay:
    """Publishes unpublished rows in id order, 100 at a time.

    Rows are claimed with ``FOR UPDATE SKIP LOCKED``; ``published_at`` is set in
    the same transaction after Kafka acknowledged every send (acks=all,
    idempotent producer). A crash in between republishes the batch: duplicates
    are possible, losses are not, and consumers dedupe on ``event_id``.
    """

    def __init__(self, pool: asyncpg.Pool, producer: Any, service: str, batch_size: int = 100, idle_sleep: float = 0.1) -> None:
        self.pool = pool
        self.producer = producer
        self.service = service
        self.batch_size = batch_size
        self.idle_sleep = idle_sleep
        self.running = False
        self._last_housekeeping = 0.0

    async def publish_batch(self) -> int:
        async with system_tx(self.pool) as conn:
            rows = await conn.fetch(
                """
                SELECT id, topic, message_key, event_type, tenant_id, envelope, headers
                FROM outbox_events
                WHERE published_at IS NULL
                ORDER BY id
                LIMIT $1
                FOR UPDATE SKIP LOCKED
                """,
                self.batch_size,
            )
            if not rows:
                return 0
            futures = []
            for r in rows:
                headers = dict(r["headers"] or {})
                headers["event_type"] = r["event_type"]
                headers["tenant_id"] = str(r["tenant_id"])
                fut = await self.producer.send(
                    r["topic"],
                    value=_dumps(r["envelope"]),
                    key=r["message_key"].encode("utf-8"),
                    headers=encode_headers(headers),
                )
                futures.append(fut)
            await asyncio.gather(*futures)
            await conn.execute(
                "UPDATE outbox_events SET published_at = now() WHERE id = ANY($1::bigint[])",
                [r["id"] for r in rows],
            )
            for r in rows:
                OUTBOX_PUBLISHED.labels(self.service, r["topic"]).inc()
            return len(rows)

    async def update_gauges(self) -> None:
        async with system_tx(self.pool) as conn:
            row = await conn.fetchrow(
                """
                SELECT count(*) AS n, COALESCE(EXTRACT(EPOCH FROM now() - min(created_at)), 0) AS age
                FROM outbox_events WHERE published_at IS NULL
                """
            )
        OUTBOX_PENDING.labels(self.service).set(row["n"])
        OUTBOX_OLDEST_AGE.labels(self.service).set(float(row["age"]))

    async def run(self, stop: asyncio.Event) -> None:
        self.running = True
        log.info("outbox_relay_started")
        loop = asyncio.get_running_loop()
        last_gauge = 0.0
        try:
            while not stop.is_set():
                try:
                    n = await self.publish_batch()
                    now = loop.time()
                    if now - last_gauge > 5:
                        last_gauge = now
                        await self.update_gauges()
                    if now - self._last_housekeeping > 3600:
                        self._last_housekeeping = now
                        await housekeeping(self.pool)
                    if n < self.batch_size:
                        await _sleep_or_stop(stop, self.idle_sleep)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.warning("outbox_relay_error", error=str(exc))
                    await _sleep_or_stop(stop, 1.0)
        finally:
            self.running = False


async def housekeeping(pool: asyncpg.Pool) -> None:
    """Delete published outbox rows older than 7 days and inbox rows older than 14."""
    async with system_tx(pool) as conn:
        await conn.execute(
            "DELETE FROM outbox_events WHERE published_at IS NOT NULL AND published_at < now() - interval '7 days'"
        )
        exists = await conn.fetchval("SELECT to_regclass('processed_events') IS NOT NULL")
        if exists:
            await conn.execute("DELETE FROM processed_events WHERE processed_at < now() - interval '14 days'")


async def _sleep_or_stop(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except TimeoutError:
        pass


def _dumps(value: Any) -> bytes:
    import json

    return json.dumps(value, separators=(",", ":"), default=str).encode("utf-8")
