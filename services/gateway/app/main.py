"""API gateway (:8080): the one address clients use.

- forwards only an allow-list of path prefixes to fixed upstreams (never to a host
  taken from the request)
- streams request and response bodies, so the SSE feed passes through
- adds/propagates ``X-Request-ID`` and ``traceparent``; CORS for the console origins
- ``/v1/system/health`` aggregates every node for the console's system map
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog
from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse
from swiftflow_common.app import build_app
from swiftflow_common.auth import Principal, require_scope
from swiftflow_common.config import BaseServiceSettings
from swiftflow_common.health import health_router
from swiftflow_common.problems import problem_response
from swiftflow_common.runtime import install_core


class Settings(BaseServiceSettings):
    service_name: str = "gateway"
    order_url: str = "http://localhost:8001"
    inventory_url: str = "http://localhost:8002"
    notification_url: str = "http://localhost:8003"
    partner_sim_url: str = "http://localhost:8004"
    order_worker_url: str = "http://localhost:9101"
    inventory_worker_url: str = "http://localhost:9102"
    notification_worker_url: str = "http://localhost:9103"
    cors_origins: str = "http://localhost:3000,http://localhost:5173"
    upstream_timeout_seconds: float = 10.0
    demo_clients: str = ""
    webhook_test_receiver_url: str = "http://localhost:8004/v1/hooks"


settings = Settings()
log = structlog.get_logger("gateway")

# (methods, path regex, upstream attr, strip "/sim"). Anything not listed is a 404.
ANY = frozenset({"GET", "POST", "PUT", "DELETE"})
ROUTES: list[tuple[frozenset[str], re.Pattern[str], str, bool]] = [
    (frozenset({"POST"}), re.compile(r"^/v1/auth/token$"), "order_url", False),
    (frozenset({"GET"}), re.compile(r"^/\.well-known/jwks\.json$"), "order_url", False),
    (ANY, re.compile(r"^/v1/orders(/[A-Za-z0-9\-]+){0,2}$"), "order_url", False),
    (ANY, re.compile(r"^/v1/(warehouses|products)$"), "inventory_url", False),
    (ANY, re.compile(r"^/v1/inventory/(adjustments|stock|[A-Za-z0-9._\-]+/availability)$"), "inventory_url", False),
    (ANY, re.compile(r"^/v1/webhooks/(subscriptions|deliveries)(/[A-Za-z0-9\-]+){0,2}$"), "notification_url", False),
    (frozenset({"GET"}), re.compile(r"^/v1/events(/stream)?$"), "notification_url", False),
]
# Simulator control endpoints: development environments only, GET/PUT only.
SIM_ROUTES: list[tuple[frozenset[str], re.Pattern[str], str, bool]] = [
    (frozenset({"GET", "PUT"}), re.compile(r"^/v1/sim/config$"), "partner_sim_url", True),
    (frozenset({"GET"}), re.compile(r"^/v1/sim/payments$"), "partner_sim_url", True),
    (frozenset({"GET"}), re.compile(r"^/v1/sim/hooks/[a-z0-9\-]{1,64}$"), "partner_sim_url", True),
    (frozenset({"PUT"}), re.compile(r"^/v1/sim/hooks/[a-z0-9\-]{1,64}/secret$"), "partner_sim_url", True),
]

HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}
# Request headers worth forwarding; everything else (cookies, X-Forwarded-*, ...) is dropped.
FORWARD_REQUEST = {
    "authorization",
    "content-type",
    "accept",
    "idempotency-key",
    "if-match",
    "traceparent",
    "tracestate",
    "user-agent",
    "last-event-id",
}
DROP_RESPONSE = HOP_BY_HOP | {"server", "date", "x-request-id", "x-content-type-options", "referrer-policy"}


def resolve(method: str, path: str) -> tuple[str, str] | None:
    """Map a request to ``(upstream base URL, upstream path)``, or None."""
    if ".." in path or "//" in path or "\\" in path or "%" in path:
        return None
    routes = ROUTES + (SIM_ROUTES if settings.is_dev else [])
    for methods, pattern, attr, strip_sim in routes:
        if pattern.match(path):
            if method not in methods:
                return None
            return getattr(settings, attr), ("/v1" + path[len("/v1/sim") :] if strip_sim else path)
    return None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    install_core(app, settings, pool=None, redis=None, namespace="gateway")
    app.state.http = httpx.AsyncClient(
        timeout=httpx.Timeout(settings.upstream_timeout_seconds, connect=3.0),
        limits=httpx.Limits(max_connections=1000, max_keepalive_connections=200),
        follow_redirects=False,
        trust_env=False,
    )
    yield
    await app.state.http.aclose()


app = build_app(settings, title="SwiftFlow Gateway", lifespan=lifespan)
app.include_router(health_router(lambda: {"order": lambda: _ping(settings.order_url + "/healthz")}))
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "If-Match", "X-Request-ID"],
    expose_headers=["X-Request-ID", "Idempotent-Replayed", "Retry-After", "RateLimit-Remaining"],
    max_age=600,
)


async def _ping(url: str) -> None:
    resp = await app.state.http.get(url, timeout=1.0)
    resp.raise_for_status()


# ---------------------------------------------------------------- system endpoints
def demo_clients() -> list[dict[str, str]]:
    if settings.environment != "local":
        return []
    out = []
    for entry in settings.demo_clients.split(";"):
        parts = [p.strip() for p in entry.split(":", 3)]
        if len(parts) == 4 and all(parts):
            out.append({"client_id": parts[0], "client_secret": parts[1], "role": parts[2], "tenant": parts[3]})
    return out


@app.get("/v1/system/info")
async def info() -> dict[str, Any]:
    return {
        "environment": settings.environment,
        "version": settings.version,
        "demo_clients": demo_clients(),
        "webhook_test_receiver_url": settings.webhook_test_receiver_url if settings.is_dev else None,
    }


NODES: list[tuple[str, str, int, str, str]] = [
    ("order-api", "api", 8001, "order_url", "/readyz"),
    ("order-worker", "worker", 9101, "order_worker_url", "/healthz"),
    ("inventory-api", "api", 8002, "inventory_url", "/readyz"),
    ("inventory-worker", "worker", 9102, "inventory_worker_url", "/healthz"),
    ("notification-api", "api", 8003, "notification_url", "/readyz"),
    ("notification-worker", "worker", 9103, "notification_worker_url", "/healthz"),
    ("partner-sim", "sim", 8004, "partner_sim_url", "/healthz"),
]


async def probe(node: tuple[str, str, int, str, str]) -> dict[str, Any]:
    node_id, kind, port, attr, path = node
    start = time.perf_counter()
    out: dict[str, Any] = {"id": node_id, "kind": kind, "port": port}
    try:
        resp = await app.state.http.get(getattr(settings, attr) + path, timeout=1.0)
        out["latency_ms"] = round((time.perf_counter() - start) * 1000)
        data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        checks = data.get("checks") or {k: v for k, v in data.items() if k != "status" and isinstance(v, str)}
        out["checks"] = checks
        out["status"] = (
            "up"
            if resp.status_code == 200 and all(v in ("up", "closed", "half_open", "open") for v in checks.values())
            else "down"
        )
        if resp.status_code != 200:
            out["error"] = f"HTTP {resp.status_code}"
    except Exception as exc:
        out.update(status="down", latency_ms=None, error=type(exc).__name__, checks={})
    return out


@app.get("/v1/system/health")
async def system_health(_: Principal = Depends(require_scope(None))) -> dict[str, Any]:
    probed = await asyncio.gather(*(probe(n) for n in NODES))
    nodes: list[dict[str, Any]] = [
        {"id": "gateway", "kind": "gateway", "port": 8080, "status": "up", "latency_ms": 0, "checks": {}},
        *probed,
    ]

    def infra(name: str, port: int, key: str) -> dict[str, Any]:
        seen = [n["checks"][key] for n in probed if key in n.get("checks", {})]
        status = "unknown" if not seen else ("up" if all(s == "up" for s in seen) else "down")
        return {"id": name, "kind": "infra", "port": port, "status": status, "latency_ms": None, "checks": {}}

    nodes += [infra("postgres", 5432, "postgres"), infra("redis", 6379, "redis"), infra("kafka", 9092, "kafka")]
    down = [n for n in nodes if n["status"] == "down"]
    return {
        "status": "up" if not down else "degraded",
        "checked_at": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "nodes": nodes,
    }


# ---------------------------------------------------------------- reverse proxy
@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE"], include_in_schema=False)
async def proxy(path: str, request: Request) -> Response:
    raw_path = request.scope.get("raw_path", b"").decode("latin-1").split("?")[0] or request.url.path
    target = resolve(request.method, raw_path)
    if target is None:
        return problem_response(request, 404, detail="No such route.")
    base, upstream_path = target
    headers = {k: v for k, v in request.headers.items() if k.lower() in FORWARD_REQUEST}
    headers["x-request-id"] = request.state.request_id
    if request.client:
        headers["x-forwarded-for"] = request.client.host
    is_stream = upstream_path == "/v1/events/stream"
    has_body = request.method in ("POST", "PUT")
    try:
        upstream_req = app.state.http.build_request(
            request.method,
            base + upstream_path,
            params=request.query_params,
            headers=headers,
            content=request.stream() if has_body else None,
            timeout=httpx.Timeout(settings.upstream_timeout_seconds, connect=3.0, read=None)
            if is_stream
            else httpx.USE_CLIENT_DEFAULT,
        )
        upstream = await app.state.http.send(upstream_req, stream=True)
    except httpx.TimeoutException:
        return problem_response(request, 504, detail="The service took too long to answer.")
    except httpx.HTTPError as exc:
        log.warning("upstream_unreachable", upstream=base, error=type(exc).__name__)
        return problem_response(request, 502, detail="The service could not be reached.")

    out_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in DROP_RESPONSE}

    async def body() -> AsyncIterator[bytes]:
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        except httpx.HTTPError:
            pass
        finally:
            await upstream.aclose()

    return StreamingResponse(body(), status_code=upstream.status_code, headers=out_headers)
