"""Kafka producer/consumer factories and topic bootstrap."""

from __future__ import annotations

import asyncio

import structlog
from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from aiokafka.admin import AIOKafkaAdminClient, NewTopic
from aiokafka.errors import TopicAlreadyExistsError

log = structlog.get_logger("kafka")

PARTITIONS = 6
REPLICATION = 1


def make_producer(bootstrap: str, client_id: str) -> AIOKafkaProducer:
    return AIOKafkaProducer(
        bootstrap_servers=bootstrap,
        client_id=client_id,
        acks="all",
        enable_idempotence=True,
        linger_ms=5,
        request_timeout_ms=15000,
    )


def make_consumer(bootstrap: str, group_id: str, topics: list[str], client_id: str) -> AIOKafkaConsumer:
    return AIOKafkaConsumer(
        *topics,
        bootstrap_servers=bootstrap,
        group_id=group_id,
        client_id=client_id,
        enable_auto_commit=False,
        auto_offset_reset="earliest",
        max_poll_records=200,
        session_timeout_ms=15000,
        heartbeat_interval_ms=3000,
    )


async def ensure_topics(bootstrap: str, topics: list[str], partitions: int = PARTITIONS) -> None:
    """Create any missing topics. Retries until the broker answers."""
    delay = 1.0
    while True:
        admin = AIOKafkaAdminClient(bootstrap_servers=bootstrap, client_id="swiftflow-bootstrap")
        try:
            await admin.start()
            existing = set(await admin.list_topics())
            missing = [t for t in topics if t not in existing]
            if missing:
                try:
                    await admin.create_topics(
                        [NewTopic(name=t, num_partitions=partitions, replication_factor=REPLICATION) for t in missing]
                    )
                    log.info("topics_created", topics=missing)
                except TopicAlreadyExistsError:
                    pass
            return
        except Exception as exc:
            log.warning("kafka_not_ready", error=str(exc), retry_in=delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 10.0)
        finally:
            try:
                await admin.close()
            except Exception:  # noqa: S110
                pass


def encode_headers(headers: dict[str, str]) -> list[tuple[str, bytes]]:
    return [(k, str(v).encode("utf-8")) for k, v in headers.items() if v is not None]


def decode_headers(headers: list[tuple[str, bytes]] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in headers or []:
        try:
            out[k] = v.decode("utf-8") if isinstance(v, bytes | bytearray) else str(v)
        except UnicodeDecodeError:
            continue
    return out
