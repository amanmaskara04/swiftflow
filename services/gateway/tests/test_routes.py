import pytest
from app.main import resolve, settings


@pytest.mark.parametrize("method,path,upstream", [
    ("POST", "/v1/auth/token", settings.order_url),
    ("GET", "/.well-known/jwks.json", settings.order_url),
    ("GET", "/v1/orders", settings.order_url),
    ("GET", "/v1/orders/stats", settings.order_url),
    ("POST", "/v1/orders/0b6e5f4a-3c2d-4b1a-9e8f-7d6c5b4a3f2e/cancel", settings.order_url),
    ("POST", "/v1/products", settings.inventory_url),
    ("GET", "/v1/inventory/TEE-CLASSIC-M/availability", settings.inventory_url),
    ("GET", "/v1/events/stream", settings.notification_url),
    ("POST", "/v1/webhooks/deliveries/0b6e5f4a-3c2d-4b1a-9e8f-7d6c5b4a3f2e/retry", settings.notification_url),
])
def test_known_routes(method, path, upstream):
    assert resolve(method, path) == (upstream, path)


def test_sim_prefix_is_stripped_and_limited_to_control_endpoints():
    assert resolve("PUT", "/v1/sim/config") == (settings.partner_sim_url, "/v1/config")
    assert resolve("GET", "/v1/sim/hooks/demo") == (settings.partner_sim_url, "/v1/hooks/demo")
    # provider endpoints are never reachable through the gateway
    assert resolve("POST", "/v1/sim/payments") is None
    assert resolve("POST", "/v1/sim/hooks/demo") is None
    assert resolve("POST", "/v1/sim/payments/pay_1/refund") is None


@pytest.mark.parametrize("method,path", [
    ("GET", "/metrics-of-order"), ("GET", "/v1/orders/../../metrics"), ("GET", "/v1/orders/%2e%2e/metrics"),
    ("GET", "/v1//orders"), ("GET", "/v1/orders\\..\\x"), ("GET", "/v1/unknown"), ("GET", "/v1/orders/a/b/c"),
    ("GET", "/readyz/x"), ("DELETE", "/v1/auth/token"), ("POST", "/v1/events"), ("GET", "/v1/payments"),
    ("GET", "/docs/../v1/orders"), ("GET", "/v1/orders/@evil.example"), ("GET", "/v1/orders/x?y"),
])
def test_everything_else_is_refused(method, path):
    assert resolve(method, path) is None


def test_sim_routes_disappear_outside_dev(monkeypatch):
    monkeypatch.setattr(settings, "environment", "production")
    assert resolve("GET", "/v1/sim/config") is None
    assert resolve("GET", "/v1/orders") is not None
