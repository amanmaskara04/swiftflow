"""Standard ``/healthz``, ``/readyz`` and ``/metrics`` routes, plus a tiny
health/metrics HTTP server for worker processes."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, FastAPI
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

Check = Callable[[], Awaitable[Any]]


async def run_checks(checks: dict[str, Check], timeout: float = 1.0) -> tuple[bool, dict[str, str]]:  # noqa: ASYNC109
    async def one(fn: Check) -> str:
        try:
            await asyncio.wait_for(fn(), timeout=timeout)
            return "up"
        except Exception:
            return "down"

    names = list(checks)
    results = await asyncio.gather(*(one(checks[n]) for n in names))
    out = dict(zip(names, results, strict=True))
    return all(v == "up" for v in out.values()), out


def health_router(checks: Callable[[], dict[str, Check]]) -> APIRouter:
    """``checks`` is called per request so it can read state set at start-up."""
    router = APIRouter(include_in_schema=False)

    @router.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @router.get("/readyz")
    async def readyz() -> JSONResponse:
        ok, results = await run_checks(checks())
        return JSONResponse({"status": "ready" if ok else "not_ready", "checks": results}, status_code=200 if ok else 503)

    @router.get("/metrics")
    async def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return router


def worker_app(status: Callable[[], dict[str, Any]]) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"status": "ok", **status()}

    @app.get("/metrics")
    async def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app


async def serve_worker_http(app: FastAPI, port: int) -> None:
    import uvicorn

    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None  # type: ignore[method-assign]
    await server.serve()
