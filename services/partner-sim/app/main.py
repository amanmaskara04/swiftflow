"""Partner simulator (:8004): a controllable payment provider and a webhook receiver.

Two faces:
- provider endpoints (``/v1/payments``, ``/v1/payments/{ref}/refund``) are called by the
  order worker with ``X-Provider-Key``; ``/v1/hooks/{inbox}`` is a public webhook receiver
- control endpoints (config, recorded payments, received webhooks) need an admin JWT

State is in memory on purpose: restarting resets it.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import random
import re
import uuid
from collections import OrderedDict, deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Request
from pydantic import BaseModel, Field, StringConstraints
from swiftflow_common.app import build_app
from swiftflow_common.auth import Principal, require_scope
from swiftflow_common.config import BaseServiceSettings
from swiftflow_common.health import health_router
from swiftflow_common.problems import ProblemError, forbidden, validation
from swiftflow_common.runtime import install_core
from swiftflow_common.signing import verify


class Settings(BaseServiceSettings):
    service_name: str = "partner-sim"
    provider_api_key: str = "dev-provider-key-change-me"
    default_failure_rate: float = 0.0
    default_latency_ms: int = 50


settings = Settings()
settings.require_real_secrets(provider_api_key=settings.provider_api_key)

MAX_PAYMENTS = 50_000
MAX_INBOXES = 200
MAX_PER_INBOX = 100
MAX_HOOK_BYTES = 256 * 1024
INBOX_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{0,63}$")


class State:
    def __init__(self) -> None:
        self.config = {"failure_rate": settings.default_failure_rate, "latency_ms": settings.default_latency_ms, "outage": False}
        self.charges: OrderedDict[str, dict[str, Any]] = OrderedDict()  # by idempotency key
        self.by_payment_id: dict[str, str] = {}
        self.refunds: OrderedDict[str, dict[str, Any]] = OrderedDict()  # by idempotency key
        self.inboxes: dict[str, deque[dict[str, Any]]] = {}
        self.secrets: dict[str, str] = {}


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    install_core(app, settings, pool=None, redis=None, namespace="sim")
    app.state.sim = State()
    yield


app = build_app(settings, title="SwiftFlow Partner Simulator", lifespan=lifespan)
app.include_router(health_router(lambda: {}))


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def provider_auth(request: Request) -> None:
    supplied = request.headers.get("x-provider-key", "")
    if not hmac.compare_digest(supplied.encode(), settings.provider_api_key.encode()):
        raise ProblemError(401, "/problems/unauthenticated", "Unauthenticated", "Invalid provider key.")


def idem_key(request: Request) -> str:
    key = request.headers.get("idempotency-key", "")
    if not key or len(key) > 200:
        raise validation("The Idempotency-Key header is required.")
    return key


async def simulate_network(sim: State) -> None:
    if sim.config["latency_ms"] > 0:
        await asyncio.sleep(sim.config["latency_ms"] / 1000)
    if sim.config["outage"]:
        raise ProblemError(503, "/problems/unavailable", "Provider unavailable", "The payment provider is down.")


# ---------------------------------------------------------------- provider face
class ChargeIn(BaseModel):
    order_id: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    tenant_id: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    amount_cents: int = Field(ge=0, le=10**12)


class RefundIn(BaseModel):
    order_id: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    tenant_id: Annotated[str, StringConstraints(min_length=1, max_length=64)]


@app.post("/v1/payments", dependencies=[Depends(provider_auth)])
async def charge(body: ChargeIn, request: Request) -> dict:
    sim: State = request.app.state.sim
    key = idem_key(request)
    await simulate_network(sim)
    existing = sim.charges.get(key)
    if existing is not None:  # same key, same answer: a retry never charges twice
        return {"status": existing["status"], "payment_id": existing["payment_id"], "replayed": True}
    declined = random.random() < sim.config["failure_rate"]
    record = {
        "payment_id": "pay_" + uuid.uuid4().hex[:20],
        "order_id": body.order_id,
        "tenant_id": body.tenant_id,
        "amount_cents": body.amount_cents,
        "status": "declined" if declined else "approved",
        "refunded": False,
        "created_at": now_iso(),
    }
    sim.charges[key] = record
    sim.by_payment_id[record["payment_id"]] = key
    while len(sim.charges) > MAX_PAYMENTS:
        _, old = sim.charges.popitem(last=False)
        sim.by_payment_id.pop(old["payment_id"], None)
    return {"status": record["status"], "payment_id": record["payment_id"], "replayed": False}


@app.post("/v1/payments/{ref}/refund", dependencies=[Depends(provider_auth)])
async def refund(ref: str, body: RefundIn, request: Request) -> dict:
    """``ref`` is a payment ID, or the charge's idempotency key when the caller never
    learned the payment ID. If no charge exists yet for that key it is voided, so a
    late-arriving charge with the same key is declined instead of taking money."""
    sim: State = request.app.state.sim
    key = idem_key(request)
    await simulate_network(sim)
    done = sim.refunds.get(key)
    if done is not None:
        return {**done, "replayed": True}
    charge_key = sim.by_payment_id.get(ref, ref)
    record = sim.charges.get(charge_key)
    if record is None:
        sim.charges[charge_key] = {
            "payment_id": "void_" + uuid.uuid4().hex[:20],
            "order_id": body.order_id,
            "tenant_id": body.tenant_id,
            "amount_cents": 0,
            "status": "voided",
            "refunded": False,
            "created_at": now_iso(),
        }
        result = {"status": "voided", "payment_id": None}
    elif record["status"] == "approved":
        record["refunded"] = True
        result = {"status": "refunded", "payment_id": record["payment_id"]}
    else:
        result = {"status": "voided", "payment_id": record["payment_id"]}
    sim.refunds[key] = {**result, "order_id": body.order_id, "tenant_id": body.tenant_id, "created_at": now_iso()}
    while len(sim.refunds) > MAX_PAYMENTS:
        sim.refunds.popitem(last=False)
    return {**result, "replayed": False}


@app.post("/v1/hooks/{inbox}", status_code=202)
async def receive_hook(inbox: str, request: Request) -> dict:
    """A public webhook receiver, like a partner's endpoint. The signature is the proof."""
    sim: State = request.app.state.sim
    if not INBOX_RE.match(inbox):
        raise validation("Inbox names are lowercase letters, digits and dashes.")
    raw = await request.body()
    if len(raw) > MAX_HOOK_BYTES:
        raise ProblemError(413, detail="Webhook bodies are limited to 256 KB.")
    if inbox not in sim.inboxes and len(sim.inboxes) >= MAX_INBOXES:
        raise ProblemError(429, "/problems/rate-limited", "Too many inboxes", "The simulator's inbox limit was reached.")
    secret = sim.secrets.get(inbox)
    valid: bool | None = None
    reason = "no_secret_registered"
    if secret is not None:
        valid, reason = verify(secret, request.headers.get("swiftflow-signature", ""), raw)
    try:
        body: Any = json.loads(raw)
    except ValueError:
        body = raw.decode("utf-8", errors="replace")[:2000]
    keep = ("content-type", "user-agent", "swiftflow-event", "swiftflow-delivery", "swiftflow-signature")
    sim.inboxes.setdefault(inbox, deque(maxlen=MAX_PER_INBOX)).appendleft(
        {
            "id": uuid.uuid4().hex,
            "received_at": now_iso(),
            "headers": {k: request.headers[k] for k in keep if k in request.headers},
            "body": body,
            "signature_valid": valid,
            "signature_reason": reason,
        }
    )
    return {"received": True}


