"""Shared fixtures for tests that run against the live stack (default http://localhost:8080)."""

import os
import uuid

import httpx
import pytest

GATEWAY = os.environ.get("GATEWAY_URL", "http://localhost:8080")


@pytest.fixture(scope="session")
def http():
    with httpx.Client(base_url=GATEWAY, timeout=20, trust_env=False) as c:
        try:
            c.get("/healthz").raise_for_status()
        except httpx.HTTPError:
            pytest.skip(f"stack is not running at {GATEWAY}")
        yield c


def _token(http, client_id):
    r = http.post("/v1/auth/token", json={"client_id": client_id, "client_secret": client_id + "-secret"})
    r.raise_for_status()
    return r.json()["access_token"]


@pytest.fixture(scope="session")
def tokens(http):
    return {c: _token(http, c) for c in ("acme-admin", "acme-partner", "globex-admin")}


@pytest.fixture(scope="session")
def auth(tokens):
    return {k: {"Authorization": f"Bearer {v}"} for k, v in tokens.items()}


@pytest.fixture()
def key():
    return {"Idempotency-Key": f"test-{uuid.uuid4()}"}
