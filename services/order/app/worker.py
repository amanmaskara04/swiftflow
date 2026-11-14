"""Order worker (:9101): outbox relay, the saga consumer and the payment processor."""

from __future__ import annotations

import asyncio
import random
from datetime import UTC, datetime
from typing import Any

import asyncpg
import httpx
import structlog
from prometheus_client import Counter, Histogram
from swiftflow_common.circuit_breaker import CircuitBreaker, CircuitOpenError
from swiftflow_common.consumer import EventConsumer
from swiftflow_common.db import TxContext, system_tx, tenant_tx
from swiftflow_common.events import ALL_TOPICS, TOPIC_INVENTORY, EventEnvelope, dlq_topic
from swiftflow_common.kafka import ensure_topics, make_producer
from swiftflow_common.outbox import OutboxRelay
from swiftflow_common.runtime import open_pool, run_worker

from . import domain, repo
from .config import MIGRATIONS, SCHEMA, Settings

settings = Settings(service_name="order-worker")
log = structlog.get_logger("order-worker")

ORDERS_FINAL = Counter("swiftflow_orders_total", "Orders reaching a state", ["status"])
SAGA_SECONDS = Histogram(
    "swiftflow_saga_duration_seconds",
    "Time from order placed to confirmed, rejected or cancelled",
    buckets=(0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60),
)
PAYMENTS = Counter("swiftflow_payment_calls_total", "Payment provider calls", ["kind", "result"])


# ---------------------------------------------------------------- saga consumer
async def handle_event(conn: asyncpg.Connection, env: EventEnvelope, ctx: TxContext, topic: str) -> None:
    if env.event_type not in ("StockReserved", "StockReservationFailed"):
        return
    order = await repo.get_order(conn, env.aggregate_id, for_update=True)
    if order is None or order["status"] != domain.PENDING:
        return  # cancelled meanwhile: inventory releases on the OrderCancelled that follows on the same partition
    if env.event_type == "StockReserved":
        await conn.execute(
            "UPDATE orders SET status = 'RESERVED', total_cents = $2, next_payment_at = now(), "
            "version = version + 1, updated_at = now() WHERE id = $1",
            order["id"],
            env.payload["total_cents"],
        )
        for item in env.payload["items"]:
            await conn.execute(
                "UPDATE order_items SET warehouse_code = $3, unit_price_cents = $4 WHERE order_id = $1 AND sku = $2",
                order["id"],
                item["sku"],
                item["warehouse_code"],
                item["unit_price_cents"],
            )
        await repo.add_history(conn, order, domain.RESERVED, from_status=order["status"], event_type="StockReserved")
    else:
        reason = f"{env.payload['reason']}:{env.payload['sku']}"
        await conn.execute(
            "UPDATE orders SET status = 'REJECTED', failure_reason = $2, version = version + 1, updated_at = now(), "
            "finalized_at = now() WHERE id = $1",
            order["id"],
            reason,
        )
        await repo.add_history(
            conn, order, domain.REJECTED, from_status=order["status"], reason=reason, event_type="StockReservationFailed"
        )
        ctx.after_commit.append(lambda: ORDERS_FINAL.labels(domain.REJECTED).inc())


# ---------------------------------------------------------------- payment processor
class PaymentUnavailable(Exception):
    pass