# ---------------------------------------------------------------- control face (admin JWT)
class ConfigIn(BaseModel):
    failure_rate: float | None = Field(default=None, ge=0, le=1)
    latency_ms: int | None = Field(default=None, ge=0, le=5000)
    outage: bool | None = None


class SecretIn(BaseModel):
    secret: Annotated[str, StringConstraints(min_length=8, max_length=200)]


def tenant_inbox(p: Principal, name: str) -> str:
    """Control-side inbox names are namespaced by tenant, so one tenant's admin cannot
    read another tenant's received webhooks."""
    if not p.tenant_slug:
        raise forbidden("This token has no tenant slug.")
    full = name if name.startswith(p.tenant_slug + "-") else f"{p.tenant_slug}-{name}"
    if not INBOX_RE.match(full):
        raise validation("Inbox names are lowercase letters, digits and dashes.")
    return full


@app.get("/v1/config")
async def get_config(request: Request, _: Principal = Depends(require_scope("admin"))) -> dict:
    return request.app.state.sim.config


@app.put("/v1/config")
async def put_config(body: ConfigIn, request: Request, _: Principal = Depends(require_scope("admin"))) -> dict:
    cfg = request.app.state.sim.config
    cfg.update({k: v for k, v in body.model_dump().items() if v is not None})
    return cfg


@app.get("/v1/payments")
async def list_payments(request: Request, p: Principal = Depends(require_scope("admin"))) -> dict:
    sim: State = request.app.state.sim
    mine = [c for c in sim.charges.values() if c["tenant_id"] == p.tenant_id]
    approved: dict[str, int] = {}
    for c in mine:
        if c["status"] == "approved":
            approved[c["order_id"]] = approved.get(c["order_id"], 0) + 1
    refunds = [r for r in sim.refunds.values() if r["tenant_id"] == p.tenant_id]
    return {
        "total": len(mine),
        "approved": sum(1 for c in mine if c["status"] == "approved"),
        "declined": sum(1 for c in mine if c["status"] == "declined"),
        "refunded": sum(1 for c in mine if c["refunded"]),
        "double_charges": sum(1 for n in approved.values() if n > 1),
        "charges": list(reversed(mine))[:200],
        "refunds": list(reversed(refunds))[:200],
    }


@app.put("/v1/hooks/{name}/secret")
async def put_secret(name: str, body: SecretIn, request: Request, p: Principal = Depends(require_scope("admin"))) -> dict:
    inbox = tenant_inbox(p, name)
    request.app.state.sim.secrets[inbox] = body.secret
    return {"inbox": inbox, "registered": True}


@app.get("/v1/hooks/{name}")
async def list_hooks(name: str, request: Request, p: Principal = Depends(require_scope("admin"))) -> dict:
    inbox = tenant_inbox(p, name)
    return {"inbox": inbox, "items": list(request.app.state.sim.inboxes.get(inbox, ()))}
