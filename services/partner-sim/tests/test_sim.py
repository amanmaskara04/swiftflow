from app.main import app, settings
from fastapi.testclient import TestClient

H = {"X-Provider-Key": settings.provider_api_key}
BODY = {"order_id": "o1", "tenant_id": "t1", "amount_cents": 500}


def test_provider_rules():
    with TestClient(app) as c:
        assert c.post("/v1/payments", json=BODY, headers={"Idempotency-Key": "k"}).status_code == 401  # no provider key
        assert c.post("/v1/payments", json=BODY, headers=H).status_code == 422  # no idempotency key
        a = c.post("/v1/payments", json=BODY, headers={**H, "Idempotency-Key": "k1"}).json()
        b = c.post("/v1/payments", json=BODY, headers={**H, "Idempotency-Key": "k1"}).json()
        assert a["status"] == "approved" and b["payment_id"] == a["payment_id"] and b["replayed"] is True
        r = c.post(f"/v1/payments/{a['payment_id']}/refund", json=BODY, headers={**H, "Idempotency-Key": "r1"}).json()
        assert r["status"] == "refunded"
        # void before the charge arrives: the late charge must not take money
        v = c.post("/v1/payments/k2/refund", json=BODY, headers={**H, "Idempotency-Key": "r2"}).json()
        late = c.post("/v1/payments", json=BODY, headers={**H, "Idempotency-Key": "k2"}).json()
        assert v["status"] == "voided" and late["status"] == "voided"
        # control endpoints need a token
        assert c.get("/v1/config").status_code == 401
        assert c.put("/v1/config", json={"outage": True}).status_code == 401
        assert c.get("/v1/payments").status_code == 401
        assert c.get("/v1/hooks/demo").status_code == 401


def test_webhook_receiver_checks_signatures():
    from swiftflow_common.signing import sign

    with TestClient(app) as c:
        app.state.sim.secrets["acme-x"] = "whsec_test_secret"
        body = b'{"event_type":"OrderConfirmed"}'
        ok = c.post("/v1/hooks/acme-x", content=body, headers={"SwiftFlow-Signature": sign("whsec_test_secret", body)})
        bad = c.post("/v1/hooks/acme-x", content=body + b" ", headers={"SwiftFlow-Signature": sign("whsec_test_secret", body)})
        old = c.post("/v1/hooks/acme-x", content=body, headers={"SwiftFlow-Signature": sign("whsec_test_secret", body, timestamp=1)})
        assert ok.status_code == bad.status_code == old.status_code == 202
        got = list(app.state.sim.inboxes["acme-x"])  # newest first
        assert [g["signature_valid"] for g in got] == [False, False, True]
        assert [g["signature_reason"] for g in got] == ["stale_timestamp", "signature_mismatch", "ok"]
        assert c.post("/v1/hooks/Bad_Name!", content=b"{}").status_code == 422
