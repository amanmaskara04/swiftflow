"""Integration tests against the real Postgres and Kafka of the running stack.

pytest tests/integration -q
"""

import asyncio
import json
import sys
import uuid
from pathlib import Path

import asyncpg
import pytest
from aiokafka import AIOKafkaConsumer, AIOKafkaProducer

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from admin import load_env

ENV = load_env()
HOST = "localhost"
KAFKA = "localhost:9092"


def dsn(role: str, pw_key: str) -> str:
    return f"postgresql://{role}:{ENV[pw_key]}@{HOST}:5432/swiftflow"


ADMIN = dsn("swiftflow", "POSTGRES_PASSWORD")
ORDER = dsn("order_svc", "ORDER_DB_PASSWORD")
INVENTORY = dsn("inventory_svc", "INVENTORY_DB_PASSWORD")
NOTIFICATION = dsn("notification_svc", "NOTIFICATION_DB_PASSWORD")


async def tenant_ids():
    c = await asyncpg.connect(ADMIN)
    try:
        rows = await c.fetch("SELECT slug, id FROM orders.tenants")
    finally:
        await c.close()
    return {r["slug"]: str(r["id"]) for r in rows}


# ------------------------------------------------------------------ database isolation
@pytest.mark.parametrize(
    "role_dsn,own,others",
    [
        (ORDER, "orders.orders", ["inventory.stock_levels", "notifications.webhook_subscriptions"]),
        (INVENTORY, "inventory.stock_levels", ["orders.api_clients", "orders.signing_keys", "notifications.event_log"]),
        (NOTIFICATION, "notifications.event_log", ["orders.api_clients", "orders.signing_keys", "inventory.stock_levels"]),
    ],
)
async def test_a_service_role_cannot_read_another_services_schema(http, role_dsn, own, others):
    c = await asyncpg.connect(role_dsn)
    try:
        for table in others:
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await c.fetch(f"SELECT * FROM {table} LIMIT 1")
        flags = await c.fetchrow(
            "SELECT rolsuper, rolbypassrls, rolcreaterole, rolcreatedb FROM pg_roles WHERE rolname = current_user"
        )
        assert not any(flags.values())
    finally:
        await c.close()


@pytest.mark.parametrize("role_dsn,table", [(ORDER, "orders"), (INVENTORY, "stock_levels"), (NOTIFICATION, "event_log")])
async def test_row_level_security_fails_closed_and_isolates_tenants(http, role_dsn, table):
    t = await tenant_ids()
    c = await asyncpg.connect(role_dsn)
    try:
        # 1. a hand-written query with no tenant set is an error, not an empty or full result
        with pytest.raises(asyncpg.PostgresError):
            await c.fetch(f"SELECT * FROM {table} LIMIT 1")
        # 2. inside a tenant's transaction only that tenant's rows exist
        for slug, other in (("acme", "globex"), ("globex", "acme")):
            async with c.transaction():
                await c.execute("SELECT set_config('app.tenant_id', $1, true)", t[slug])
                seen = {str(r["tenant_id"]) for r in await c.fetch(f"SELECT DISTINCT tenant_id FROM {table}")}
                assert seen <= {t[slug]}, f"{slug} session saw {seen}"
                # 3. and it cannot write a row for another tenant, even by naming it
                if table == "orders":
                    with pytest.raises(asyncpg.InsufficientPrivilegeError):
                        async with c.transaction():
                            await c.execute(
                                "INSERT INTO orders (id, tenant_id, status, idempotency_key, request_hash) VALUES ($1, $2, 'PENDING', $3, 'x')",
                                uuid.uuid4(),
                                t[other],
                                f"rls-{uuid.uuid4()}",
                            )
                    assert (
                        await c.fetchval("UPDATE orders SET customer_ref = 'hacked' WHERE tenant_id = $1 RETURNING 1", t[other])
                        is None
                    )
        # 4. the setting does not survive the transaction (pooled connections stay clean)
        with pytest.raises(asyncpg.PostgresError):
            await c.fetch(f"SELECT * FROM {table} LIMIT 1")
    finally:
        await c.close()


async def test_check_constraint_refuses_an_oversold_row(http):
    """The last line of defence: even a buggy UPDATE cannot make reserved exceed on hand."""
    c = await asyncpg.connect(ADMIN)
    try:
        row = await c.fetchrow("SELECT id, on_hand FROM inventory.stock_levels LIMIT 1")
        with pytest.raises(asyncpg.CheckViolationError):
            await c.execute("UPDATE inventory.stock_levels SET reserved = $2 WHERE id = $1", row["id"], row["on_hand"] + 1)
    finally:
        await c.close()


async def test_secrets_are_stored_hashed(http):
    c = await asyncpg.connect(ADMIN)
    try:
        hashes = [r["secret_hash"] for r in await c.fetch("SELECT secret_hash FROM orders.api_clients")]
    finally:
        await c.close()
    assert hashes and all(h.startswith("$argon2id$") for h in hashes)
    assert not any("secret" in h for h in hashes)


# ------------------------------------------------------------------ messaging
def envelope(event_type, tenant, order_id, payload, event_id=None):
    return {
        "event_id": event_id or str(uuid.uuid4()),
        "event_type": event_type,
        "schema_version": 1,
        "occurred_at": "2026-11-14T10:15:00.000Z",
        "tenant_id": tenant,
        "aggregate_id": order_id,
        "producer": "order",
        "payload": {"order_id": order_id, **payload},
    }


async def wait_for(fn, timeout=20.0):
    end = asyncio.get_running_loop().time() + timeout
    while True:
        v = await fn()
        if v or asyncio.get_running_loop().time() > end:
            return v
        await asyncio.sleep(0.25)


