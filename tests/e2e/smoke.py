"""End-to-end smoke test through the gateway. Run: python tests/e2e/smoke.py"""

import asyncio
import os
import sys
import time
import uuid

import httpx

GW = os.environ.get("GATEWAY_URL", "http://localhost:8080")
results = []


def check(name, cond, detail=""):
    results.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail else ""))


async def token(c, cid, secret):
    r = await c.post("/v1/auth/token", json={"client_id": cid, "client_secret": secret})
    r.raise_for_status()
    return {"Authorization": "Bearer " + r.json()["access_token"]}


async def wait(c, h, oid, want, timeout=20, field="status"):
    end = time.time() + timeout
    while time.time() < end:
        o = (await c.get(f"/v1/orders/{oid}", headers=h)).json()
        if o.get(field) in want:
            return o
        await asyncio.sleep(0.2)
    return o


async def place(c, h, items, key=None):
    return await c.post(
        "/v1/orders", json={"customer_ref": "smoke", "items": items}, headers={**h, "Idempotency-Key": key or str(uuid.uuid4())}
    )


async def avail(c, h, sku):
    return (await c.get(f"/v1/inventory/{sku}/availability", headers=h)).json()


async def stock_row(c, h, sku):
    g = (await c.get("/v1/inventory/stock", headers=h)).json()
    return next(r for r in g["rows"] if r["sku"] == sku)


