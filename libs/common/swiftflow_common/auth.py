"""JWT verification (EdDSA only) via JWKS, ``Principal`` and ``require_scope``."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
import jwt
import structlog
from fastapi import Request
from jwt.algorithms import OKPAlgorithm

from .problems import ProblemError, forbidden, unauthenticated

log = structlog.get_logger("auth")

ALLOWED_ALGORITHMS = ["EdDSA"]  # explicit allow-list; never taken from the token

ROLE_SCOPES: dict[str, list[str]] = {
    "admin": ["admin", "orders:read", "orders:write", "inventory:read", "webhooks:read", "webhooks:write"],
    "partner": ["orders:read", "orders:write", "inventory:read", "webhooks:read", "webhooks:write"],
}


@dataclass(frozen=True)
class Principal:
    tenant_id: str
    client_id: str
    role: str
    scopes: frozenset[str] = field(default_factory=frozenset)
    tenant_slug: str | None = None

    def has(self, scope: str) -> bool:
        return scope in self.scopes


def jwk_to_public_key(jwk: dict[str, Any]) -> Any:
    if jwk.get("kty") != "OKP" or jwk.get("crv") != "Ed25519":
        raise ValueError("only OKP/Ed25519 keys are accepted")
    return OKPAlgorithm.from_jwk(json.dumps(jwk))


KeyProvider = Callable[[str, bool], Awaitable[Any | None]]


class JWKSClient:
    """Fetches and caches the issuer's JWKS (10 minutes). An unknown ``kid``
    triggers at most one refresh every 30 seconds."""

    def __init__(self, url: str, ttl: float = 600.0, min_refresh_interval: float = 30.0) -> None:
        self.url = url
        self.ttl = ttl
        self.min_refresh_interval = min_refresh_interval
        self._keys: dict[str, Any] = {}
        self._fetched_at = float("-inf")
        self._lock = asyncio.Lock()

    async def _refresh(self) -> None:
        async with httpx.AsyncClient(timeout=3.0, trust_env=False) as client:
            resp = await client.get(self.url)
            resp.raise_for_status()
            data = resp.json()
        keys: dict[str, Any] = {}
        for jwk in data.get("keys", []):
            kid = jwk.get("kid")
            if not kid:
                continue
            try:
                keys[kid] = jwk_to_public_key(jwk)
            except Exception:
                log.warning("jwks_key_skipped", kid=kid)
        self._keys = keys
        self._fetched_at = time.monotonic()

    def _needs_refresh(self, kid: str, allow_refresh: bool) -> bool:
        age = time.monotonic() - self._fetched_at
        if age > self.ttl:
            return True
        return kid not in self._keys and allow_refresh and age > self.min_refresh_interval

    async def get_key(self, kid: str, allow_refresh: bool = True) -> Any | None:
        if self._needs_refresh(kid, allow_refresh):
            async with self._lock:
                if self._needs_refresh(kid, allow_refresh):
                    try:
                        await self._refresh()
                    except Exception as exc:
                        log.warning("jwks_fetch_failed", error=str(exc))
                        # back off for min_refresh_interval before trying again
                        self._fetched_at = time.monotonic() - self.ttl + self.min_refresh_interval
                        if not self._keys:
                            self._fetched_at = float("-inf")
                            raise ProblemError(
                                503,
                                "/problems/unavailable",
                                "Key set unavailable",
                                "Token keys could not be loaded; try again shortly.",
                            ) from None
        return self._keys.get(kid)


class TokenVerifier:
    def __init__(self, issuer: str, audience: str, key_provider: KeyProvider) -> None:
        self.issuer = issuer
        self.audience = audience
        self.key_provider = key_provider

    async def verify(self, token: str) -> Principal:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            raise unauthenticated("Malformed token.") from None
        if header.get("alg") not in ALLOWED_ALGORITHMS:
            raise unauthenticated("Unsupported token algorithm.")
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise unauthenticated("Token has no key id.")
        key = await self.key_provider(kid, True)
        if key is None:
            raise unauthenticated("Unknown signing key.")
        try:
            claims = jwt.decode(
                token,
                key=key,
                algorithms=ALLOWED_ALGORITHMS,
                audience=self.audience,
                issuer=self.issuer,
                options={"require": ["exp", "iat", "iss", "aud", "sub", "tenant_id", "scope", "role"]},
                leeway=5,
            )
        except jwt.ExpiredSignatureError:
            raise unauthenticated("Token expired.") from None
        except jwt.PyJWTError:
            raise unauthenticated("Invalid token.") from None
        return principal_from_claims(claims)


def principal_from_claims(claims: dict[str, Any]) -> Principal:
    try:
        tenant_id = str(uuid.UUID(str(claims["tenant_id"])))
    except (KeyError, ValueError):
        raise unauthenticated("Token has no valid tenant.") from None
    scope = claims.get("scope")
    if not isinstance(scope, str):
        raise unauthenticated("Token has no scope.")
    role = claims.get("role")
    if role not in ROLE_SCOPES:
        raise unauthenticated("Token has an unknown role.")
    slug = claims.get("tenant_slug")
    return Principal(
        tenant_id=tenant_id,
        client_id=str(claims.get("sub")),
        role=role,
        scopes=frozenset(scope.split()),
        tenant_slug=slug if isinstance(slug, str) else None,
    )


def bearer_token(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    scheme, _, token = auth.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise unauthenticated("Missing bearer token.")
    return token.strip()


async def authenticate(request: Request) -> Principal:
    """Verify the bearer token and apply the tenant's rate limit."""
    cached = getattr(request.state, "principal", None)
    if cached is not None:
        return cached
    verifier: TokenVerifier = request.app.state.verifier
    principal = await verifier.verify(bearer_token(request))
    limiter = getattr(request.app.state, "rate_limiter", None)
    if limiter is not None:
        await limiter.enforce(request, f"tenant:{principal.tenant_id}")
    request.state.principal = principal
    return principal


def require_scope(scope: str | None = None) -> Callable[[Request], Awaitable[Principal]]:
    """FastAPI dependency: a valid token, and ``scope`` when given."""

    async def dependency(request: Request) -> Principal:
        principal = await authenticate(request)
        if scope is not None and not principal.has(scope):
            raise forbidden(f"This action requires the '{scope}' scope.")
        return principal

    dependency.__name__ = f"require_scope_{scope or 'any'}"
    return dependency