async def test_redelivered_event_reserves_once(http):
    """The same OrderPlaced delivered three times must create one reservation (inbox dedupe)."""
    t = (await tenant_ids())["acme"]
    order_id = str(uuid.uuid4())
    placed = envelope("OrderPlaced", t, order_id, {"customer_ref": None, "items": [{"sku": "GLOVE-LINER", "qty": 2}]})
    admin = await asyncpg.connect(ADMIN)
    producer = AIOKafkaProducer(bootstrap_servers=KAFKA)
    await producer.start()
    try:
        before = await admin.fetchval(
            "SELECT s.reserved FROM inventory.stock_levels s JOIN inventory.products p ON p.id = s.product_id WHERE p.sku = 'GLOVE-LINER'"
        )
        for _ in range(3):
            await producer.send_and_wait("orders.events", json.dumps(placed).encode(), key=order_id.encode())

        async def reserved():
            return await admin.fetchval("SELECT count(*) FROM inventory.reservations WHERE order_id = $1", uuid.UUID(order_id))

        assert await wait_for(reserved) == 1
        await asyncio.sleep(1.5)  # give the duplicates time to be (wrongly) processed
        assert await reserved() == 1
        after = await admin.fetchval(
            "SELECT s.reserved FROM inventory.stock_levels s JOIN inventory.products p ON p.id = s.product_id WHERE p.sku = 'GLOVE-LINER'"
        )
        assert after - before == 2
        assert (
            await admin.fetchval(
                "SELECT count(*) FROM inventory.processed_events WHERE event_id = $1", uuid.UUID(placed["event_id"])
            )
            == 1
        )
    finally:  # release the stock again so the invariants stay clean
        cancel = envelope("OrderCancelled", t, order_id, {"reason": "customer_request", "previous_status": "PENDING"})
        await producer.send_and_wait("orders.events", json.dumps(cancel).encode(), key=order_id.encode())

        async def released():
            return await admin.fetchval(
                "SELECT count(*) FROM inventory.reservations WHERE order_id = $1 AND status = 'RELEASED'", uuid.UUID(order_id)
            )

        assert await wait_for(released) == 1
        await producer.stop()
        await admin.close()


async def test_poison_message_goes_to_the_dlq_and_the_partition_keeps_moving(http, auth):
    marker = f"poison-{uuid.uuid4()}"
    key = str(uuid.uuid4()).encode()
    producer = AIOKafkaProducer(bootstrap_servers=KAFKA)
    dlq = AIOKafkaConsumer("orders.events.dlq", bootstrap_servers=KAFKA, auto_offset_reset="latest", enable_auto_commit=False)
    await producer.start()
    await dlq.start()
    try:
        await dlq.seek_to_end()
        for tp in dlq.assignment():
            await dlq.position(tp)
        await producer.send_and_wait("orders.events", json.dumps({"not": "an envelope", "marker": marker}).encode(), key=key)
        await producer.send_and_wait("orders.events", b"\xff\xfe not json " + marker.encode(), key=key)
        found: dict[str, dict] = {}
        end = asyncio.get_running_loop().time() + 25
        while len(found) < 2 and asyncio.get_running_loop().time() < end:
            for _tp, msgs in (await dlq.getmany(timeout_ms=1000)).items():
                for m in msgs:
                    if marker.encode() in m.value:
                        headers = {k: v.decode() for k, v in m.headers}
                        found[headers["consumer"]] = headers
        assert {"inventory-reservations", "notification-feed"} <= set(found), f"dead letters from: {set(found)}"
        for h in found.values():
            assert h["original_topic"] == "orders.events" and h["error"] and "attempts" in h
    finally:
        await dlq.stop()
        await producer.stop()
    # the consumers were not stopped by the poison: a real order still completes
    r = http.post(
        "/v1/orders",
        json={"items": [{"sku": "MUG-ENAMEL", "qty": 1}]},
        headers={**auth["acme-admin"], "Idempotency-Key": f"after-poison-{uuid.uuid4()}"},
    )
    oid = r.json()["id"]

    async def confirmed():
        return http.get(f"/v1/orders/{oid}", headers=auth["acme-admin"]).json()["status"] == "CONFIRMED"

    assert await wait_for(confirmed)


async def test_outbox_rows_are_all_published(http):
    c = await asyncpg.connect(ADMIN)
    try:

        async def drained():
            n = await c.fetchval(
                "SELECT (SELECT count(*) FROM orders.outbox_events WHERE published_at IS NULL) "
                "+ (SELECT count(*) FROM inventory.outbox_events WHERE published_at IS NULL)"
            )
            return n == 0

        assert await wait_for(drained, timeout=30)
        assert await c.fetchval("SELECT count(*) FROM orders.outbox_events WHERE published_at IS NOT NULL") > 0
    finally:
        await c.close()


def test_repeated_query_parameters_all_reach_the_service(http, auth):
    # The gateway once forwarded only the last of a repeated key, so a two-status filter returned one status.
    def page(qs):
        r = http.get("/v1/orders?limit=100&" + qs, headers=auth["acme-admin"])
        assert r.status_code == 200
        return r.json()["items"]

    singles = {s: page("status=" + s) for s in ("CONFIRMED", "SHIPPED", "CANCELLED", "REJECTED")}
    a, b = sorted((s for s in singles if singles[s]), key=lambda s: len(singles[s]))[:2] or (None, None)
    if not b:
        pytest.skip("needs orders in at least two statuses")
    merged = sorted(singles[a] + singles[b], key=lambda o: (o["created_at"], o["id"]), reverse=True)[:100]
    want = [o["id"] for o in merged]
    assert {o["status"] for o in merged} == {a, b}
    assert [o["id"] for o in page(f"status={a}&status={b}")] == want
    assert [o["id"] for o in page(f"status={b}&status={a}")] == want
