"""Unit tests for swiftflow_common: no infrastructure needed."""

from __future__ import annotations

import asyncio
import time
import uuid

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import Depends
from fastapi.testclient import TestClient
from swiftflow_common import signing
from swiftflow_common.app import build_app, decode_cursor, encode_cursor
from swiftflow_common.auth import ROLE_SCOPES, TokenVerifier, require_scope
from swiftflow_common.circuit_breaker import CLOSED, HALF_OPEN, OPEN, CircuitBreaker, CircuitOpenError
from swiftflow_common.config import BaseServiceSettings
from swiftflow_common.consumer import backoff_delay
from swiftflow_common.idempotency import IdempotencyStore, request_hash, validate_key
from swiftflow_common.problems import ProblemError
from swiftflow_common.ratelimit import bucket_step

ISS, AUD = "swiftflow-order", "swiftflow"
TENANT = str(uuid.uuid4())


# --------------------------------------------------------------------------- JWT


@pytest.fixture(scope="module")
def keypair():
    priv = Ed25519PrivateKey.generate()
    return priv, priv.public_key()


def make_token(priv, **overrides):
    now = int(time.time())
    claims = {
        "iss": ISS,
        "aud": AUD,
        "sub": "acme-admin",
        "tenant_id": TENANT,
        "role": "admin",
        "scope": " ".join(ROLE_SCOPES["admin"]),
        "iat": now,
        "exp": now + 900,
        "jti": uuid.uuid4().hex,
    }
    claims.update(overrides)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, priv, algorithm="EdDSA", headers={"kid": "k1"})


def verifier_for(pub):
    async def provider(kid, _refresh):
        return pub if kid == "k1" else None

    return TokenVerifier(ISS, AUD, provider)


def verify(v, token):
    return asyncio.run(v.verify(token))


def test_valid_token(keypair):
    priv, pub = keypair
    p = verify(verifier_for(pub), make_token(priv))
    assert p.tenant_id == TENANT and p.has("admin") and p.client_id == "acme-admin"


@pytest.mark.parametrize(
    "overrides",
    [
        {"exp": int(time.time()) - 60},
        {"aud": "someone-else"},
        {"iss": "evil"},
        {"tenant_id": None},
        {"tenant_id": "not-a-uuid"},
        {"role": "superuser"},
    ],
)
def test_bad_claims_rejected(keypair, overrides):
    priv, pub = keypair
    with pytest.raises(ProblemError) as e:
        verify(verifier_for(pub), make_token(priv, **overrides))
    assert e.value.status == 401


def test_token_from_other_key_rejected(keypair):
    _, pub = keypair
    other = Ed25519PrivateKey.generate()
    with pytest.raises(ProblemError) as e:
        verify(verifier_for(pub), make_token(other))
    assert e.value.status == 401


def test_alg_none_rejected(keypair):
    _, pub = keypair
    token = jwt.encode({"iss": ISS, "aud": AUD, "tenant_id": TENANT}, None, algorithm="none", headers={"kid": "k1"})
    with pytest.raises(ProblemError) as e:
        verify(verifier_for(pub), token)
    assert e.value.status == 401


def test_hs256_with_public_key_rejected(keypair):
    _, pub = keypair
    pem = pub.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    import base64
    import hashlib
    import hmac
    import json

    def b64(b):
        return base64.urlsafe_b64encode(b).rstrip(b"=")

    header = b64(json.dumps({"alg": "HS256", "kid": "k1", "typ": "JWT"}).encode())
    now = int(time.time())
    body = b64(
        json.dumps(
            {
                "iss": ISS,
                "aud": AUD,
                "sub": "x",
                "tenant_id": TENANT,
                "role": "admin",
                "scope": "admin",
                "iat": now,
                "exp": now + 60,
            }
        ).encode()
    )
    sig = b64(hmac.new(pem, header + b"." + body, hashlib.sha256).digest())
    token = (header + b"." + body + b"." + sig).decode()
    with pytest.raises(ProblemError) as e:
        verify(verifier_for(pub), token)
    assert e.value.status == 401


