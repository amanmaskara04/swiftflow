"""FastAPI app factory shared by every service, and cursor pagination helpers."""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

from fastapi import FastAPI

from .config import BaseServiceSettings
from .observability import HttpMiddleware, configure_logging, setup_tracing
from .problems import install_problem_handlers, validation


def build_app(
    settings: BaseServiceSettings,
    *,
    title: str,
    lifespan: Callable[[FastAPI], AbstractAsyncContextManager[Any]] | None = None,
) -> FastAPI:
    configure_logging(settings.service_name, settings.log_level, settings.log_format)
    app = FastAPI(title=title, version=settings.version, lifespan=lifespan)
    install_problem_handlers(app)
    app.add_middleware(HttpMiddleware, service=settings.service_name, max_body_bytes=settings.max_body_bytes)
    setup_tracing(settings.service_name, settings.otel_exporter_otlp_endpoint, app)
    app.state.settings = settings
    return app


def encode_cursor(values: list[Any]) -> str:
    raw = json.dumps(values, separators=(",", ":"), default=str).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor: str | None, arity: int) -> list[Any] | None:
    """Decode an opaque cursor; a malformed one is a 422, never a 500."""
    if cursor is None or cursor == "":
        return None
    try:
        if len(cursor) > 512:
            raise ValueError("too long")
        padded = cursor + "=" * (-len(cursor) % 4)
        values = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        if not isinstance(values, list) or len(values) != arity:
            raise ValueError("bad shape")
        if not all(isinstance(v, str | int) for v in values):
            raise ValueError("bad types")
        return values
    except (ValueError, UnicodeError, json.JSONDecodeError):
        raise validation(
            "The cursor is malformed.",
            [{"loc": ["query", "cursor"], "msg": "malformed cursor", "type": "value_error"}],
        ) from None


async def noop_lifespan(_: FastAPI) -> AsyncIterator[None]:
    yield
