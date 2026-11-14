import itertools

import pytest
from app import domain

ALLOWED = {
    ("PENDING", "RESERVED"), ("PENDING", "REJECTED"), ("PENDING", "CANCELLED"),
    ("RESERVED", "CONFIRMED"), ("RESERVED", "CANCELLED"),
    ("CONFIRMED", "SHIPPED"), ("CONFIRMED", "CANCELLED"),
}


@pytest.mark.parametrize("a,b", list(itertools.product(domain.STATUSES, repeat=2)))
def test_every_transition_is_allowed_or_forbidden(a, b):
    assert domain.can_transition(a, b) is ((a, b) in ALLOWED)


def test_terminal_states_go_nowhere():
    for s in domain.TERMINAL:
        assert not any(domain.can_transition(s, t) for t in domain.STATUSES)


def test_cancel_and_ship_rules():
    assert [s for s in domain.STATUSES if domain.can_cancel(s)] == ["PENDING", "RESERVED", "CONFIRMED"]
    assert [s for s in domain.STATUSES if domain.can_ship(s)] == ["CONFIRMED"]


@pytest.mark.parametrize(
    "status,pay,attempts,expected",
    [
        ("CONFIRMED", "CHARGED", 0, "REFUND_PENDING"),  # money taken: give it back
        ("RESERVED", "NONE", 1, "REFUND_PENDING"),  # a charge may be in flight: void it by key
        ("RESERVED", "NONE", 0, "NONE"),  # never tried to charge
        ("PENDING", "NONE", 0, "NONE"),
    ],
)
def test_money_after_cancel(status, pay, attempts, expected):
    assert domain.payment_status_after_cancel(status, pay, attempts) == expected


def test_retry_delay_backs_off_and_caps():
    delays = [domain.retry_delay_seconds(n) for n in range(1, 9)]
    assert delays == sorted(delays) and delays[0] == 1 and max(delays) == 30


def test_issued_token_verifies_and_tampering_fails():
    import asyncio
    import base64
    import json

    from app.keys import KeyStore, generate_pem, public_jwk
    from cryptography.hazmat.primitives import serialization
    from swiftflow_common.auth import TokenVerifier, jwk_to_public_key
    from swiftflow_common.problems import ProblemError

    ks = KeyStore(None, None)  # type: ignore[arg-type]
    ks.signing_key = serialization.load_pem_private_key(generate_pem().encode(), password=None)
    jwk = public_jwk(ks.signing_key.public_key())
    ks.kid = jwk["kid"]
    key = jwk_to_public_key(jwk)

    async def provider(kid, _refresh):
        return key if kid == ks.kid else None

    v = TokenVerifier("iss", "aud", provider)
    args = dict(issuer="iss", audience="aud", client_id="c", tenant_id="6f1d2c3b-4a59-4e6f-8a7b-9c0d1e2f3a4b",
                tenant_slug="acme", role="partner", scopes=["orders:read"])
    tok = ks.issue(ttl=60, **args)
    p = asyncio.run(v.verify(tok))
    assert p.tenant_slug == "acme" and p.has("orders:read") and not p.has("admin")

    head, body, sig = tok.split(".")
    claims = json.loads(base64.urlsafe_b64decode(body + "=="))
    claims["scope"] = "admin"
    forged = ".".join([head, base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("="), sig])
    for bad in (forged, ks.issue(ttl=-60, **args), tok + "x", "not-a-token"):
        with pytest.raises(ProblemError) as e:
            asyncio.run(v.verify(bad))
        assert e.value.status == 401
    with pytest.raises(ProblemError):  # right key, wrong audience
        asyncio.run(TokenVerifier("iss", "other", provider).verify(tok))