def _scope_app(keypair):
    _, pub = keypair
    app = build_app(BaseServiceSettings(service_name="test"), title="t")
    app.state.verifier = verifier_for(pub)

    @app.get("/admin-only")
    async def admin_only(p=Depends(require_scope("admin"))):
        return {"tenant": p.tenant_id}

    @app.get("/boom")
    async def boom():
        raise RuntimeError("SELECT secret FROM table -- should never leak")

    return app


def test_scope_checks(keypair):
    priv, _ = keypair
    client = TestClient(_scope_app(keypair), raise_server_exceptions=False)
    assert client.get("/admin-only").status_code == 401
    ok = client.get("/admin-only", headers={"Authorization": f"Bearer {make_token(priv)}"})
    assert ok.status_code == 200 and ok.json()["tenant"] == TENANT
    partner = make_token(priv, role="partner", scope=" ".join(ROLE_SCOPES["partner"]))
    r = client.get("/admin-only", headers={"Authorization": f"Bearer {partner}"})
    assert r.status_code == 403
    assert r.headers["content-type"].startswith("application/problem+json")
    assert r.json()["type"] == "/problems/forbidden"


def test_unhandled_error_hides_details(keypair):
    client = TestClient(_scope_app(keypair), raise_server_exceptions=False)
    r = client.get("/boom", headers={"X-Request-ID": "req-test-1"})
    assert r.status_code == 500
    body = r.json()
    assert body["request_id"] == "req-test-1"
    assert "SELECT" not in r.text and "Traceback" not in r.text


def test_body_limit(keypair):
    app = build_app(BaseServiceSettings(service_name="test", max_body_bytes=100), title="t")

    @app.post("/echo")
    async def echo(body: dict):
        return body

    client = TestClient(app)
    r = client.post("/echo", content=b'{"a":"' + b"x" * 200 + b'"}', headers={"content-type": "application/json"})
    assert r.status_code == 413
    assert r.json()["type"] == "/problems/payload-too-large"
    assert client.post("/echo", json={"a": 1}).status_code == 200


# --------------------------------------------------------------------------- idempotency


def test_request_hash_ignores_key_order():
    assert request_hash("POST", "/v1/orders", {"a": 1, "b": [1, 2]}) == request_hash("post", "/v1/orders", {"b": [1, 2], "a": 1})
    assert request_hash("POST", "/v1/orders", {"a": 1}) != request_hash("POST", "/v1/orders", {"a": 2})


def test_idempotency_key_validation():
    with pytest.raises(ProblemError):
        validate_key(None)
    with pytest.raises(ProblemError):
        validate_key("x" * 300)
    assert validate_key("k-1") == "k-1"
    assert validate_key(None, required=False) is None


class FakeRedis:
    def __init__(self):
        self.data = {}

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.data:
            return None
        self.data[key] = value
        return True

    async def get(self, key):
        return self.data.get(key)

    async def delete(self, key):
        self.data.pop(key, None)


def test_idempotency_store_flow():
    async def run():
        store = IdempotencyStore(FakeRedis(), "t")
        assert await store.begin(TENANT, "k", "h1") is None
        with pytest.raises(ProblemError) as e:
            await store.begin(TENANT, "k", "h1")
        assert e.value.status == 409
        with pytest.raises(ProblemError) as e:
            await store.begin(TENANT, "k", "h2")
        assert e.value.status == 422
        await store.complete(TENANT, "k", "h1", 202, {"id": "o1"})
        replay = await store.begin(TENANT, "k", "h1")
        assert replay is not None and replay.status == 202 and replay.body == {"id": "o1"}
        # another tenant with the same key is independent
        assert await store.begin(str(uuid.uuid4()), "k", "h2") is None

    asyncio.run(run())


# --------------------------------------------------------------------------- rate limit


