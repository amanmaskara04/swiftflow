"""Quick throughput probe:  python loadtest/bench.py [--gateway URL] [--n 600] [--c 50]"""

import argparse
import asyncio
import time
import uuid

import httpx


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gateway", default="http://localhost:8080")
    ap.add_argument("--n", type=int, default=600)
    ap.add_argument("--c", type=int, default=50)
    a = ap.parse_args()
    async with httpx.AsyncClient(base_url=a.gateway, timeout=60, trust_env=False, limits=httpx.Limits(max_connections=a.c)) as c:
        tok = (await c.post("/v1/auth/token", json={"client_id": "acme-admin", "client_secret": "acme-admin-secret"})).json()[
            "access_token"
        ]
        h = {"Authorization": f"Bearer {tok}"}

        async def bench(name, fn):
            sem = asyncio.Semaphore(a.c)
            lat = []

            async def one():
                async with sem:
                    s = time.perf_counter()
                    r = await fn()
                    lat.append((time.perf_counter() - s) * 1000)
                    return r.status_code

            t = time.perf_counter()
            codes = await asyncio.gather(*(one() for _ in range(a.n)))
            el = time.perf_counter() - t
            lat.sort()
            print(
                f"{name:28s} {a.n / el:7.0f} req/s   p50 {lat[len(lat) // 2]:6.0f} ms  p95 {lat[int(len(lat) * 0.95)]:6.0f} ms   {sorted(set(codes))}"
            )

        await bench("GET /healthz", lambda: c.get("/healthz"))
        await bench("GET /v1/system/info", lambda: c.get("/v1/system/info"))
        await bench("GET /v1/warehouses (auth+db)", lambda: c.get("/v1/warehouses", headers=h))
        await bench("GET /v1/orders?limit=1", lambda: c.get("/v1/orders?limit=1", headers=h))
        await bench(
            "POST /v1/orders",
            lambda: c.post(
                "/v1/orders",
                headers={**h, "Idempotency-Key": str(uuid.uuid4())},
                json={"items": [{"sku": "TOWEL-QUICK", "qty": 999}]},
            ),
        )


asyncio.run(main())
