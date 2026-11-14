"""The oversell proof: N concurrent one-unit orders against M units must confirm exactly min(N, M).

    python loadtest/oversell.py [--units 100] [--orders 500] [--concurrency 200] [--sku LOAD-TEST-1] [--no-report]

Writes docs/results/oversell-<timestamp>.md and exits non-zero on failure.
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent


def pct(values: list[float], p: float) -> float:
    s = sorted(values)
    return s[min(len(s) - 1, int(len(s) * p / 100))] if s else 0.0


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gateway", default="http://localhost:8080")
    ap.add_argument("--sku", default="LOAD-TEST-1")
    ap.add_argument("--units", type=int, default=100)
    ap.add_argument("--orders", type=int, default=500)
    ap.add_argument("--concurrency", type=int, default=200)
    ap.add_argument("--timeout", type=float, default=180)
    ap.add_argument("--no-report", action="store_true")
    ap.add_argument("--no-reset", action="store_true", help="keep the simulator's fault settings (used by the chaos run)")
    a = ap.parse_args()

    limits = httpx.Limits(max_connections=a.concurrency + 20)
    async with httpx.AsyncClient(base_url=a.gateway, timeout=60, trust_env=False, limits=limits) as c:
        tok = (await c.post("/v1/auth/token", json={"client_id": "acme-admin", "client_secret": "acme-admin-secret"})).json()[
            "access_token"
        ]
        h = {"Authorization": f"Bearer {tok}"}

        async def cell():
            rows = (await c.get("/v1/inventory/stock", headers=h)).json()["rows"]
            row = next(r for r in rows if r["sku"] == a.sku)
            return row["cells"][0]

        if not a.no_reset:
            await c.put("/v1/sim/config", json={"failure_rate": 0, "outage": False, "latency_ms": 20}, headers=h)
        before = await cell()
        r = await c.post(
            "/v1/inventory/adjustments",
            headers={**h, "Idempotency-Key": str(uuid.uuid4())},
            json={
                "sku": a.sku,
                "warehouse_code": before["warehouse_code"],
                "mode": "set",
                "quantity": before["reserved"] + a.units,
                "reason": "oversell load test",
            },
        )
        r.raise_for_status()
        reserved_before = before["reserved"]
        pay_before = (await c.get("/v1/sim/payments", headers=h)).json()["approved"]

        sem = asyncio.Semaphore(a.concurrency)
        lat: list[float] = []
        ids: list[str] = []
        errors: dict[int, int] = {}

        async def place() -> None:
            async with sem:
                for _ in range(6):
                    t = time.perf_counter()
                    resp = await c.post(
                        "/v1/orders",
                        headers={**h, "Idempotency-Key": f"load-{uuid.uuid4()}"},
                        json={"customer_ref": "load-test", "items": [{"sku": a.sku, "qty": 1}]},
                    )
                    if resp.status_code == 202:
                        lat.append((time.perf_counter() - t) * 1000)
                        ids.append(resp.json()["id"])
                        return
                    if resp.status_code == 429:
                        await asyncio.sleep(float(resp.headers.get("retry-after", "1")))
                        continue
                    errors[resp.status_code] = errors.get(resp.status_code, 0) + 1
                    return

        t0 = time.perf_counter()
        await asyncio.gather(*(place() for _ in range(a.orders)))
        placed_s = time.perf_counter() - t0

        final: dict[str, str] = {}
        pending = set(ids)
        deadline = time.perf_counter() + a.timeout
        psem = asyncio.Semaphore(50)

        async def poll(oid: str) -> None:
            async with psem:
                resp = await c.get(f"/v1/orders/{oid}", headers=h)
                if resp.status_code == 200 and resp.json()["status"] in ("CONFIRMED", "REJECTED", "CANCELLED", "SHIPPED"):
                    final[oid] = resp.json()["status"]
                    pending.discard(oid)

        while pending and time.perf_counter() < deadline:
            await asyncio.gather(*(poll(o) for o in list(pending)))
            if pending:
                await asyncio.sleep(0.5)
        settled_s = time.perf_counter() - t0
        await asyncio.sleep(1.5)
        after = await cell()
        pay = (await c.get("/v1/sim/payments", headers=h)).json()

    confirmed = sum(1 for s in final.values() if s == "CONFIRMED")
    rejected = sum(1 for s in final.values() if s == "REJECTED")
    cancelled = sum(1 for s in final.values() if s == "CANCELLED")
    expected = min(a.units, a.orders)
    reserved_by_run = after["reserved"] - reserved_before
    oversold = max(0, after["reserved"] - after["on_hand"]) + max(0, confirmed - a.units)
    checks = [
        (f"every order was accepted ({len(ids)}/{a.orders})", len(ids) == a.orders and not errors),
        (f"every order settled ({len(pending)} unsettled)", not pending),
        *(
            # with faults injected (chaos run) declines free stock for later orders, so only the ceiling is fixed
            [(f"never more than {a.units} confirmed (got {confirmed})", confirmed <= a.units)]
            if a.no_reset
            else [
                (f"exactly {expected} confirmed (got {confirmed})", confirmed == expected),
                (
                    f"the rest rejected (got {rejected} rejected, {cancelled} cancelled)",
                    rejected + cancelled == a.orders - expected,
                ),
            ]
        ),
        (f"stock reserved by this run = confirmed orders (got {reserved_by_run} vs {confirmed})", reserved_by_run == confirmed),
        (f"oversold units = 0 (got {oversold})", oversold == 0),
        (
            f"provider charged one payment per confirmed order (got {pay['approved'] - pay_before} vs {confirmed})",
            a.no_reset or pay["approved"] - pay_before == confirmed,
        ),
        (f"no double charges (got {pay['double_charges']})", pay["double_charges"] == 0),
    ]
    ok = all(p for _, p in checks)
    lines = [
        f"# Oversell load test — {'PASSED' if ok else 'FAILED'}",
        "",
        f"Run at {datetime.now().isoformat(timespec='seconds')} against `{a.gateway}`.",
        "",
        "| Setting | Value |",
        "|---|---|",
        f"| SKU | `{a.sku}` |",
        f"| Units in stock | {a.units} |",
        f"| Orders fired | {a.orders} |",
        f"| Client concurrency | {a.concurrency} |",
        "",
        "| Result | Value |",
        "|---|---|",
        f"| Confirmed | **{confirmed}** |",
        f"| Rejected | {rejected} |",
        f"| Cancelled | {cancelled} |",
        f"| Oversold units | **{oversold}** |",
        f"| Stock after | on hand {after['on_hand']}, reserved {after['reserved']}, available {after['available']} |",
        f"| Time to accept all orders | {placed_s:.2f} s ({len(ids) / placed_s:.0f} orders/s) |",
        f"| Time until every order settled | {settled_s:.2f} s |",
        f"| Accept latency p50 / p95 / p99 | {statistics.median(lat):.0f} / {pct(lat, 95):.0f} / {pct(lat, 99):.0f} ms |"
        if lat
        else "| Accept latency | n/a |",
        "",
        "| Check | |",
        "|---|---|",
        *[f"| {name} | {'pass' if p else '**FAIL**'} |" for name, p in checks],
        "",
    ]
    report = "\n".join(lines)
    print(report)
    if errors:
        print("non-202 responses:", errors)
    if not a.no_report:
        out = ROOT / "docs" / "results" / f"oversell-{datetime.now():%Y%m%d-%H%M%S}.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(report, encoding="utf-8")
        print(f"report: {out}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