def test_token_bucket_maths():
    res, tokens = bucket_step(None, None, 0, rate=10, burst=3)
    assert res.allowed and tokens == 2
    res, tokens = bucket_step(tokens, 0, 0, rate=10, burst=3)
    res, tokens = bucket_step(tokens, 0, 0, rate=10, burst=3)
    assert res.allowed and tokens == 0
    res, tokens = bucket_step(tokens, 0, 0, rate=10, burst=3)
    assert not res.allowed and res.retry_after == pytest.approx(0.1)
    res, tokens = bucket_step(tokens, 0, 100, rate=10, burst=3)  # 100 ms later: one token back
    assert res.allowed
    res, tokens = bucket_step(0.0, 0, 60_000, rate=10, burst=3)  # refill is capped at burst
    assert tokens == 2


# --------------------------------------------------------------------------- circuit breaker


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_circuit_breaker_transitions():
    clock = Clock()
    cb = CircuitBreaker("t", failure_threshold=3, reset_timeout=10, clock=clock)
    assert cb.state == CLOSED
    for _ in range(3):
        assert cb.allow()
        cb.record_failure()
    assert cb.state == OPEN and not cb.allow()
    clock.t = 10
    assert cb.state == HALF_OPEN
    assert cb.allow()  # one probe
    assert not cb.allow()  # second caller waits
    cb.record_failure()
    assert cb.state == OPEN
    clock.t = 25
    assert cb.allow()
    cb.record_success()
    assert cb.state == CLOSED


def test_circuit_breaker_call_raises_when_open():
    clock = Clock()
    cb = CircuitBreaker("t2", failure_threshold=1, reset_timeout=5, clock=clock)

    async def fail():
        raise RuntimeError("down")

    async def run():
        with pytest.raises(RuntimeError):
            await cb.call(fail)
        with pytest.raises(CircuitOpenError):
            await cb.call(fail)

    asyncio.run(run())


def test_success_resets_failure_count():
    cb = CircuitBreaker("t3", failure_threshold=2, clock=Clock())
    cb.record_failure()
    cb.record_success()
    cb.record_failure()
    assert cb.state == CLOSED


# --------------------------------------------------------------------------- signing


def test_signature_roundtrip_and_attacks():
    body = b'{"event_type":"OrderConfirmed"}'
    now = 1_763_115_300
    header = signing.sign("s3cret", body, timestamp=now)
    assert signing.verify("s3cret", header, body, now=now) == (True, "ok")
    assert signing.verify("s3cret", header, body + b" ", now=now)[0] is False  # tampered body
    assert signing.verify("wrong", header, body, now=now)[0] is False  # wrong secret
    assert signing.verify("s3cret", header, body, now=now + 301) == (False, "stale_timestamp")
    assert signing.verify("s3cret", "garbage", body, now=now) == (False, "malformed_signature")


# --------------------------------------------------------------------------- misc


def test_cursor_roundtrip_and_malformed():
    c = encode_cursor(["2026-11-14T10:00:00+00:00", "abc"])
    assert decode_cursor(c, 2) == ["2026-11-14T10:00:00+00:00", "abc"]
    assert decode_cursor(None, 2) is None
    for bad in ["!!!", "' OR 1=1 --", encode_cursor(["only-one"]), encode_cursor([{"x": 1}, 2])]:
        with pytest.raises(ProblemError) as e:
            decode_cursor(bad, 2)
        assert e.value.status == 422


def test_backoff_has_full_jitter():
    for attempt in range(4):
        for _ in range(50):
            assert 0 <= backoff_delay(attempt) <= 0.2 * 2**attempt


def test_rate_limit_script_is_pure_lua():
    """Guards against anything but Lua ending up in the script (a stray Python comment once
    broke it, and the limiter then failed open without any test noticing)."""
    from swiftflow_common.ratelimit import LUA_TOKEN_BUCKET

    first = LUA_TOKEN_BUCKET.strip().splitlines()[0]
    assert first.startswith("local "), first
    assert "#" not in LUA_TOKEN_BUCKET and "noqa" not in LUA_TOKEN_BUCKET
