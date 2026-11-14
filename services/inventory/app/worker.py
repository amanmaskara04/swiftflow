"""Inventory worker (:9102): outbox relay + the reservation consumer."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Any

import asyncpg
from swiftflow_common.consumer import EventConsumer
from swiftflow_common.db import TxContext
from swiftflow_common.events import ALL_TOPICS, TOPIC_ORDERS, EventEnvelope, dlq_topic
from swiftflow_common.kafka import ensure_topics, make_producer
from swiftflow_common.outbox import OutboxRelay, add_event
from swiftflow_common.runtime import open_pool, open_redis, run_worker

from .config import MIGRATIONS, PRODUCER, SCHEMA, Settings
from .stock import StockChange, cache_key, commit_order, release_order, reserve_order

settings = Settings(service_name="inventory-worker")


def make_handler(redis: Any):
    async def emit_changes(conn: asyncpg.Connection, tenant_id: str, changes: list[StockChange], ctx: TxContext) -> None:
        for ch in changes:
            await add_event(
                conn,
                "StockLevelChanged",
                tenant_id=tenant_id,
                aggregate_id=ch.product_id,
                producer=PRODUCER,
                payload=ch.payload(),
                key=f"{tenant_id}:{ch.sku}",
            )
        skus = sorted({ch.sku for ch in changes})
        if skus:

            async def invalidate() -> None:
                with suppress(Exception):
                    await redis.delete(*(cache_key(tenant_id, s) for s in skus))

            ctx.after_commit.append(invalidate)

    async def handle(conn: asyncpg.Connection, env: EventEnvelope, ctx: TxContext, topic: str) -> None:
        order_id = env.aggregate_id
        tenant_id = env.tenant_id
        if env.event_type == "OrderPlaced":
            result = await reserve_order(conn, tenant_id, order_id, env.payload["items"])
            if result.ok:
                await add_event(
                    conn,
                    "StockReserved",
                    tenant_id=tenant_id,
                    aggregate_id=order_id,
                    producer=PRODUCER,
                    payload={"order_id": order_id, "items": result.items, "total_cents": result.total_cents},
                )
                await emit_changes(conn, tenant_id, result.changes, ctx)
            else:
                await add_event(
                    conn,
                    "StockReservationFailed",
                    tenant_id=tenant_id,
                    aggregate_id=order_id,
                    producer=PRODUCER,
                    payload={
                        "order_id": order_id,
                        "reason": result.reason,
                        "sku": result.sku,
                        "requested": result.requested,
                        "available": max(0, result.available),
                    },
                )
        elif env.event_type == "OrderCancelled":
            items, changes = await release_order(conn, order_id)
            await add_event(
                conn,
                "StockReleased",
                tenant_id=tenant_id,
                aggregate_id=order_id,
                producer=PRODUCER,
                payload={"order_id": order_id, "items": items},
            )
            await emit_changes(conn, tenant_id, changes, ctx)
        elif env.event_type == "OrderShipped":
            items, changes = await commit_order(conn, order_id)
            await add_event(
                conn,
                "StockCommitted",
                tenant_id=tenant_id,
                aggregate_id=order_id,
                producer=PRODUCER,
                payload={"order_id": order_id, "items": items},
            )
            await emit_changes(conn, tenant_id, changes, ctx)
        # OrderConfirmed needs nothing from inventory: the reservation stays ACTIVE until shipped.

    return handle


async def start(stop: asyncio.Event, status: dict[str, Any]) -> list[Any]:
    pool = await open_pool(settings.database_url, SCHEMA, MIGRATIONS)
    redis = open_redis(settings.redis_url)
    await ensure_topics(settings.kafka_bootstrap, [*ALL_TOPICS, *(dlq_topic(t) for t in ALL_TOPICS)])
    producer = make_producer(settings.kafka_bootstrap, "inventory-worker")
    await producer.start()
    relay = OutboxRelay(pool, producer, "inventory")
    consumer = EventConsumer(
        name="inventory-reservations",
        topics=[TOPIC_ORDERS],
        bootstrap=settings.kafka_bootstrap,
        pool=pool,
        handler=make_handler(redis),
        dlq_producer=producer,
    )
    status["kafka"] = lambda: "up" if consumer.connected else "down"
    status["relay"] = lambda: "up" if relay.running else "down"
    return [relay.run(stop), consumer.run(stop)]


if __name__ == "__main__":
    asyncio.run(run_worker(settings, settings.worker_port, start))
