# Testing

The tests exist to prove the five claims in the [README](../README.md). Each claim has a named test that fails loudly when it breaks.

## Layers

| Layer | Tool | Needs | Location | Covers |
|-------|------|-------|----------|--------|
| Unit | pytest | nothing | `services/*/tests`, `libs/common/tests` | Order state machine (every allowed and forbidden transition), circuit breaker, idempotency hashing, scope checks, webhook signing and verification, SSRF guard, token-bucket maths, warehouse choice, gateway route table |
| Contract | pytest + jsonschema | nothing | `libs/contracts/tests` | Every example event matches its schema; every event a producer builds in tests matches too |
| Integration | pytest + pytest-asyncio | full stack + seed | `tests/integration` | Real Postgres and Kafka: the scenarios below |
| Security | pytest + httpx | full stack + seed | `tests/security` | Attacks through the gateway that must all be refused |
| End to end | a plain script, `tests/e2e/smoke.py` (asyncio + httpx) | full stack + seed | `tests/e2e` | The whole saga over HTTP through the gateway: 21 checks |
| Load | `loadtest/oversell.py` (asyncio + httpx), `loadtest/bench.py` (throughput probe) | full stack | `loadtest/` | No overselling under concurrency; latency |
| Chaos | `chaos/kill_mid_saga.ps1` + invariant checker | full stack in Docker | `chaos/` | Process and broker failures mid-saga |

Counts at the last run: 165 unit and contract tests, 12 integration, 81 security, 21 end-to-end checks. Coverage is not measured.

`.\swiftflow.ps1 test` runs lint, unit and contract tests. `.\swiftflow.ps1 test-int` runs the integration and security suites and then the end-to-end script.

The integration, security and end-to-end tests read the gateway address from `GATEWAY_URL` (default `http://localhost:8080`). The end-to-end script and the load test (`--gateway`) also run against the hosted demo. The integration tests that open database or Kafka connections need those ports, so they only run where the stack is local. Fifteen security tests assume a direct plain-HTTP gateway and fail against the public HTTPS address, where Caddy and nginx sit in front: they send raw paths over port 80 (redirected to HTTPS), a foreign `Host` header (refused by Caddy before it reaches the gateway) and a `localhost` CORS origin (the demo allows only its own origin).

## Integration scenarios

All in `tests/integration/test_integration.py`.

| Test | What it does | Pass condition |
|------|--------------|----------------|
| `test_a_service_role_cannot_read_another_services_schema` | Connects as each service's database role and reads the other schemas | Permission denied every time; the role is not a superuser and cannot bypass RLS |
| `test_row_level_security_fails_closed_and_isolates_tenants` | Queries each tenant table with no tenant set, then as each tenant | No tenant set: the query raises; a tenant session sees only its own rows |
| `test_check_constraint_refuses_an_oversold_row` | Tries to write `reserved > on_hand` directly | The `CHECK` constraint refuses it |
| `test_secrets_are_stored_hashed` | Reads the stored client secrets | Argon2 hashes only |
| `test_redelivered_event_reserves_once` | Delivers the same `OrderPlaced` twice | One reservation; the second delivery is dropped by the inbox |
| `test_poison_message_goes_to_the_dlq_and_the_partition_keeps_moving` | Publishes a malformed message, then a valid one | The malformed one lands on `.dlq`; the valid one is processed |
| `test_outbox_rows_are_all_published` | Reads the order and inventory outboxes after traffic | No row is left unpublished |
| `test_repeated_query_parameters_all_reach_the_service` | Lists orders with `status=A&status=B` in both orders | Both statuses come back (the gateway once forwarded only the last value) |

Concurrent reservation is proven by the end-to-end script (200 orders against 50 units) and the load test rather than by an integration test. Idempotency-key replay is covered by the end-to-end script and the security suite; adjusting stock below the reserved amount is covered by the security suite.

## End-to-end checks

| Scenario | Pass condition |
|----------|----------------|
| Happy path | `202 PENDING`, then `CONFIRMED`; total computed from inventory prices; `SHIPPED` after ship and `on_hand` reduced |
| Idempotency | Same key and body returns the same order; same key with a different body returns `422` |
| Payment declined | `CANCELLED` with `payment_declined`; stock available again |
| Out of stock | `REJECTED` with `insufficient_stock` |
| Cancel after confirmation | `CANCELLED` and the payment `REFUNDED` |
| Webhook | The simulator receives `OrderConfirmed` with a valid signature |
| Auth | No token → 401; partner calling ship or fault injection → 403 |
| Cross-tenant | A Globex token cannot read or cancel an Acme order (404, not 403, so existence is not leaked) |
| Concurrency | 200 concurrent orders against 50 units: exactly 50 confirmed, reserved grows by 50, no double charges |
| Health | Every node reports up |

## Security tests

Kept in `tests/security/` and run with the integration suite. Each one is an attack that must fail.

