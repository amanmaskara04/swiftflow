"""Attacks that must fail. Run with the stack up:  pytest tests/security -q"""

import base64
import hashlib
import hmac
import json
import time
import uuid

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ACME_SKU = "MUG-ENAMEL"


def b64(d: bytes) -> str:
    return base64.urlsafe_b64encode(d).decode().rstrip("=")


def parts(token):
    h, p, s = token.split(".")
    dec = lambda x: json.loads(base64.urlsafe_b64decode(x + "=" * (-len(x) % 4)))  # noqa: E731
    return dec(h), dec(p), s


@pytest.fixture(scope="module")
def acme_order(http, auth):
    r = http.post(
        "/v1/orders",
        json={"items": [{"sku": ACME_SKU, "qty": 1}]},
        headers={**auth["acme-admin"], "Idempotency-Key": f"sec-{uuid.uuid4()}"},
    )
    assert r.status_code == 202, r.text
    return r.json()["id"]


# ------------------------------------------------------------------ tokens
def forged_tokens(real):
    head, claims, sig = parts(real)
    other = Ed25519PrivateKey.generate()
    admin = {**claims, "scope": "admin orders:read orders:write inventory:read", "role": "admin"}
    yield "payload changed, signature kept", ".".join([real.split(".")[0], b64(json.dumps(admin).encode()), sig])
    yield "signed with another key, real kid", jwt.encode(admin, other, algorithm="EdDSA", headers={"kid": head["kid"]})
    yield (
        "alg none",
        ".".join([b64(json.dumps({"alg": "none", "kid": head["kid"]}).encode()), b64(json.dumps(admin).encode()), ""]),
    )
    hs_head = b64(json.dumps({"alg": "HS256", "kid": head["kid"], "typ": "JWT"}).encode())
    body = b64(json.dumps(admin).encode())
    yield "HS256 with empty key", f"{hs_head}.{body}.{b64(hmac.new(b'', f'{hs_head}.{body}'.encode(), hashlib.sha256).digest())}"
    yield (
        "expired",
        jwt.encode({**claims, "exp": int(time.time()) - 3600}, other, algorithm="EdDSA", headers={"kid": head["kid"]}),
    )
    yield "no kid", jwt.encode(admin, other, algorithm="EdDSA")
    yield "unknown kid", jwt.encode(admin, other, algorithm="EdDSA", headers={"kid": "attacker"})
    yield "garbage", "aaaa.bbbb.cccc"
    yield "one segment", "x"


def test_forged_tokens_are_rejected_by_every_service(http, tokens):
    paths = [
        "/v1/orders",
        "/v1/inventory/stock",
        "/v1/events",
        "/v1/webhooks/subscriptions",
        "/v1/sim/config",
        "/v1/system/health",
    ]
    for name, tok in forged_tokens(tokens["acme-partner"]):
        for path in paths:
            r = http.get(path, headers={"Authorization": f"Bearer {tok}"})
            assert r.status_code == 401, f"{name} was accepted by {path}: {r.status_code}"


def test_hs256_signed_with_the_public_key_is_rejected(http, tokens):
    """The classic key-confusion attack: use the published public key as an HMAC secret."""
    head, claims, _ = parts(tokens["acme-partner"])
    jwk = next(k for k in http.get("/.well-known/jwks.json").json()["keys"] if k["kid"] == head["kid"])
    public = base64.urlsafe_b64decode(jwk["x"] + "==")
    h = b64(json.dumps({"alg": "HS256", "kid": head["kid"]}).encode())
    p = b64(json.dumps({**claims, "scope": "admin", "role": "admin"}).encode())
    tok = f"{h}.{p}.{b64(hmac.new(public, f'{h}.{p}'.encode(), hashlib.sha256).digest())}"
    assert http.get("/v1/orders", headers={"Authorization": f"Bearer {tok}"}).status_code == 401


def test_missing_and_malformed_auth(http):
    for headers in ({}, {"Authorization": "Basic YWRtaW46YWRtaW4="}, {"Authorization": "Bearer"}, {"Authorization": "Token abc"}):
        assert http.get("/v1/orders", headers=headers).status_code == 401


