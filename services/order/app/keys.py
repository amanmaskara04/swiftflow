"""Ed25519 signing keys: load from the environment or create once in
``orders.signing_keys``; publish public keys as a JWKS; issue tokens."""

from __future__ import annotations

import base64
import hashlib
import time
import uuid
from typing import Any

import asyncpg
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from swiftflow_common.auth import jwk_to_public_key


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def public_jwk(public_key: Ed25519PublicKey) -> dict[str, str]:
    raw = public_key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    kid = _b64u(hashlib.sha256(raw).digest())[:16]
    return {"kty": "OKP", "crv": "Ed25519", "x": _b64u(raw), "kid": kid, "use": "sig", "alg": "EdDSA"}


def generate_pem() -> str:
    key = Ed25519PrivateKey.generate()
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode(
        "ascii"
    )


class KeyStore:
    def __init__(self, pool: asyncpg.Pool, env_pem: str | None) -> None:
        self.pool = pool
        self.env_pem = (env_pem or "").strip() or None
        self.signing_key: Ed25519PrivateKey | None = None
        self.kid = ""
        self.jwks: list[dict[str, Any]] = []
        self._public: dict[str, Any] = {}
        self._loaded_at = 0.0

    async def load(self) -> None:
        async with self.pool.acquire() as conn:
            if self.env_pem:
                pem = self.env_pem.replace("\\n", "\n")
            else:
                row = await conn.fetchrow(
                    "SELECT private_pem FROM signing_keys WHERE retired_at IS NULL ORDER BY created_at, kid LIMIT 1"
                )
                if row is None:
                    candidate = generate_pem()
                    key = serialization.load_pem_private_key(candidate.encode(), password=None)
                    jwk = public_jwk(key.public_key())  # type: ignore[arg-type]
                    await conn.execute(
                        "INSERT INTO signing_keys (kid, private_pem, public_jwk) VALUES ($1, $2, $3) ON CONFLICT DO NOTHING",
                        jwk["kid"],
                        candidate,
                        jwk,
                    )
                    # Another replica may have won the race: everyone uses the oldest key.
                    row = await conn.fetchrow(
                        "SELECT private_pem FROM signing_keys WHERE retired_at IS NULL ORDER BY created_at, kid LIMIT 1"
                    )
                pem = row["private_pem"]
            key = serialization.load_pem_private_key(pem.encode(), password=None)
            if not isinstance(key, Ed25519PrivateKey):
                raise RuntimeError("JWT signing key must be Ed25519")
            own = public_jwk(key.public_key())
            stored = [
                dict(r["public_jwk"])
                for r in await conn.fetch("SELECT public_jwk FROM signing_keys WHERE retired_at IS NULL ORDER BY created_at")
            ]
        jwks = {own["kid"]: own}
        for j in stored:
            jwks.setdefault(j["kid"], j)
        self.signing_key, self.kid = key, own["kid"]
        self.jwks = list(jwks.values())
        self._public = {kid: jwk_to_public_key(j) for kid, j in jwks.items()}
        self._loaded_at = time.monotonic()

    async def get_key(self, kid: str, allow_refresh: bool = True) -> Any | None:
        """Key provider for the local ``TokenVerifier``."""
        if kid not in self._public and allow_refresh and time.monotonic() - self._loaded_at > 30:
            await self.load()
        return self._public.get(kid)

    def issue(
        self,
        *,
        issuer: str,
        audience: str,
        ttl: int,
        client_id: str,
        tenant_id: str,
        tenant_slug: str,
        role: str,
        scopes: list[str],
    ) -> str:
        now = int(time.time())
        claims = {
            "iss": issuer,
            "aud": audience,
            "sub": client_id,
            "tenant_id": tenant_id,
            "tenant_slug": tenant_slug,
            "role": role,
            "scope": " ".join(scopes),
            "iat": now,
            "exp": now + ttl,
            "jti": uuid.uuid4().hex,
        }
        return jwt.encode(claims, self.signing_key, algorithm="EdDSA", headers={"kid": self.kid})