async def main():
    async with httpx.AsyncClient(base_url=GW, timeout=30, trust_env=False) as c:
        acme = await token(c, "acme-admin", "acme-admin-secret")
        globex = await token(c, "globex-admin", "globex-admin-secret")
        await c.put("/v1/sim/config", json={"failure_rate": 0, "latency_ms": 20, "outage": False}, headers=acme)

        # webhook endpoint -> simulator receiver (own inbox per run; endpoints from earlier runs are removed)
        inbox = "smoke-" + uuid.uuid4().hex[:8]
        for old in (await c.get("/v1/webhooks/subscriptions", headers=acme)).json()["items"]:
            if "/acme-smoke" in old["url"]:
                await c.delete(f"/v1/webhooks/subscriptions/{old['id']}", headers=acme)
        info = (await c.get("/v1/system/info")).json()
        sub = await c.post(
            "/v1/webhooks/subscriptions",
            headers=acme,
            json={"url": info["webhook_test_receiver_url"] + "/acme-" + inbox, "event_types": ["OrderConfirmed"]},
        )
        check("create webhook endpoint", sub.status_code == 201, sub.text[:120])
        await c.put(f"/v1/sim/hooks/{inbox}/secret", json={"secret": sub.json()["signing_secret"]}, headers=acme)

        # happy path
        before = await stock_row(c, acme, "MUG-ENAMEL")
        key = str(uuid.uuid4())
        r = await place(c, acme, [{"sku": "MUG-ENAMEL", "qty": 2}], key)
        check("place order -> 202 PENDING", r.status_code == 202 and r.json()["status"] == "PENDING", r.text[:150])
        oid = r.json()["id"]
        o = await wait(c, acme, oid, {"CONFIRMED"})
        check("order reaches CONFIRMED", o["status"] == "CONFIRMED", str([h["to_status"] for h in o.get("history", [])]))
        check("total computed from inventory prices", o["total_cents"] == 2200, str(o["total_cents"]))

        # idempotency
        r2 = await place(c, acme, [{"sku": "MUG-ENAMEL", "qty": 2}], key)
        check(
            "same key + same body -> same order", r2.json().get("id") == oid and r2.headers.get("idempotent-replayed") == "true"
        )
        r3 = await place(c, acme, [{"sku": "MUG-ENAMEL", "qty": 3}], key)
        check("same key + different body -> 422", r3.status_code == 422, r3.json().get("type", ""))

        # ship
        r = await c.post(f"/v1/orders/{oid}/ship", headers=acme)
        o = await wait(c, acme, oid, {"SHIPPED"})
        await asyncio.sleep(1.5)
        after = await stock_row(c, acme, "MUG-ENAMEL")
        check(
            "ship -> SHIPPED and on_hand drops by 2",
            o["status"] == "SHIPPED" and before["total_on_hand"] - after["total_on_hand"] == 2,
            f"{before['total_on_hand']} -> {after['total_on_hand']}, reserved {after['total_reserved']}",
        )

        # tenant isolation
        r = await c.get(f"/v1/orders/{oid}", headers=globex)
        check("other tenant gets 404 for this order", r.status_code == 404)
        r = await c.post(f"/v1/orders/{oid}/cancel", headers=globex)
        check("other tenant cannot cancel it (404)", r.status_code == 404)

        # webhook delivered + signature valid
        hooks = []
        for _ in range(40):
            hooks = (await c.get(f"/v1/sim/hooks/{inbox}", headers=acme)).json()["items"]
            if hooks:
                break
            await asyncio.sleep(0.25)
        check(
            "webhook delivered with valid signature",
            bool(hooks) and hooks[0]["signature_valid"] is True,
            hooks[0]["signature_reason"] if hooks else "none received",
        )

        # payment declined -> compensation
        a0 = await avail(c, acme, "CAP-TRAIL")
        await asyncio.sleep(5.5)
        a0 = await avail(c, acme, "CAP-TRAIL")
        await c.put("/v1/sim/config", json={"failure_rate": 1}, headers=acme)
        r = await place(c, acme, [{"sku": "CAP-TRAIL", "qty": 3}])
        o = await wait(c, acme, r.json()["id"], {"CANCELLED"})
        await c.put("/v1/sim/config", json={"failure_rate": 0}, headers=acme)
        await asyncio.sleep(1.5)
        a1 = await stock_row(c, acme, "CAP-TRAIL")
        check(
            "payment declined -> CANCELLED (payment_declined)",
            o["status"] == "CANCELLED" and o["failure_reason"] == "payment_declined",
        )
        check(
            "stock released after decline",
            a1["total_available"] == a0["total_available"],
            f"{a0['total_available']} -> {a1['total_available']}",
        )

        # out of stock
        r = await place(c, acme, [{"sku": "TOWEL-QUICK", "qty": 999}])
        o = await wait(c, acme, r.json()["id"], {"REJECTED"})
        check("too many units -> REJECTED", o["status"] == "REJECTED", str(o.get("failure_reason")))

        # cancel a confirmed order -> refund
        r = await place(c, acme, [{"sku": "BOTTLE-750", "qty": 1}])
        o = await wait(c, acme, r.json()["id"], {"CONFIRMED"})
        r = await c.post(f"/v1/orders/{o['id']}/cancel", headers=acme)
        o = await wait(c, acme, o["id"], {"REFUNDED"}, field="payment_status")
        check(
            "cancel CONFIRMED -> CANCELLED + REFUNDED",
            o["status"] == "CANCELLED" and o["payment_status"] == "REFUNDED",
            o["payment_status"],
        )

        # auth
        r = await c.get("/v1/orders")
        check("no token -> 401", r.status_code == 401)
        partner = await token(c, "acme-partner", "acme-partner-secret")
        r = await c.post(f"/v1/orders/{oid}/ship", headers=partner)
        check("partner cannot ship -> 403", r.status_code == 403)
        r = await c.put("/v1/sim/config", json={"outage": True}, headers=partner)
        check("partner cannot inject faults -> 403", r.status_code == 403)

        # concurrency: 200 orders vs 50 units (on top of whatever is already reserved for unshipped orders)
        held = (await stock_row(c, acme, "LOAD-TEST-1"))["cells"][0]["reserved"]
        r = await c.post(
            "/v1/inventory/adjustments",
            headers={**acme, "Idempotency-Key": str(uuid.uuid4())},
            json={"sku": "LOAD-TEST-1", "warehouse_code": "BLR-1", "mode": "set", "quantity": held + 50, "reason": "smoke"},
        )
        sem = asyncio.Semaphore(100)

        async def one():
            # A small host can shed a request under this burst (429, or 502/504 from the proxy). Retrying with
            # the same idempotency key is safe: an order that was created the first time is returned, not doubled.
            key = str(uuid.uuid4())
            async with sem:
                for _ in range(5):
                    r = await place(c, acme, [{"sku": "LOAD-TEST-1", "qty": 1}], key)
                    if r.status_code not in (429, 502, 503, 504):
                        return r.json().get("id")
                    await asyncio.sleep(float(r.headers.get("retry-after", "1")))
                return None

        t0 = time.time()
        ids = await asyncio.gather(*(one() for _ in range(200)))
        placed = time.time() - t0
        final = {}
        end = time.time() + 90
        pending = set(i for i in ids if i)
        while pending and time.time() < end:
            for i in list(pending)[:50]:
                o = (await c.get(f"/v1/orders/{i}", headers=acme)).json()
                if o["status"] in ("CONFIRMED", "REJECTED", "CANCELLED"):
                    final[i] = o["status"]
                    pending.discard(i)
            await asyncio.sleep(0.3)
        n_conf = sum(1 for s in final.values() if s == "CONFIRMED")
        n_rej = sum(1 for s in final.values() if s == "REJECTED")
        await asyncio.sleep(1)
        row = await stock_row(c, acme, "LOAD-TEST-1")
        cell = row["cells"][0]
        want = 50
        check(
            "200 concurrent orders vs 50 units: exactly 50 confirmed",
            n_conf == want and n_rej == 200 - want and not pending,
            f"confirmed {n_conf}, rejected {n_rej}, unsettled {len(pending)}, placed in {placed:.2f}s, settled in {time.time() - t0:.2f}s",
        )
        check("reserved grew by exactly 50, nothing oversold", cell["reserved"] == held + 50 and cell["on_hand"] == held + 50, str(cell))
        pay = (await c.get("/v1/sim/payments", headers=acme)).json()
        check("no double charges", pay["double_charges"] == 0, f"approved {pay['approved']}")
        h = (await c.get("/v1/system/health", headers=acme)).json()
        check(
            "all nodes up", h["status"] == "up", ", ".join(f"{n['id']}={n['status']}" for n in h["nodes"] if n["status"] != "up")
        )
    print(f"\n{sum(results)}/{len(results)} checks passed")
    sys.exit(0 if all(results) else 1)


asyncio.run(main())