class PaymentProcessor:
    """Charges RESERVED orders and refunds REFUND_PENDING ones.

    1. Claim due rows with FOR UPDATE SKIP LOCKED and push ``next_payment_at`` forward
       (a lease), then commit.
    2. Call the provider with no transaction open. Idempotency keys: the order ID for a
       charge, ``refund:<order id>`` for a refund, so a retry can never charge twice.
    3. Apply the result in a new transaction, re-checking the order's state.
    A crash anywhere is recovered when the lease expires.
    """

    def __init__(self, pool: asyncpg.Pool, client: httpx.AsyncClient, breaker: CircuitBreaker) -> None:
        self.pool = pool
        self.client = client
        self.breaker = breaker
        self.running = False

    async def claim(self, limit: int = 20) -> list[asyncpg.Record]:
        async with system_tx(self.pool) as conn:
            rows = await conn.fetch(
                """
                SELECT id, tenant_id, status, total_cents, payment_id, payment_status, payment_attempts, created_at
                FROM orders
                WHERE next_payment_at <= now() AND (status = 'RESERVED' OR payment_status = 'REFUND_PENDING')
                ORDER BY next_payment_at LIMIT $1
                FOR UPDATE SKIP LOCKED
                """,
                limit,
            )
            if rows:
                await conn.execute(
                    "UPDATE orders SET next_payment_at = now() + make_interval(secs => $2), "
                    "payment_attempts = payment_attempts + 1 WHERE id = ANY($1::uuid[])",
                    [r["id"] for r in rows],
                    float(settings.payment_lease_seconds),
                )
            return rows

    async def _post(self, path: str, key: str, body: dict[str, Any]) -> dict[str, Any]:
        async def call() -> httpx.Response:
            return await self.client.post(
                path, json=body, headers={"Idempotency-Key": key, "X-Provider-Key": settings.provider_api_key}
            )

        try:
            resp = await self.breaker.call(call, is_failure=lambda r: r.status_code >= 500)
        except CircuitOpenError as exc:
            raise PaymentUnavailable(f"circuit open, retry in {exc.retry_in:.0f}s") from None
        except httpx.HTTPError as exc:
            raise PaymentUnavailable(type(exc).__name__) from None
        if resp.status_code >= 500:
            raise PaymentUnavailable(f"provider returned {resp.status_code}")
        if resp.status_code >= 400:
            raise RuntimeError(f"payment provider rejected the request: {resp.status_code}")
        return resp.json()

    async def _retry_later(self, order: asyncpg.Record) -> None:
        delay = domain.retry_delay_seconds(order["payment_attempts"] + 1) + random.uniform(0, 0.5)
        async with system_tx(self.pool) as conn:
            await conn.execute(
                "UPDATE orders SET next_payment_at = now() + make_interval(secs => $2) "
                "WHERE id = $1 AND (status = 'RESERVED' OR payment_status = 'REFUND_PENDING')",
                order["id"],
                delay,
            )

    async def charge(self, order: asyncpg.Record) -> None:
        oid, tid = str(order["id"]), str(order["tenant_id"])
        try:
            result = await self._post(
                "/v1/payments", oid, {"order_id": oid, "tenant_id": tid, "amount_cents": order["total_cents"] or 0}
            )
        except PaymentUnavailable as exc:
            PAYMENTS.labels("charge", "unavailable").inc()
            log.warning("payment_unavailable", order_id=oid, error=str(exc))
            await self._retry_later(order)
            return
        approved = result.get("status") == "approved"
        PAYMENTS.labels("charge", "approved" if approved else "declined").inc()
        final: str | None = None
        async with tenant_tx(self.pool, tid) as conn:
            cur = await repo.get_order(conn, order["id"], for_update=True)
            if cur["status"] == domain.RESERVED and approved:
                await conn.execute(
                    "UPDATE orders SET status = 'CONFIRMED', payment_id = $2, payment_status = 'CHARGED', "
                    "next_payment_at = NULL, payment_attempts = 0, version = version + 1, updated_at = now(), "
                    "finalized_at = COALESCE(finalized_at, now()) WHERE id = $1",
                    cur["id"],
                    result["payment_id"],
                )
                await repo.add_history(conn, cur, domain.CONFIRMED, from_status=cur["status"], event_type="PaymentApproved")
                await repo.emit(
                    conn, "OrderConfirmed", cur, {"total_cents": cur["total_cents"] or 0, "payment_id": result["payment_id"]}
                )
                final = domain.CONFIRMED
            elif cur["status"] == domain.RESERVED:
                await conn.execute(
                    "UPDATE orders SET status = 'CANCELLED', failure_reason = 'payment_declined', next_payment_at = NULL, "
                    "payment_attempts = 0, version = version + 1, updated_at = now(), "
                    "finalized_at = COALESCE(finalized_at, now()) WHERE id = $1",
                    cur["id"],
                )
                await repo.add_history(
                    conn,
                    cur,
                    domain.CANCELLED,
                    from_status=cur["status"],
                    reason="payment_declined",
                    event_type="PaymentDeclined",
                )
                await repo.emit(conn, "OrderCancelled", cur, {"reason": "payment_declined", "previous_status": cur["status"]})
                final = domain.CANCELLED
            elif approved and cur["payment_status"] not in ("REFUNDED",):
                # The order was cancelled while the charge was in flight: give the money back.
                await conn.execute(
                    "UPDATE orders SET payment_id = $2, payment_status = 'REFUND_PENDING', next_payment_at = now(), "
                    "payment_attempts = 0, updated_at = now() WHERE id = $1",
                    cur["id"],
                    result["payment_id"],
                )
        if final:
            ORDERS_FINAL.labels(final).inc()
            SAGA_SECONDS.observe(max(0.0, (datetime.now(UTC) - order["created_at"]).total_seconds()))

    async def refund(self, order: asyncpg.Record) -> None:
        oid, tid = str(order["id"]), str(order["tenant_id"])
        ref = order["payment_id"] or oid  # no payment id yet: void by the charge's idempotency key
        try:
            result = await self._post(f"/v1/payments/{ref}/refund", f"refund:{oid}", {"order_id": oid, "tenant_id": tid})
        except PaymentUnavailable as exc:
            PAYMENTS.labels("refund", "unavailable").inc()
            log.warning("refund_unavailable", order_id=oid, error=str(exc))
            await self._retry_later(order)
            return
        refunded = result.get("status") == "refunded"
        PAYMENTS.labels("refund", result.get("status", "unknown")).inc()
        async with tenant_tx(self.pool, tid) as conn:
            cur = await repo.get_order(conn, order["id"], for_update=True)
            if cur["payment_status"] != "REFUND_PENDING":
                return
            await conn.execute(
                "UPDATE orders SET payment_status = $2, next_payment_at = NULL, payment_attempts = 0, updated_at = now() "
                "WHERE id = $1",
                cur["id"],
                "REFUNDED" if refunded else "NONE",
            )
            if refunded:
                await repo.add_history(conn, cur, cur["status"], from_status=cur["status"], event_type="PaymentRefunded")

    async def process(self, order: asyncpg.Record) -> None:
        try:
            if order["status"] == domain.RESERVED:
                await self.charge(order)
            else:
                await self.refund(order)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # the lease expires and the order is retried
            log.error("payment_processing_failed", order_id=str(order["id"]), error=f"{type(exc).__name__}: {exc}")

    async def run(self, stop: asyncio.Event) -> None:
        self.running = True
        try:
            while not stop.is_set():
                try:
                    rows = await self.claim()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.warning("payment_claim_failed", error=str(exc))
                    rows = []
                if rows:
                    await asyncio.gather(*(self.process(r) for r in rows))
                else:
                    await asyncio.sleep(0.1)
        finally:
            self.running = False


