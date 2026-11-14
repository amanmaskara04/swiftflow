"""Per-tenant token bucket in Redis (Lua, atomic). Fails open if Redis is down."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any

import structlog
from fastapi import Request

from .problems import ProblemError

log = structlog.get_logger("ratelimit")


@dataclass(frozen=True)
class BucketResult:
    allowed: bool
    tokens: float
    retry_after: float  # seconds until one token is available (0 when allowed)


def bucket_step(
    tokens: float | None, last_ms: int | None, now_ms: int, rate: float, burst: int, cost: float = 1.0
) -> tuple[BucketResult, float]:
    """Pure token-bucket maths; the Lua script below is a line-by-line port.

    Returns the result and the new token count.
    """
    if tokens is None or last_ms is None:
        tokens = float(burst)
        last_ms = now_ms
    elapsed = max(0, now_ms - last_ms) / 1000.0
    tokens = min(float(burst), tokens + elapsed * rate)
    if tokens >= cost:
        tokens -= cost
        return BucketResult(True, tokens, 0.0), tokens
    retry = (cost - tokens) / rate if rate > 0 else math.inf
    return BucketResult(False, tokens, retry), tokens


LUA_TOKEN_BUCKET = """
local key = KEYS[1]
local rate = tonumber(ARGV[1])
local burst = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local cost = tonumber(ARGV[4])
local data = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(data[1])
local ts = tonumber(data[2])
if tokens == nil or ts == nil then
  tokens = burst
  ts = now
end
local elapsed = math.max(0, now - ts) / 1000.0
tokens = math.min(burst, tokens + elapsed * rate)
local allowed = 0
local retry = 0
if tokens >= cost then
  tokens = tokens - cost
  allowed = 1
else
  retry = (cost - tokens) / rate
end
redis.call('HSET', key, 'tokens', tokens, 'ts', now)
redis.call('PEXPIRE', key, math.ceil((burst / rate) * 1000) + 1000)
return {allowed, tostring(tokens), tostring(retry)}
"""


class RateLimiter:
    def __init__(self, redis: Any, rate: float, burst: int, prefix: str = "rl") -> None:
        self.redis = redis
        self.rate = rate
        self.burst = burst
        self.prefix = prefix
        self._script = redis.register_script(LUA_TOKEN_BUCKET) if redis is not None else None

    async def check(self, key: str, cost: float = 1.0) -> BucketResult | None:
        if self._script is None:
            return None
        try:
            now_ms = int(time.time() * 1000)
            allowed, tokens, retry = await self._script(keys=[f"{self.prefix}:{key}"], args=[self.rate, self.burst, now_ms, cost])
            return BucketResult(bool(int(allowed)), float(tokens), float(retry))
        except Exception as exc:  # fail open
            log.warning("ratelimit_unavailable", error=str(exc))
            return None

    async def enforce(self, request: Request, key: str) -> None:
        result = await self.check(key)
        if result is None:
            return
        request.state.ratelimit_remaining = max(0, math.floor(result.tokens))
        if not result.allowed:
            retry_after = max(1, math.ceil(result.retry_after))
            raise ProblemError(
                429,
                "/problems/rate-limited",
                "Too many requests",
                "The rate limit for this tenant was exceeded.",
                headers={"Retry-After": str(retry_after)},
            )