def test_jwks_never_exposes_private_material(http):
    for k in http.get("/.well-known/jwks.json").json()["keys"]:
        assert set(k) <= {"kty", "crv", "x", "kid", "use", "alg"} and "d" not in k


def test_wrong_secret_and_unknown_client_look_the_same(http):
    a = http.post("/v1/auth/token", json={"client_id": "acme-admin", "client_secret": "wrong"})
    b = http.post("/v1/auth/token", json={"client_id": "no-such-client", "client_secret": "wrong"})
    assert a.status_code == b.status_code == 401
    assert a.json()["detail"] == b.json()["detail"]  # no client enumeration


def test_token_endpoint_throttles_guessing(http):
    cid = f"brute-{uuid.uuid4().hex[:8]}"
    codes = [http.post("/v1/auth/token", json={"client_id": cid, "client_secret": f"guess{i}"}).status_code for i in range(30)]
    assert 429 in codes, codes


# ------------------------------------------------------------------ scopes
def test_partner_cannot_use_admin_endpoints(http, auth, acme_order, key):
    p = {**auth["acme-partner"], **key}
    assert http.post(f"/v1/orders/{acme_order}/ship", headers=p).status_code == 403
    assert http.post("/v1/products", json={"sku": "X", "name": "x", "price_cents": 1}, headers=p).status_code == 403
    assert http.post("/v1/warehouses", json={"code": "X", "name": "x"}, headers=p).status_code == 403
    assert (
        http.post(
            "/v1/inventory/adjustments",
            headers=p,
            json={"sku": ACME_SKU, "warehouse_code": "BLR-1", "mode": "set", "quantity": 9},
        ).status_code
        == 403
    )
    for method, path in (
        ("GET", "/v1/sim/config"),
        ("PUT", "/v1/sim/config"),
        ("GET", "/v1/sim/payments"),
        ("GET", "/v1/sim/hooks/demo"),
    ):
        assert http.request(method, path, json={"outage": True}, headers=p).status_code == 403


# ------------------------------------------------------------------ tenant isolation
def test_other_tenant_cannot_see_or_touch_an_order(http, auth, acme_order, key):
    g = {**auth["globex-admin"], **key}
    assert http.get(f"/v1/orders/{acme_order}", headers=g).status_code == 404
    assert http.post(f"/v1/orders/{acme_order}/cancel", headers=g).status_code == 404
    assert http.post(f"/v1/orders/{acme_order}/ship", headers=g).status_code == 404
    assert acme_order not in json.dumps(http.get("/v1/orders?limit=100", headers=g).json())
    assert acme_order not in json.dumps(http.get(f"/v1/orders?q={acme_order[:8]}", headers=g).json())
    assert http.get(f"/v1/events?aggregate_id={acme_order}", headers=g).json()["items"] == []


def test_other_tenant_sees_none_of_the_catalogue(http, auth):
    g = auth["globex-admin"]
    assert http.get(f"/v1/inventory/{ACME_SKU}/availability", headers=g).status_code == 404
    stock = http.get("/v1/inventory/stock", headers=g).json()
    assert ACME_SKU not in {r["sku"] for r in stock["rows"]} and "BLR-1" not in {w["code"] for w in stock["warehouses"]}
    assert ACME_SKU not in json.dumps(http.get("/v1/products?limit=100", headers=g).json())


def test_tenant_in_the_request_is_ignored(http, auth, tokens, key):
    acme_tenant = parts(tokens["acme-admin"])[1]["tenant_id"]
    r = http.post(
        "/v1/orders",
        headers={**auth["globex-admin"], **key},
        json={"tenant_id": acme_tenant, "items": [{"sku": "WIDGET-A", "qty": 1}]},
    )
    assert r.status_code == 202
    assert http.get(f"/v1/orders/{r.json()['id']}?tenant_id={acme_tenant}", headers=auth["acme-admin"]).status_code == 404