async def start(stop: asyncio.Event, status: dict[str, Any]) -> list[Any]:
    pool = await open_pool(settings.database_url, SCHEMA, MIGRATIONS)
    await ensure_topics(settings.kafka_bootstrap, [*ALL_TOPICS, *(dlq_topic(t) for t in ALL_TOPICS)])
    producer = make_producer(settings.kafka_bootstrap, "order-worker")
    await producer.start()
    relay = OutboxRelay(pool, producer, "order")
    consumer = EventConsumer(
        name="order-saga",
        topics=[TOPIC_INVENTORY],
        bootstrap=settings.kafka_bootstrap,
        pool=pool,
        handler=handle_event,
        dlq_producer=producer,
    )
    breaker = CircuitBreaker("payment", failure_threshold=5, reset_timeout=15.0)
    client = httpx.AsyncClient(base_url=settings.payment_url, timeout=settings.payment_timeout_seconds, trust_env=False)
    payments = PaymentProcessor(pool, client, breaker)
    status["kafka"] = lambda: "up" if consumer.connected else "down"
    status["relay"] = lambda: "up" if relay.running else "down"
    status["payment_circuit"] = lambda: breaker.state
    return [relay.run(stop), consumer.run(stop), payments.run(stop)]


if __name__ == "__main__":
    asyncio.run(run_worker(settings, settings.worker_port, start))