| Area | Test | Must happen |
|------|------|-------------|
| Tokens | Expired token; token signed with a different key; `alg: none`; `alg: HS256` signed with the public key; wrong `aud` or `iss`; missing `tenant_id` | `401` every time |
| Scopes | Partner token on `ship`, on adjustments, on `/v1/sim/config` | `403` |
| Tenancy | Globex token reads, cancels or ships an Acme order; reads Acme stock, events, deliveries, sim inbox | `404` or empty; never Acme data |
| Tenancy | Body or query containing another `tenant_id` | Ignored; the token's tenant is used |
| Injection | SQL metacharacters in `q`, `sku`, `customer_ref`, cursors | Treated as data; malformed cursor → `422` |
| Idempotency | Key reused with a different body; key reused by another tenant | `422`; the other tenant gets its own independent order |
| Webhooks | Subscription URL of `http://169.254.169.254/`, `http://localhost:5432`, a hostname resolving to a private IP, a non-http scheme | `422` at subscribe time; blocked again at delivery time if DNS changes |
| Webhooks | Tampered body, stale timestamp (> 5 min), wrong secret at the receiver | `signature_valid: false` |
| Limits | Body over 1 MB; 51 items; `qty` of 0 or 10⁹; 300-char idempotency key | `413` / `422` |
| Rate limit | Burst above the bucket | `429` with `Retry-After`; another tenant is unaffected |
| Gateway | Path traversal (`/v1/orders/../../metrics`), unknown prefix, `Host`/`X-Forwarded-Host` tricks | `404`; never proxied to an arbitrary host |
| Errors | Force a 500 | Problem JSON with a request ID; no stack trace or SQL in the body |
| Headers | Any response through the gateway / console | `X-Content-Type-Options: nosniff`, `Referrer-Policy`, `Content-Security-Policy` on the console, CORS limited to configured origins |
| Secrets | Grep the repo and images | No private keys or real secrets committed; client secrets stored only as argon2 hashes |

## Load test: the oversell proof

```powershell
.\swiftflow.ps1 load            # defaults: SKU LOAD-TEST-1, 100 units, 500 orders, concurrency 200
.venv\Scripts\python.exe loadtest\oversell.py --gateway https://<host> --units 500 --orders 2000
```
1. Sets the test SKU to 100 free units in one warehouse and the simulator to 0 % declines.
2. Fires 500 single-unit orders, up to 200 at a time, each with a unique idempotency key.
3. Polls until every order is final.

**Pass:** every order accepted and settled, exactly 100 `CONFIRMED`, 400 `REJECTED`, stock reserved by the run = 100, oversold units = 0, one payment per confirmed order, simulator `double_charges = 0`. Prints throughput, accept-latency p50/p95/p99 and time to settle, and writes `docs/results/oversell-<timestamp>.md`.

`loadtest/bench.py` is a quick throughput probe for five endpoints (`--gateway`, `--n`, `--c`).

### Results on the hosted demo (2026-11-15)

One Azure VM with 2 vCPUs and 4 GB runs the whole stack, tested from a laptop over the internet.

| Run | Result |
|-----|--------|
| 500 orders against 100 units | Passed: 100 confirmed, 400 rejected, 0 oversold; accepted at 29 orders/s, settled in 27 s ([report](results/oversell-20261115-121809.md)) |
| 2,000 orders against 500 units | 500 confirmed, 0 oversold, 0 double charges; 9 requests timed out with 502/504, so the run is marked failed on "every order was accepted" ([report](results/oversell-20261115-122001.md)) |
| Throughput probe, 50 concurrent | About 30 orders/s and about 80 authenticated reads/s, the same when run on the VM itself — the VM's CPU is the limit, not the network |
| Invariant checker afterwards | All checks pass across 15,584 orders |

The same laptop runs the local stack at about 50 orders/s.

## Chaos test

`chaos/kill_mid_saga.ps1` sets the simulator to 10 % declines and 300 ms latency, starts the load test (400 orders against 150 units by default), then six times kills and restarts a random container among `order-worker`, `inventory-worker` and `notification-worker`, and restarts `kafka` once halfway through. When the load run ends it waits 20 s and runs the invariant checker.

## Invariant checker — `chaos/check_invariants.py`

Connects as the superuser (bypassing RLS on purpose) and exits non-zero on any failure. `--grace` (default 120 s) is how old a row must be before it counts as stuck.

1. No stock row has `reserved > on_hand` or a negative value.
2. For every stock row, `reserved` equals the sum of its `ACTIVE` reservations.
3. No order older than the grace period is still `PENDING` or `RESERVED`.
4. a. `CANCELLED` and `REJECTED` orders hold no `ACTIVE` reservation. b. Every `CONFIRMED` order holds one per line. c. Every `SHIPPED` order's reservations are `COMMITTED`.
5. a. No `CANCELLED` order still holds money (`CHARGED` or `REFUND_PENDING`). b. `CONFIRMED` and `SHIPPED` orders are charged exactly once. c. The simulator recorded no double charges.
6. No outbox row is stuck unpublished.
7. Every tenant table has row-level security forced.

It also prints order totals by status.

## CI and deployment

`.github/workflows/ci.yml`, on every push and pull request:

1. **Lint and unit tests:** ruff, then the library, contract and service tests.
2. **Console:** type-check (`tsc`) and the production build.
3. **Full stack:** build every image → Trivy scan of the repository (fails on a fixable `CRITICAL` or `HIGH`) and of the order image (fixable `CRITICAL`) → start the Compose stack → seed → integration and security tests → end-to-end script → oversell load test → invariant checker.
4. **Deploy** (pushes to `main` only, after everything above passes): sign in to Azure with OIDC, update the demo VM to the commit, run the end-to-end script against the public URL. Skipped when the VM is stopped or the one-time `deploy.ps1 cd-setup` has not been run.

Every third-party action is pinned to a full commit SHA.
