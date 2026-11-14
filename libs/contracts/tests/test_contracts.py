"""Contract tests: every schema is valid, every example matches its schema, and
the producers' envelope builder emits messages that match too."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from swiftflow_common.events import EVENT_TOPICS, ContractError, build_envelope, validate_envelope

ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = sorted((ROOT / "schemas").glob("*.v1.json"))
EXAMPLES = sorted((ROOT / "examples").glob("*.json"))


def test_one_schema_and_example_per_event_type() -> None:
    names = {p.name.split(".v")[0] for p in SCHEMAS}
    assert names == set(EVENT_TOPICS)
    assert {p.stem for p in EXAMPLES} == set(EVENT_TOPICS)


@pytest.mark.parametrize("path", SCHEMAS, ids=lambda p: p.name)
def test_schema_is_valid_draft_2020_12(path: Path) -> None:
    Draft202012Validator.check_schema(json.loads(path.read_text(encoding="utf-8")))


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_example_matches_schema(path: Path) -> None:
    example = json.loads(path.read_text(encoding="utf-8"))
    env = validate_envelope(example)
    assert env.event_type == path.stem


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_example_missing_payload_field_is_rejected(path: Path) -> None:
    example = json.loads(path.read_text(encoding="utf-8"))
    broken = copy.deepcopy(example)
    broken["payload"].pop(next(iter(broken["payload"])))
    with pytest.raises(ContractError):
        validate_envelope(broken)


def test_unknown_event_type_is_rejected() -> None:
    example = json.loads((ROOT / "examples" / "OrderPlaced.json").read_text(encoding="utf-8"))
    example["event_type"] = "OrderTeleported"
    with pytest.raises(ContractError):
        validate_envelope(example)


def test_bad_tenant_id_is_rejected() -> None:
    example = json.loads((ROOT / "examples" / "OrderShipped.json").read_text(encoding="utf-8"))
    example["tenant_id"] = "not-a-uuid"
    with pytest.raises(ContractError):
        validate_envelope(example)


def test_wrong_producer_is_rejected() -> None:
    example = json.loads((ROOT / "examples" / "StockReserved.json").read_text(encoding="utf-8"))
    example["producer"] = "order"
    with pytest.raises(ContractError):
        validate_envelope(example)


def test_builder_output_matches_contract() -> None:
    env = build_envelope(
        "OrderCancelled",
        tenant_id="6f1d2c3b-4a59-4e6f-8a7b-9c0d1e2f3a4b",
        aggregate_id="0b6e5f4a-3c2d-4b1a-9e8f-7d6c5b4a3f2e",
        producer="order",
        payload={
            "order_id": "0b6e5f4a-3c2d-4b1a-9e8f-7d6c5b4a3f2e",
            "reason": "customer_request",
            "previous_status": "CONFIRMED",
        },
    )
    assert validate_envelope(env).payload["reason"] == "customer_request"


def test_builder_refuses_invalid_payload() -> None:
    with pytest.raises(ContractError):
        build_envelope(
            "OrderCancelled",
            tenant_id="6f1d2c3b-4a59-4e6f-8a7b-9c0d1e2f3a4b",
            aggregate_id="0b6e5f4a-3c2d-4b1a-9e8f-7d6c5b4a3f2e",
            producer="order",
            payload={"order_id": "0b6e5f4a-3c2d-4b1a-9e8f-7d6c5b4a3f2e", "reason": "boredom"},
        )
