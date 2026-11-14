"""``EventConsumer``: idempotent inbox, retries with jitter, DLQ, manual commits."""

from __future__ import annotations

import asyncio
import json
import random
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import asyncpg
import structlog
from aiokafka import TopicPartition
from prometheus_client import Counter, Gauge, Histogram

from .db import TxContext, tenant_tx
from .events import ContractError, EventEnvelope, dlq_topic, validate_envelope
from .kafka import decode_headers, encode_headers, make_consumer
from .observability import extract_context

log = structlog.get_logger("consumer")

EVENTS_CONSUMED = Counter("swiftflow_events_consumed_total", "Events consumed by result", ["consumer", "result"])
HANDLE_SECONDS = Histogram("swiftflow_event_handle_seconds", "Event handler latency", ["consumer"])
CONSUMER_LAG = Gauge("swiftflow_consumer_lag", "Consumer lag (messages)", ["consumer", "topic", "partition"])

Handler = Callable[[asyncpg.Connection, EventEnvelope, TxContext, str], Awaitable[None]]

MAX_ATTEMPTS = 3
BASE_DELAY = 0.2


def backoff_delay(attempt: int, base: float = BASE_DELAY) -> float:
    """Full jitter: uniform(0, base * 2^attempt)."""
    return random.uniform(0, base * (2**attempt))


class PoisonMessage(Exception):
    pass


class EventConsumer:
    def __init__(
        self,
        *,
        name: str,
        topics: list[str],
        bootstrap: str,
        pool: asyncpg.Pool,
        handler: Handler,
        dlq_producer: Any,
        max_attempts: int = MAX_ATTEMPTS,
    ) -> None:
        self.name = name
        self.topics = topics
        self.bootstrap = bootstrap
        self.pool = pool
        self.handler = handler
        self.dlq_producer = dlq_producer
        self.max_attempts = max_attempts
        self.consumer: Any = None
        self.connected = False

    # ------------------------------------------------------------------ processing

    def parse(self, value: bytes | None) -> EventEnvelope:
        try:
            raw = json.loads(value or b"")
        except (ValueError, UnicodeDecodeError) as exc:
            raise PoisonMessage(f"invalid JSON: {exc}") from None
        try:
            return validate_envelope(raw)
        except (ContractError, ValueError) as exc:
            raise PoisonMessage(str(exc)) from None

    async def handle_once(self, env: EventEnvelope, topic: str) -> bool:
        """One attempt in one transaction. Returns False for a duplicate."""
        ctx = TxContext()
        async with tenant_tx(self.pool, env.tenant_id) as conn:
            inserted = await conn.fetchval(
                """
                INSERT INTO processed_events (consumer, event_id) VALUES ($1, $2)
                ON CONFLICT DO NOTHING RETURNING 1
                """,
                self.name,
                env.event_id,
            )
            if not inserted:
                return False
            await self.handler(conn, env, ctx, topic)
        await ctx.run_after_commit()
        return True

    async def process_message(self, msg: Any) -> str:
        """Process one Kafka record to completion. Returns the result label."""
        headers = decode_headers(msg.headers)
        try:
            env = self.parse(msg.value)
        except PoisonMessage as exc:
            await self.send_to_dlq(msg, str(exc), 0)
            EVENTS_CONSUMED.labels(self.name, "dlq").inc()
            return "dlq"

        token = None
        trace_ctx = extract_context(headers)
        if trace_ctx is not None:
            try:
                from opentelemetry import context as otel_context

                token = otel_context.attach(trace_ctx)
            except Exception:
                token = None
        try:
            last_error = ""
            for attempt in range(self.max_attempts):
                try:
                    with HANDLE_SECONDS.labels(self.name).time():
                        fresh = await self.handle_once(env, msg.topic)
                    result = "ok" if fresh else "duplicate"
                    EVENTS_CONSUMED.labels(self.name, result).inc()
                    return result
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    last_error = f"{type(exc).__name__}: {exc}"[:500]
                    log.warning(
                        "event_handler_failed",
                        consumer=self.name,
                        event_type=env.event_type,
                        event_id=env.event_id,
                        attempt=attempt + 1,
                        error=last_error,
                    )
                    if attempt + 1 < self.max_attempts:
                        EVENTS_CONSUMED.labels(self.name, "retry").inc()
                        await asyncio.sleep(backoff_delay(attempt))
            await self.send_to_dlq(msg, last_error, self.max_attempts)
            EVENTS_CONSUMED.labels(self.name, "dlq").inc()
            return "dlq"
        finally:
            if token is not None:
                from opentelemetry import context as otel_context

                otel_context.detach(token)

    async def send_to_dlq(self, msg: Any, error: str, attempts: int) -> None:
        headers = decode_headers(msg.headers)
        headers.update(
            {
                "error": error[:1000],
                "attempts": str(attempts),
                "consumer": self.name,
                "failed_at": datetime.now(UTC).isoformat(),
                "original_topic": msg.topic,
                "original_partition": str(msg.partition),
                "original_offset": str(msg.offset),
            }
        )
        await self.dlq_producer.send_and_wait(dlq_topic(msg.topic), value=msg.value, key=msg.key, headers=encode_headers(headers))
        log.error("event_dead_lettered", consumer=self.name, topic=msg.topic, offset=msg.offset, error=error)

    # ------------------------------------------------------------------ loop

    async def _process_partition(self, tp: TopicPartition, records: list[Any]) -> None:
        for msg in records:  # in order: one partition holds one order's events
            await self.process_message(msg)
        await self.consumer.commit({tp: records[-1].offset + 1})

    async def run(self, stop: asyncio.Event) -> None:
        delay = 1.0
        while not stop.is_set():
            self.consumer = make_consumer(self.bootstrap, self.name, self.topics, client_id=self.name)
            try:
                await self.consumer.start()
                self.connected = True
                delay = 1.0
                last_lag = 0.0
                log.info("consumer_started", consumer=self.name, topics=self.topics)
                while not stop.is_set():
                    batches = await self.consumer.getmany(timeout_ms=500, max_records=200)
                    if batches:
                        await asyncio.gather(*(self._process_partition(tp, recs) for tp, recs in batches.items()))
                    now = asyncio.get_running_loop().time()
                    if now - last_lag > 5:
                        last_lag = now
                        await self._update_lag()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.connected = False
                log.warning("consumer_error", consumer=self.name, error=str(exc), retry_in=delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 10.0)
            finally:
                self.connected = False
                try:
                    await self.consumer.stop()
                except Exception:  # noqa: S110
                    pass

    async def _update_lag(self) -> None:
        try:
            for tp in self.consumer.assignment():
                hw = self.consumer.highwater(tp)
                if hw is None:
                    continue
                pos = await self.consumer.position(tp)
                CONSUMER_LAG.labels(self.name, tp.topic, str(tp.partition)).set(max(0, hw - pos))
        except Exception:  # noqa: S110
            pass
