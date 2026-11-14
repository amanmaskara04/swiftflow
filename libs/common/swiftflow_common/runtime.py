"""Start-up helpers shared by API and worker processes."""

from __future__ import annotations

import asyncio
import contextlib
import signal
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import asyncpg
import redis.asyncio as aioredis
import structlog
from fastapi import FastAPI

from .auth import JWKSClient, KeyProvider, TokenVerifier
from .config import BaseServiceSettings
from .db import check_postgres, create_pool
from .health import health_router, serve_worker_http, worker_app
from .idempotency import IdempotencyStore
from .migrate import apply_migrations
from .observability import configure_logging, setup_tracing
from .ratelimit import RateLimiter

log = structlog.get_logger("runtime")


async def open_pool(dsn: str, schema: str | None = None, migrations: Path | None = None, **kw: Any) -> asyncpg.Pool:
    """Wait for Postgres, apply migrations when given, and return a pool."""
    delay = 0.5
    while True:
        try:
            if schema and migrations:
                await apply_migrations(dsn, schema, migrations)
            return await create_pool(dsn, **kw)
        except (OSError, asyncpg.PostgresConnectionError, asyncpg.CannotConnectNowError) as exc:
            log.warning("postgres_not_ready", error=str(exc), retry_in=delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 5.0)


def open_redis(url: str) -> aioredis.Redis:
    return aioredis.from_url(url, decode_responses=True, socket_timeout=2.0, socket_connect_timeout=2.0)


def install_core(
    app: FastAPI,
    settings: BaseServiceSettings,
    *,
    pool: asyncpg.Pool | None,
    redis: aioredis.Redis | None,
    namespace: str,
    key_provider: KeyProvider | None = None,
) -> None:
    """Put the shared dependencies on ``app.state`` (verifier, limiter, idempotency)."""
    app.state.pool = pool
    app.state.redis = redis
    provider = key_provider or JWKSClient(settings.jwks_url).get_key
    app.state.verifier = TokenVerifier(settings.jwt_issuer, settings.jwt_audience, provider)
    if redis is not None:
        app.state.rate_limiter = RateLimiter(redis, settings.rate_limit_rps, settings.rate_limit_burst)
        app.state.idempotency = IdempotencyStore(redis, namespace)


def core_health(app: FastAPI) -> Any:
    def checks() -> dict[str, Callable[[], Awaitable[Any]]]:
        out: dict[str, Callable[[], Awaitable[Any]]] = {}
        pool = getattr(app.state, "pool", None)
        redis = getattr(app.state, "redis", None)
        if pool is not None:
            out["postgres"] = lambda: check_postgres(pool)
        if redis is not None:
            out["redis"] = lambda: redis.ping()
        return out

    return health_router(checks)


async def close_core(app: FastAPI) -> None:
    pool = getattr(app.state, "pool", None)
    redis = getattr(app.state, "redis", None)
    if pool is not None:
        await pool.close()
    if redis is not None:
        with contextlib.suppress(Exception):
            await redis.aclose()


async def run_worker(
    settings: BaseServiceSettings,
    port: int,
    start: Callable[[asyncio.Event, dict[str, Any]], Awaitable[list[Awaitable[Any]]]],
) -> None:
    """Run a worker process: ``start`` returns the long-running coroutines; this
    adds the health/metrics server and stops everything on SIGINT/SIGTERM.

    ``status`` (the dict passed to ``start``) is merged into ``/healthz``.
    """
    configure_logging(settings.service_name, settings.log_level, settings.log_format)
    setup_tracing(settings.service_name, settings.otel_exporter_otlp_endpoint)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, ValueError):  # not available on Windows
            loop.add_signal_handler(sig, stop.set)

    status: dict[str, Any] = {}

    def snapshot() -> dict[str, Any]:
        return {k: (v() if callable(v) else v) for k, v in status.items()}

    coros = await start(stop, status)
    tasks = [asyncio.create_task(c) for c in coros]  # type: ignore[arg-type]
    tasks.append(asyncio.create_task(serve_worker_http(worker_app(snapshot), port)))
    log.info("worker_started", port=port, tasks=len(tasks))
    stopper = asyncio.create_task(stop.wait())
    try:
        done, _ = await asyncio.wait([*tasks, stopper], return_when=asyncio.FIRST_COMPLETED)
        for t in done:
            if t is not stopper and t.exception() is not None:
                log.error("worker_task_crashed", error=repr(t.exception()))
    finally:
        stop.set()
        for t in [*tasks, stopper]:
            t.cancel()
        await asyncio.gather(*tasks, stopper, return_exceptions=True)
        log.info("worker_stopped")
