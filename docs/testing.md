# Testing

The tests exist to prove the five claims in the [README](../README.md). Each claim has a named test that fails loudly when it breaks.

## Layers

| Layer | Tool | Needs | Location | Covers |
|-------|------|-------|----------|--------|
| Unit | pytest | nothing | `services/*/tests/unit`, `libs/common/tests` | Order state machine (every allowed and forbidden transition), circuit breaker, idempotency hashing, scope checks, webhook signing and verification, SSRF guard, token-bucket maths, warehouse choice |
| Contract | pytest + jsonschema | nothing | `libs/contracts/tests` | Every example event matches its schema; every event a producer builds in tests matches too |
| Integration | pytest + pytest-asyncio | infra running (`swiftflow.ps1 infra`) | `tests/integration` | Real Postgres, Redis and Kafka: the scenarios below |
| End to end | pytest + httpx | full stack | `tests/e2e` | The whole saga over HTTP through the gateway |
| Load | `loadtest/oversell.py` (asyncio + httpx), `loadtest/bench.py` (throughput probe) | full stack | `loadtest/` | No overselling under concurrency; latency |
| Chaos | `chaos/kill_mid_saga.ps1` + invariant checker | full stack in Docker | `chaos/` | Process and broker failures mid-saga |

Coverage target: 80 % lines on `libs/common` and each service's domain modules.

## Integration scenarios

| Test | Setup | Pass condition |
|------|-------|----------------|
| `test_concurrent_reservation` | 1 SKU, 50 in stock; 200 concurrent 1-unit reservations | Exactly 50 `ACTIVE` reservations; `reserved = 50`; 150 failures |
| `test_redelivered_event` | Deliver the same `OrderPlaced` twice | One reservation; second counted as `duplicate` |
| `test_relay_crash` | 1,000 outbox rows; cancel the relay task halfway; restart | All 1,000 `event_id`s reach Kafka (duplicates allowed, losses not) |
| `test_poison_message` | Publish a malformed message, then a valid one | Malformed lands on `.dlq` with error headers after 3 attempts; valid one processed |
| `test_idempotency_key` | Same key + same body twice; then same key + different body | One order, same response replayed; then `422` |
| `test_tenant_isolation` | Rows for tenants A and B | Tenant A session reads 0 of B's rows; a session with no tenant set raises |
| `test_adjust_below_reserved` | 10 on hand, 8 reserved; set to 5 | `409`; nothing changes |
| `test_multi_item_all_or_nothing` | Item 1 available, item 2 not | No reservation for either item |

## End-to-end scenarios

| Scenario | Pass condition |
|----------|----------------|
| Happy path | `CONFIRMED` within 10 s; `SHIPPED` after ship; stock `on_hand` reduced |
| Payment declined (`failure_rate = 1`) | `CANCELLED` with `payment_declined`; stock available again |
| Out of stock | `REJECTED` with `insufficient_stock` |
| Webhook | Simulator receives `OrderConfirmed` with a valid signature |
| Provider outage | Orders wait in `RESERVED`; circuit opens; after recovery they reach `CONFIRMED` |
| Auth | No token → 401; partner calling ship → 403; burst over limit → 429 |
| Cross-tenant | Globex token cannot `GET` an Acme order (404, not 403, so existence is not leaked) |

## Security tests

Kept in `tests/security/` and run with the e2e suite. Each one is an attack that must fail.

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
.\swiftflow.ps1 load            # defaults: SKU LOAD-TEST-1, 100 units, 500 orders, concurrency 500
```
1. Sets the test SKU to 100 units in one warehouse and the simulator to 0 % declines.
2. Fires 500 single-unit orders at once, each with a unique idempotency key.
3. Polls until every order is final.

**Pass:** exactly 100 `CONFIRMED`, 400 `REJECTED`, `reserved = 100`, oversold units = 0, simulator `double_charges = 0`. Prints throughput, accept-latency p50/p95/p99 and time to settle, and writes `docs/results/oversell-<timestamp>.md`.

## Chaos test

`chaos/kill_mid_saga.ps1` starts the load test, then every 5 s restarts a random container among `order-worker`, `inventory-worker`, `notification-worker`, and once restarts `kafka`. When traffic stops and lag drains, it runs the invariant checker.

## Invariant checker — `chaos/check_invariants.py`

Connects as the superuser (bypassing RLS on purpose) and exits non-zero on any failure:

1. No stock row has `reserved > on_hand` or a negative value.
2. For every stock row, `reserved` equals the sum of its `ACTIVE` reservations.
3. Every order older than 2 minutes is `CONFIRMED`, `SHIPPED`, `REJECTED` or `CANCELLED`.
4. `CANCELLED` and `REJECTED` orders hold no `ACTIVE` reservations; `CONFIRMED` orders hold one per item.
5. The simulator recorded at most one successful charge per order, and every `CANCELLED` order that was charged has exactly one refund (no order is left `REFUND_PENDING` for more than 2 minutes).
6. No outbox row is unpublished for more than 30 s, and every `.dlq` message is listed in the report.

## CI

`.github/workflows/ci.yml`: ruff lint and format check → unit and contract tests → build every image → Trivy scan (fail on fixable `CRITICAL`) → start the Compose stack → seed → e2e tests → oversell load test → invariant checker → publish the results table to the job summary. Every third-party action is pinned to a full commit SHA.
