"""Logging, HTTP middleware (request IDs, metrics, body limit, security headers),
OpenTelemetry setup and Kafka header propagation."""

from __future__ import annotations

import json
import logging
import re
import sys
import time
import uuid
from typing import Any

import structlog
from prometheus_client import Counter, Histogram

from .problems import PROBLEM_JSON, problem_body

HTTP_REQUESTS = Counter("swiftflow_http_requests_total", "HTTP requests", ["service", "method", "route", "status"])
HTTP_LATENCY = Histogram(
    "swiftflow_http_request_duration_seconds",
    "HTTP request latency",
    ["service", "method", "route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:\-]{1,128}$")


def configure_logging(service: str, level: str = "INFO", fmt: str = "console") -> None:
    lvl = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=lvl, force=True)
    for noisy in ("aiokafka", "kafka", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(max(lvl, logging.WARNING))
    renderer: Any = structlog.processors.JSONRenderer() if fmt == "json" else structlog.dev.ConsoleRenderer(colors=False)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _add_trace_ids,
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(lvl),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
    structlog.contextvars.bind_contextvars(service=service)


def _add_trace_ids(_: Any, __: str, event: dict[str, Any]) -> dict[str, Any]:
    try:
        from opentelemetry import trace

        ctx = trace.get_current_span().get_span_context()
        if ctx.is_valid:
            event["trace_id"] = format(ctx.trace_id, "032x")
    except Exception:  # noqa: S110 - logging must never fail
        pass
    return event


def new_request_id() -> str:
    return "req_" + uuid.uuid4().hex


class BodyTooLarge(Exception):
    pass


class HttpMiddleware:
    """Pure ASGI middleware (does not buffer, so SSE and proxies stream).

    - accepts or generates ``X-Request-ID`` and returns it
    - enforces the request body size limit (413)
    - records Prometheus metrics per route template
    - adds ``RateLimit-Remaining`` when the rate limiter set it, plus security headers
    """

    def __init__(self, app: Any, service: str, max_body_bytes: int = 1_048_576) -> None:
        self.app = app
        self.service = service
        self.max_body = max_body_bytes

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        rid = headers.get("x-request-id", "")
        if not _REQUEST_ID_RE.match(rid):
            rid = new_request_id()
        state = scope.setdefault("state", {})
        state["request_id"] = rid
        structlog.contextvars.bind_contextvars(request_id=rid)

        start = time.perf_counter()
        status_holder = {"status": 500, "started": False}

        async def send_problem(status: int, detail: str) -> None:
            body = json.dumps(
                problem_body(
                    status,
                    "/problems/payload-too-large" if status == 413 else "/problems/internal",
                    "Request body too large" if status == 413 else "Internal server error",
                    detail,
                    scope.get("path"),
                    rid,
                )
            ).encode()
            await send_wrapper(
                {
                    "type": "http.response.start",
                    "status": status,
                    "headers": [(b"content-type", PROBLEM_JSON.encode()), (b"content-length", str(len(body)).encode())],
                }
            )
            await send_wrapper({"type": "http.response.body", "body": body})

        received = 0

        async def receive_wrapper() -> dict:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_body:
                    raise BodyTooLarge()
            return message

        async def send_wrapper(message: dict) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                status_holder["started"] = True
                hdrs = list(message.get("headers", []))
                names = {k.lower() for k, _ in hdrs}
                hdrs.append((b"x-request-id", rid.encode()))
                remaining = scope.get("state", {}).get("ratelimit_remaining")
                if remaining is not None and b"ratelimit-remaining" not in names:
                    hdrs.append((b"ratelimit-remaining", str(int(remaining)).encode()))
                if b"x-content-type-options" not in names:
                    hdrs.append((b"x-content-type-options", b"nosniff"))
                if b"referrer-policy" not in names:
                    hdrs.append((b"referrer-policy", b"no-referrer"))
                message = {**message, "headers": hdrs}
            await send(message)

        declared = headers.get("content-length")
        if declared is not None:
            try:
                if int(declared) > self.max_body:
                    status_holder["status"] = 413
                    await send_problem(413, f"Request bodies are limited to {self.max_body} bytes.")
                    return
            except ValueError:
                pass
        try:
            await self.app(scope, receive_wrapper, send_wrapper)
        except BodyTooLarge:
            if not status_holder["started"]:
                await send_problem(413, f"Request bodies are limited to {self.max_body} bytes.")
            status_holder["status"] = 413
        finally:
            route = scope.get("route")
            route_path = getattr(route, "path", None) or "unmatched"
            method = scope.get("method", "GET")
            HTTP_REQUESTS.labels(self.service, method, route_path, str(status_holder["status"])).inc()
            HTTP_LATENCY.labels(self.service, method, route_path).observe(time.perf_counter() - start)
            structlog.contextvars.unbind_contextvars("request_id")


# --------------------------------------------------------------------------- tracing

_tracing_enabled = False


def setup_tracing(service: str, endpoint: str | None, app: Any = None) -> bool:
    """Enable OpenTelemetry when an OTLP endpoint is configured. No-op otherwise."""
    global _tracing_enabled
    if not endpoint:
        return False
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        if not _tracing_enabled:
            provider = TracerProvider(resource=Resource.create({"service.name": service}))
            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint.rstrip("/") + "/v1/traces")))
            trace.set_tracer_provider(provider)
            from opentelemetry.instrumentation.asyncpg import AsyncPGInstrumentor
            from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

            HTTPXClientInstrumentor().instrument()
            AsyncPGInstrumentor().instrument()
            _tracing_enabled = True
        if app is not None:
            from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

            FastAPIInstrumentor.instrument_app(app, excluded_urls="healthz,readyz,metrics")
        return True
    except Exception as exc:  # tracing must never stop a service from starting
        structlog.get_logger("observability").warning("tracing_disabled", error=str(exc))
        return False


def current_trace_headers() -> dict[str, str]:
    """W3C trace context of the current span (for outbox rows / Kafka headers)."""
    carrier: dict[str, str] = {}
    try:
        from opentelemetry.propagate import inject

        inject(carrier)
    except Exception:  # noqa: S110
        pass
    return {k: v for k, v in carrier.items() if k in ("traceparent", "tracestate")}


def extract_context(headers: dict[str, str]) -> Any:
    try:
        from opentelemetry.propagate import extract

        return extract(headers)
    except Exception:
        return None


def get_tracer(name: str) -> Any:
    from opentelemetry import trace

    return trace.get_tracer(name)
