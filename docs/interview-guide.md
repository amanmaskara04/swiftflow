# Interview guide

## The 3-minute demo

Run from the console at http://localhost:3000, signed in as `acme-admin`.

| Time | Screen | Do | Say |
|------|--------|----|-----|
| 0:00 | System map | Point at the nodes and ports | "Six services, each on its own port. The three domain services never call each other: every hand-off is a Kafka event, and each owns its own Postgres schema." |
| 0:25 | Orders | Place an order; open its drawer | "The API returns 202 straight away. Watch the rail: Inventory reserves, Order charges the payment provider, and the order is confirmed — about half a second end to end." |
| 0:55 | Orders | Re-send with the same idempotency key | "Same key, same body: the stored response comes back, and no second order exists." |
| 1:10 | Chaos Lab | Decline rate to 100 %; place an order | "Payment declined, so the saga compensates: the rail branches, Inventory releases the stock, and the order ends cancelled." |
| 1:35 | Chaos Lab | Run the oversell test (100 units, 500 orders) | "500 orders at once against 100 units. Exactly 100 confirmed, 400 rejected, zero oversold. That's one conditional UPDATE under a row lock, with a CHECK constraint as backstop — no distributed lock." |
| 2:15 | Chaos Lab | Turn on provider outage; place orders; turn it off | "The circuit breaker opens, orders wait in Reserved instead of failing, and they complete when the provider comes back." |
| 2:40 | Webhooks | Show a delivery with "Signature valid" | "Partners get HMAC-signed webhooks with retries, and the URL is checked against private IP ranges at delivery time to block SSRF." |

## Talking points, by pattern

| Pattern | Where | One-line explanation |
|---------|-------|----------------------|
| Transactional outbox | every producer | Business row and event row commit together; a relay publishes afterwards, at least once |
| Idempotent consumer (inbox) | every consumer | `(consumer, event_id)` inserted in the same transaction as the effect |
| Orchestrated saga with compensation | order worker | One place owns each order's state, so "where is order X stuck?" always has an answer |
| Conditional update under row lock | inventory | Correctness lives in Postgres; Redis only speeds reads up |
| Idempotency keys | `POST` endpoints, payments | Redis for the fast path, a DB unique constraint as backstop; payment uses `order_id` |
| Circuit breaker (hand-written) | payments, webhooks | Closed → open after 5 failures → half-open probe after 15 s |
| Lease-based work claiming | payment processor, dispatcher | `FOR UPDATE SKIP LOCKED` + a lease timestamp; crash-safe, no external call inside a transaction |
| Row-level security with `FORCE` | every tenant table | Tenant from the JWT, set per transaction; an unset tenant fails closed |
| Asymmetric JWTs + JWKS | order issues, others verify | No shared signing secret across services |
| DLQ | every consumer | 3 retries with jitter, then `.dlq` with error headers; the partition keeps moving |

## Likely questions

| Question | Core of the answer |
|----------|--------------------|
| How do you prevent overselling? | Conditional `UPDATE … WHERE on_hand - reserved >= qty` evaluated under the row lock; `CHECK (reserved <= on_hand)` as backstop; rows locked in a fixed order to avoid deadlocks |
| Why not a Redis lock? | Leases can expire during a GC pause or network delay, letting two holders proceed. The database already serialises the write |
| Is it exactly-once? | No: at-least-once delivery with idempotent consumers. Kafka transactions are exactly-once only within Kafka |
| What if Inventory crashes mid-saga? | Its offset was never committed, so the event is redelivered; the inbox drops duplicates; the saga continues. The chaos test proves it |
| What if the relay publishes and then crashes? | Events are published again; consumers dedupe on `event_id`. Duplicates are allowed, losses are not |
| Why Kafka instead of HTTP between services? | Decoupling, buffering during outages, replay, independent scaling. The cost is eventual consistency and more moving parts |
| Orchestration or choreography? | Orchestration: one place to see an order's state. The trade-off is that Order knows every step |
| Why does payment run outside the consumer? | A slow provider must not hold a row lock or block a partition; the lease makes a crash mid-call safe, and the idempotency key makes the retry safe |
| How is one tenant isolated from another? | RLS with `FORCE`, `set_config('app.tenant_id', …, true)` per transaction from the JWT, a separate DB role per service |
| What breaks first under load? | The single Postgres and the single broker. Consumers scale to the partition count (6). Next steps: read replica for availability, more partitions, 3 brokers |
| How would you go multi-region? | Each region owns its warehouses' stock; replicate events; route orders to the owning region. Cross-region orders get slower |

## Resume entry

**SwiftFlow — Distributed Order Fulfillment Platform** · Python, FastAPI, Kafka, PostgreSQL, Redis, React, Docker
- Built a multi-tenant, event-driven fulfillment platform of six independently deployable services, using the transactional outbox, idempotent consumers and an orchestrated saga with compensating transactions.
- Prevented overselling with conditional updates under Postgres row locks: *[your measured result, e.g. 0 units oversold across 500 concurrent orders for 100 units]*, with all invariants holding under scripted process and broker failures.
- Isolated tenants with Postgres row-level security and Ed25519 JWTs verified via JWKS; delivered HMAC-signed webhooks with retries and SSRF protection.
- Built a React operations console with a live saga view, system map and a chaos lab for injecting payment failures and running the oversell test.

Only quote numbers you have produced and saved in `docs/results/`.