def test_webhook_endpoints_and_receiver_are_per_tenant(http, auth):
    sub = http.post(
        "/v1/webhooks/subscriptions",
        headers=auth["acme-admin"],
        json={"url": "http://partner-sim:8004/v1/hooks/acme-sectest", "event_types": ["OrderShipped"]},
    ).json()
    try:
        g = auth["globex-admin"]
        assert sub["id"] not in json.dumps(http.get("/v1/webhooks/subscriptions", headers=g).json())
        assert http.get(f"/v1/webhooks/subscriptions/{sub['id']}", headers=g).status_code == 404
        assert http.delete(f"/v1/webhooks/subscriptions/{sub['id']}", headers=g).status_code == 404
        assert http.post(f"/v1/webhooks/subscriptions/{sub['id']}/test", headers=g).status_code == 404
        # Globex asking for Acme's inbox by name gets its own (empty) namespaced inbox instead
        assert http.get("/v1/sim/hooks/acme-sectest", headers=g).json()["inbox"] == "globex-acme-sectest"
        assert "signing_secret" not in http.get(f"/v1/webhooks/subscriptions/{sub['id']}", headers=auth["acme-admin"]).json()
    finally:
        http.delete(f"/v1/webhooks/subscriptions/{sub['id']}", headers=auth["acme-admin"])


def test_idempotency_keys_do_not_cross_tenants(http, auth):
    k = {"Idempotency-Key": f"shared-{uuid.uuid4()}"}
    a = http.post("/v1/orders", json={"items": [{"sku": ACME_SKU, "qty": 1}]}, headers={**auth["acme-admin"], **k})
    g = http.post("/v1/orders", json={"items": [{"sku": "WIDGET-A", "qty": 1}]}, headers={**auth["globex-admin"], **k})
    assert a.status_code == g.status_code == 202 and a.json()["id"] != g.json()["id"]
    again = http.post("/v1/orders", json={"items": [{"sku": ACME_SKU, "qty": 2}]}, headers={**auth["acme-admin"], **k})
    assert again.status_code == 422 and again.json()["type"].endswith("idempotency-mismatch")


# ------------------------------------------------------------------ injection and input limits
@pytest.mark.parametrize(
    "q", ["' OR '1'='1", "'; DROP TABLE orders;--", "%", "_", "\\", "%' UNION SELECT secret_hash FROM api_clients--", "\x00"]
)
def test_sql_metacharacters_are_data(http, auth, q):
    r = http.get("/v1/orders", params={"q": q}, headers=auth["acme-admin"])
    assert r.status_code in (200, 422) and "secret_hash" not in r.text
    if r.status_code == 200 and q in ("' OR '1'='1", "%' UNION SELECT secret_hash FROM api_clients--"):
        assert r.json()["items"] == []
    r = http.get("/v1/products", params={"q": q}, headers=auth["acme-admin"])
    assert r.status_code in (200, 422)


def test_wildcards_do_not_match_everything(http, auth):
    assert http.get("/v1/orders", params={"q": "%"}, headers=auth["acme-admin"]).json()["items"] == []


@pytest.mark.parametrize(
    "cursor", ["not-base64!!", "e30", b64(b'["x","y"]'), b64(b"[1]"), "A" * 600, b64(b"'; DROP TABLE orders;--")]
)
def test_malformed_cursors_are_422_not_500(http, auth, cursor):
    for path in ("/v1/orders", "/v1/events", "/v1/webhooks/deliveries"):
        assert http.get(path, params={"cursor": cursor}, headers=auth["acme-admin"]).status_code == 422, path


@pytest.mark.parametrize(
    "body",
    [
        {"items": []},
        {"items": [{"sku": ACME_SKU, "qty": 0}]},
        {"items": [{"sku": ACME_SKU, "qty": 10**9}]},
        {"items": [{"sku": ACME_SKU, "qty": -1}]},
        {"items": [{"sku": "a b; rm -rf /", "qty": 1}]},
        {"items": [{"sku": ACME_SKU, "qty": 1}] * 2},
        {"items": [{"sku": f"S{i}", "qty": 1} for i in range(51)]},
        {"items": [{"sku": ACME_SKU, "qty": 1}], "customer_ref": "x" * 201},
        {"items": "nope"},
        {},
    ],
)
def test_invalid_orders_are_rejected(http, auth, key, body):
    r = http.post("/v1/orders", json=body, headers={**auth["acme-admin"], **key})
    assert r.status_code == 422 and r.headers["content-type"].startswith("application/problem+json")


