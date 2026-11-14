"""Idempotency keys: request hashing and a Redis-backed begin/complete store.

Same key + same body  -> the stored response is replayed (``Idempotent-Replayed: true``)
Same key + other body -> 422 ``idempotency-mismatch``
Key still in flight   -> 409 ``idempotency-conflict``
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

import structlog
from fastapi import Request
from fastapi.responses import JSONResponse

from .problems import ProblemError

log = structlog.get_logger("idempotency")

TTL_SECONDS = 24 * 3600
IN_FLIGHT_TTL_SECONDS = 60
MAX_KEY_LENGTH = 200


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def request_hash(method: str, path: str, body: Any) -> str:
    """Hash of the *parsed* body, so whitespace and key order do not matter."""
    material = f"{method.upper()}\n{path}\n{canonical_json(body)}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def validate_key(key: str | None, required: bool = True) -> str | None:
    if key is None or key == "":
        if required:
            raise ProblemError(
                422,
                "/problems/validation",
                "Validation failed",
                "The Idempotency-Key header is required.",
                errors=[{"loc": ["header", "Idempotency-Key"], "msg": "required", "type": "missing"}],
            )
        return None
    if len(key) > MAX_KEY_LENGTH or not key.isprintable():
        raise ProblemError(
            422,
            "/problems/validation",
            "Validation failed",
            f"Idempotency-Key must be 1-{MAX_KEY_LENGTH} printable characters.",
            errors=[{"loc": ["header", "Idempotency-Key"], "msg": "invalid length", "type": "value_error"}],
        )
    return key


def mismatch() -> ProblemError:
    return ProblemError(
        422,
        "/problems/idempotency-mismatch",
        "Idempotency key reused",
        "This Idempotency-Key was already used with a different request body.",
    )


def in_flight() -> ProblemError:
    return ProblemError(
        409,
        "/problems/idempotency-conflict",
        "Request in progress",
        "A request with this Idempotency-Key is still being processed.",
    )


@dataclass
class StoredResponse:
    status: int
    body: Any

    def to_response(self) -> JSONResponse:
        return JSONResponse(self.body, status_code=self.status, headers={"Idempotent-Replayed": "true"})


class IdempotencyStore:
    """``begin`` returns ``None`` when the caller owns the key and must do the work,
    or a ``StoredResponse`` to replay. Redis errors degrade to ``None`` (the
    caller's database constraint is the fallback)."""

    def __init__(self, redis: Any, namespace: str) -> None:
        self.redis = redis
        self.namespace = namespace

    def _key(self, tenant_id: str, key: str) -> str:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return f"idem:{self.namespace}:{tenant_id}:{digest}"

    async def begin(self, tenant_id: str, key: str, req_hash: str) -> StoredResponse | None:
        rkey = self._key(tenant_id, key)
        try:
            claimed = await self.redis.set(
                rkey, json.dumps({"state": "in_flight", "hash": req_hash}), nx=True, ex=IN_FLIGHT_TTL_SECONDS
            )
            if claimed:
                return None
            raw = await self.redis.get(rkey)
        except Exception as exc:
            log.warning("idempotency_redis_unavailable", error=str(exc))
            return None
        if raw is None:  # expired between the two calls; let the DB decide
            return None
        record = json.loads(raw)
        if record.get("hash") != req_hash:
            raise mismatch()
        if record.get("state") != "done":
            raise in_flight()
        return StoredResponse(record["status"], record["body"])

    async def complete(self, tenant_id: str, key: str, req_hash: str, status: int, body: Any) -> None:
        try:
            await self.redis.set(
                self._key(tenant_id, key),
                json.dumps({"state": "done", "hash": req_hash, "status": status, "body": body}, default=str),
                ex=TTL_SECONDS,
            )
        except Exception as exc:
            log.warning("idempotency_complete_failed", error=str(exc))

    async def abandon(self, tenant_id: str, key: str) -> None:
        try:
            await self.redis.delete(self._key(tenant_id, key))
        except Exception as exc:
            log.warning("idempotency_abandon_failed", error=str(exc))


async def run_idempotent(
    request: Request,
    store: IdempotencyStore,
    tenant_id: str,
    body: Any,
    handler: Any,
    *,
    required: bool,
) -> JSONResponse:
    """Generic wrapper for POST handlers without their own DB-level idempotency.

    ``handler`` is an async callable returning ``(status, json_body)``.
    """
    key = validate_key(request.headers.get("idempotency-key"), required=required)
    if key is None:
        status, payload = await handler()
        return JSONResponse(payload, status_code=status)
    h = request_hash(request.method, request.url.path, body)
    stored = await store.begin(tenant_id, key, h)
    if stored is not None:
        return stored.to_response()
    try:
        status, payload = await handler()
    except BaseException:
        # errors are not stored: the client may fix the cause and retry with the same key
        await store.abandon(tenant_id, key)
        raise
    await store.complete(tenant_id, key, h, status, payload)
    return JSONResponse(payload, status_code=status)
