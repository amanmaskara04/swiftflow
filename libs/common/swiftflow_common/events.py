"""Event envelope and contract validation against ``libs/contracts/schemas``."""

from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from pydantic import BaseModel, Field

TOPIC_ORDERS = "orders.events"
TOPIC_INVENTORY = "inventory.events"
TOPIC_STOCK_CHANGES = "inventory.stock-changes"
ALL_TOPICS = (TOPIC_ORDERS, TOPIC_INVENTORY, TOPIC_STOCK_CHANGES)

EVENT_TOPICS: dict[str, str] = {
    "OrderPlaced": TOPIC_ORDERS,
    "OrderConfirmed": TOPIC_ORDERS,
    "OrderCancelled": TOPIC_ORDERS,
    "OrderShipped": TOPIC_ORDERS,
    "StockReserved": TOPIC_INVENTORY,
    "StockReservationFailed": TOPIC_INVENTORY,
    "StockReleased": TOPIC_INVENTORY,
    "StockCommitted": TOPIC_INVENTORY,
    "StockLevelChanged": TOPIC_STOCK_CHANGES,
}
EVENT_TYPES = tuple(EVENT_TOPICS)


def dlq_topic(topic: str) -> str:
    return f"{topic}.dlq"


class ContractError(ValueError):
    pass


class EventEnvelope(BaseModel):
    event_id: str
    event_type: str
    schema_version: int = 1
    occurred_at: str
    tenant_id: str
    aggregate_id: str
    producer: str
    payload: dict[str, Any] = Field(default_factory=dict)


def contracts_dir() -> Path:
    env = os.environ.get("SWIFTFLOW_CONTRACTS_DIR")
    if env:
        return Path(env)
    # libs/common/swiftflow_common/events.py -> libs/contracts/schemas
    return Path(__file__).resolve().parents[2] / "contracts" / "schemas"


@lru_cache(maxsize=1)
def _validators() -> dict[tuple[str, int], Draft202012Validator]:
    out: dict[tuple[str, int], Draft202012Validator] = {}
    directory = contracts_dir()
    for path in sorted(directory.glob("*.v*.json")):
        name, _, rest = path.name.partition(".v")
        version = int(rest.split(".")[0])
        schema = json.loads(path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        out[(name, version)] = Draft202012Validator(schema)
    if not out:
        raise RuntimeError(f"no event contracts found in {directory}")
    return out


def validate_envelope(envelope: dict[str, Any]) -> EventEnvelope:
    """Validate a whole envelope against its event type's schema."""
    if not isinstance(envelope, dict):
        raise ContractError("envelope must be an object")
    etype = envelope.get("event_type")
    version = envelope.get("schema_version", 1)
    validator = _validators().get((str(etype), int(version) if isinstance(version, int) else -1))
    if validator is None:
        raise ContractError(f"no contract for {etype} v{version}")
    errors = sorted(validator.iter_errors(envelope), key=lambda e: list(e.path))
    if errors:
        first = errors[0]
        where = "/".join(str(p) for p in first.path) or "<root>"
        raise ContractError(f"{etype}: {where}: {first.message}")
    return EventEnvelope.model_validate(envelope)


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def build_envelope(
    event_type: str,
    *,
    tenant_id: str,
    aggregate_id: str,
    producer: str,
    payload: dict[str, Any],
    event_id: str | None = None,
    occurred_at: str | None = None,
) -> dict[str, Any]:
    envelope = {
        "event_id": event_id or str(uuid.uuid4()),
        "event_type": event_type,
        "schema_version": 1,
        "occurred_at": occurred_at or utc_now_iso(),
        "tenant_id": str(tenant_id),
        "aggregate_id": str(aggregate_id),
        "producer": producer,
        "payload": payload,
    }
    validate_envelope(envelope)
    return envelope