def test_idempotency_key_rules(http, auth):
    body = {"items": [{"sku": ACME_SKU, "qty": 1}]}
    assert http.post("/v1/orders", json=body, headers=auth["acme-admin"]).status_code == 422
    assert http.post("/v1/orders", json=body, headers={**auth["acme-admin"], "Idempotency-Key": "k" * 300}).status_code == 422


def test_oversized_body_is_413(http, auth, key):
    r = http.post(
        "/v1/orders",
        content=b'{"items":[],"pad":"' + b"x" * 1_200_000 + b'"}',
        headers={**auth["acme-admin"], **key, "Content-Type": "application/json"},
    )
    assert r.status_code == 413


def test_adjustment_cannot_go_negative_or_below_reserved(http, auth, key):
    h = {**auth["acme-admin"], **key}
    r = http.post(
        "/v1/inventory/adjustments",
        headers=h,
        json={"sku": ACME_SKU, "warehouse_code": "BLR-1", "mode": "delta", "quantity": -(10**6)},
    )
    assert r.status_code in (409, 422)
    r = http.post(
        "/v1/inventory/adjustments",
        headers={**auth["acme-admin"], "Idempotency-Key": str(uuid.uuid4()), "If-Match": "999999"},
        json={"sku": ACME_SKU, "warehouse_code": "BLR-1", "mode": "delta", "quantity": 1},
    )
    assert r.status_code == 409 and r.json()["type"].endswith("version-conflict")


# ------------------------------------------------------------------ SSRF
@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",
        "https://169.254.169.254/",
        "http://localhost:5432/",
        "https://127.0.0.1/",
        "https://[::1]/",
        "http://postgres:5432/",
        "http://redis:6379/",
        "http://order-api:8001/metrics",
        "http://kafka:29092/",
        "http://partner-sim:8004@evil.example/",
        "http://partner-sim:9999/",
        "https://10.0.0.1/hook",
        "https://192.168.1.1/",
        "file:///etc/passwd",
        "gopher://localhost:6379/_FLUSHALL",
        "ftp://example.com/x",
        "https://0.0.0.0/",
        "https://0x7f000001/",
        "https://2130706433/",
        "https://localhost/",
        "https://example.com/\r\nHost: evil",
    ],
)
def test_webhook_urls_cannot_point_inside(http, auth, url):
    r = http.post("/v1/webhooks/subscriptions", headers=auth["acme-admin"], json={"url": url, "event_types": ["*"]})
    assert r.status_code == 422, f"{url} was accepted"


def test_unknown_event_types_are_rejected(http, auth):
    r = http.post(
        "/v1/webhooks/subscriptions",
        headers=auth["acme-admin"],
        json={"url": "http://partner-sim:8004/v1/hooks/acme-x", "event_types": ["<script>alert(1)</script>"]},
    )
    assert r.status_code == 422


# ------------------------------------------------------------------ gateway
@pytest.mark.parametrize(
    "path",
    [
        "/metrics/../v1/orders",
        "/v1/orders/../../metrics",
        "/v1/orders/..%2f..%2fmetrics",
        "/v1/orders/%2e%2e/%2e%2e/readyz",
        "/v1//orders",
        "/v1/unknown",
        "/internal",
        "/v1/sim/payments/x/refund",
        "/v1/payments",
        "/docs/..%5c..%5cmetrics",
        "/v1/orders/x/y/z",
        "/v1/auth/token/extra",
    ],
)
def test_gateway_forwards_only_known_routes(http, auth, path):
    # http.client sends the path exactly as written; httpx would tidy "../" away before sending.
    import http.client as hc
    from urllib.parse import urlsplit

    u = urlsplit(str(http.base_url))
    conn = hc.HTTPConnection(u.hostname, u.port, timeout=10)
    try:
        conn.request("GET", path, headers=auth["acme-admin"])
        status = conn.getresponse().status
    finally:
        conn.close()
    assert status in (404, 405), f"{path} -> {status}"


def test_provider_endpoints_are_not_exposed_through_the_gateway(http, auth):
    r = http.post(
        "/v1/sim/payments",
        headers={**auth["acme-admin"], "Idempotency-Key": "x", "X-Provider-Key": "dev-provider-key-change-me"},
        json={"order_id": "o", "tenant_id": "t", "amount_cents": 1},
    )
    assert r.status_code == 404


def test_host_header_tricks_do_not_change_the_upstream(http, auth):
    for h in (
        {"Host": "evil.example"},
        {"X-Forwarded-Host": "evil.example"},
        {"X-Original-URL": "/metrics"},
        {"X-Rewrite-URL": "/metrics"},
    ):
        r = http.get("/v1/orders?limit=1", headers={**auth["acme-admin"], **h})
        assert r.status_code == 200 and "items" in r.json()


def test_cors_allows_only_configured_origins(http):
    ok = http.options("/v1/orders", headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "GET"})
    bad = http.options("/v1/orders", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"})
    assert ok.headers.get("access-control-allow-origin") == "http://localhost:5173"
    assert "access-control-allow-origin" not in bad.headers


def test_security_headers_and_no_server_banner(http, auth):
    r = http.get("/v1/orders?limit=1", headers=auth["acme-admin"])
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["referrer-policy"] == "no-referrer"
    assert r.headers.get("x-request-id") and "uvicorn" not in r.headers.get("server", "").lower()


def test_request_id_is_sanitised(http, auth):
    evil = 'x"><script>alert(1)</script>'
    r = http.get("/v1/orders/not-a-uuid", headers={**auth["acme-admin"], "X-Request-ID": evil})
    assert r.headers["x-request-id"] != evil and "<script>" not in r.text
    r = http.get("/v1/orders?limit=1", headers={**auth["acme-admin"], "X-Request-ID": "trace-abc.123"})
    assert r.headers["x-request-id"] == "trace-abc.123"


def test_errors_never_leak_internals(http, auth, key):
    probes = [
        http.get("/v1/orders/not-a-uuid", headers=auth["acme-admin"]),
        http.get("/v1/orders?limit=abc", headers=auth["acme-admin"]),
        http.post("/v1/orders", content=b"{bad json", headers={**auth["acme-admin"], **key, "Content-Type": "application/json"}),
        http.post("/v1/auth/token", content=b"\xff\xfe", headers={"Content-Type": "application/json"}),
        http.get("/v1/inventory/" + "A" * 64 + "/availability", headers=auth["acme-admin"]),
    ]
    for r in probes:
        assert 400 <= r.status_code < 500, r.status_code
        low = r.text.lower()
        for leak in ("traceback", "asyncpg", "select ", "insert ", 'file "', "/app/", "pydantic", "postgresql://"):
            assert leak not in low, f"{leak!r} leaked in {r.text[:200]}"


def test_no_api_docs_leak_secrets_and_metrics_are_not_public_via_gateway(http):
    for path in ("/v1/metrics", "/v1/orders/metrics/x"):
        assert http.get(path).status_code in (401, 404)


# ------------------------------------------------------------------ rate limit (last: it drains a bucket)
def test_rate_limit_is_enforced_and_per_tenant(http, auth):
    """Every authenticated request spends a token from the tenant's bucket (the header shows it),
    and the bucket itself, run against the live Redis with a small size, refuses the request
    that exceeds it without touching another tenant's bucket."""
    import asyncio

    import redis.asyncio as aioredis
    from swiftflow_common.ratelimit import RateLimiter

    remaining = int(http.get("/v1/warehouses", headers=auth["globex-admin"]).headers["ratelimit-remaining"])
    assert 0 <= remaining < 1000  # a token was spent from this tenant's bucket

    async def drain() -> tuple[list[bool], float, bool]:
        r = aioredis.from_url("redis://127.0.0.1:6379/0", decode_responses=True)
        try:
            limiter = RateLimiter(r, rate=0.01, burst=5, prefix=f"rl-test-{uuid.uuid4().hex[:8]}")
            results = [await limiter.check("tenant:a") for _ in range(8)]
            other = await limiter.check("tenant:b")
            assert all(x is not None for x in results), "the limiter failed open: its Redis script did not run"
            return [x.allowed for x in results], results[-1].retry_after, other.allowed
        finally:
            await r.aclose()

    allowed, retry_after, other_allowed = asyncio.run(drain())
    assert allowed == [True] * 5 + [False] * 3
    assert retry_after > 1
    assert other_allowed  # another tenant is unaffected
