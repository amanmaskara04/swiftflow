# Services

Every service is independently buildable, configurable and runnable. Each has its own directory, `requirements.txt`, `Dockerfile`, port, health checks and tests. They share code only through the `libs/common` package (`swiftflow_common`), never through runtime calls.

## Port map

| Port | Process | Health | Metrics |
|------|---------|--------|---------|
| 3000 | `web` (nginx, Docker) | `/` | — |
| 5173 | `web` (Vite dev server, host) | `/` | — |
| 8080 | `gateway` | `/healthz`, `/readyz` | `/metrics` |
| 8001 | `order-api` | `/healthz`, `/readyz` | `/metrics` |
| 9101 | `order-worker` | `/healthz` | `/metrics` |
| 8002 | `inventory-api` | `/healthz`, `/readyz` | `/metrics` |
| 9102 | `inventory-worker` | `/healthz` | `/metrics` |
| 8003 | `notification-api` | `/healthz`, `/readyz` | `/metrics` |
| 9103 | `notification-worker` | `/healthz` | `/metrics` |
| 8004 | `partner-sim` | `/healthz` | `/metrics` |
| 5432 | Postgres | `pg_isready` | — |
| 6379 | Redis | `PING` | — |
| 9092 | Kafka (host listener; containers use `kafka:29092`) | broker API | — |
| 9090 / 3001 / 16686 | Prometheus / Grafana / Jaeger (`observability` profile) | — | — |

`/healthz` answers "is the process alive" and never touches dependencies. `/readyz` checks Postgres and Redis and returns `503` with a per-dependency breakdown when any is down.

---

## Gateway — `services/gateway` — :8080

**Job:** be the one address clients use.

| Path prefix | Upstream |
|-------------|----------|
| `/v1/auth/*`, `/v1/orders*`, `/.well-known/jwks.json` | order :8001 |
| `/v1/warehouses*`, `/v1/products*`, `/v1/inventory*` | inventory :8002 |
| `/v1/webhooks*`, `/v1/events*` | notification :8003 |
| `/v1/sim/{config,payments,hooks}*` (control endpoints only; `GET`/`PUT`) | partner-sim :8004 (prefix stripped to `/v1/*`); routed only when `ENVIRONMENT` is `local` or `ci` |
| `/v1/system/health`, `/v1/system/info` | answered by the gateway itself |

The gateway forwards only the paths in this table (an allow-list); anything else is `404`. It never forwards to a host taken from the request.

**Behaviour**
- Streams request and response bodies (works for SSE on `/v1/events/stream`).
- Adds `X-Request-ID` if missing and returns it; starts a trace and forwards `traceparent`.
- Handles CORS for the dev console origin.
- Returns `502` as `application/problem+json` when an upstream is unreachable, `504` on timeout.
- `/v1/system/health` calls every service's `/readyz` and worker `/healthz` in parallel (1-second timeout) and returns status and latency per node — this drives the console's system map.

**Config:** `ORDER_URL`, `INVENTORY_URL`, `NOTIFICATION_URL`, `PARTNER_SIM_URL`, `ORDER_WORKER_URL`, `INVENTORY_WORKER_URL`, `NOTIFICATION_WORKER_URL`, `CORS_ORIGINS`, `UPSTREAM_TIMEOUT_SECONDS`.

**Stateless.** No database.

---

## Order service — `services/order` — :8001 / worker :9101

**Job:** identity, the order lifecycle and the saga.

**Owns** schema `orders`: `tenants`, `api_clients`, `signing_keys`, `orders`, `order_items`, `order_history`, `outbox_events`, `processed_events`.

**API process**
- Client-credentials token endpoint; Ed25519 JWT signing; JWKS publication.
- Create, list, get, cancel and ship orders. Create is idempotent (Redis + DB unique key).
- Order history (every state transition with time and reason) for the console's saga rail.

**Worker process**
| Task | What it does |
|------|--------------|
| Outbox relay | Publishes `orders.outbox_events` to `orders.events` |
| Consumer `order-saga` | Reads `inventory.events`: `StockReserved` → `RESERVED`; `StockReservationFailed` → `REJECTED` |
| Payment processor | Charges due `RESERVED` orders through the circuit breaker (`CONFIRMED` or `CANCELLED`); refunds orders in `REFUND_PENDING` |

**Events out:** `OrderPlaced`, `OrderConfirmed`, `OrderCancelled`, `OrderShipped`.
**Events in:** `StockReserved`, `StockReservationFailed`.
**External call:** partner-sim `POST /v1/payments` (idempotency key = `order_id`).

**Config:** `DATABASE_URL` (role `order_svc`), `REDIS_URL`, `KAFKA_BOOTSTRAP`, `PAYMENT_URL`, `JWT_ISSUER`, `JWT_AUDIENCE`, `JWT_PRIVATE_KEY_PEM` (optional; generated and stored in `signing_keys` if absent), `TOKEN_TTL_SECONDS=900`, `RATE_LIMIT_RPS`, `RATE_LIMIT_BURST`.

---

## Inventory service — `services/inventory` — :8002 / worker :9102

**Job:** the single source of truth for stock.

**Owns** schema `inventory`: `warehouses`, `products`, `stock_levels`, `reservations`, `adjustments`, `outbox_events`, `processed_events`.

**API process**
- Create and list warehouses and products (products carry `price_cents`).
- Stock adjustments (`set` or `delta`), with optional `If-Match` version for optimistic concurrency.
- Availability per SKU per warehouse (Redis cache, 5-second TTL, invalidated on change).
- Stock overview grid for the console (every SKU × warehouse with on hand, reserved, available).

**Worker process**
| Task | What it does |
|------|--------------|
| Outbox relay | Publishes to `inventory.events` and `inventory.stock-changes` |
| Consumer `inventory-reservations` | `OrderPlaced` → reserve all-or-nothing; `OrderCancelled` → release; `OrderShipped` → commit |

**Events out:** `StockReserved` (with unit prices), `StockReservationFailed`, `StockReleased`, `StockCommitted`, `StockLevelChanged`.
**Events in:** `OrderPlaced`, `OrderCancelled`, `OrderShipped`.

**Warehouse choice:** for each item, the warehouse with the most availability that can fill the whole line. If none can, the order is rejected with `insufficient_stock:<sku>`.

**Config:** `DATABASE_URL` (role `inventory_svc`), `REDIS_URL`, `KAFKA_BOOTSTRAP`, `JWKS_URL`, `AVAILABILITY_CACHE_TTL=5`.

---

## Notification service — `services/notification` — :8003 / worker :9103

**Job:** tell people and partners what happened.

**Owns** schema `notifications`: `event_log`, `webhook_subscriptions`, `webhook_deliveries`, `processed_events`.

**API process**
- Event feed (`GET /v1/events`, filter by `aggregate_id` or type) — used for the order timeline.
- Live stream (`GET /v1/events/stream`, Server-Sent Events, per tenant, fed by Redis pub/sub).
- Webhook subscriptions (secret returned once), deliveries list, manual retry, test send.

**Worker process**
| Task | What it does |
|------|--------------|
| Consumer `notification-feed` | Reads all three topics; writes `event_log`; publishes to Redis channel `events:<tenant>`; creates a `PENDING` delivery for each matching subscription |
| Webhook dispatcher | Sends due deliveries with `SwiftFlow-Signature`, retries on a 5 s → 30 s → 2 m → 10 m → 1 h schedule, then `FAILED`; circuit breaker per host |

**SSRF guard:** URLs must be `https` and resolve to public IPs, unless the host is listed in `WEBHOOK_ALLOWED_HOSTS` (development only, e.g. `partner-sim,localhost`).

**Config:** `DATABASE_URL` (role `notification_svc`), `REDIS_URL`, `KAFKA_BOOTSTRAP`, `JWKS_URL`, `WEBHOOK_ALLOWED_HOSTS`, `WEBHOOK_ALLOW_HTTP` (dev).

---

## Partner simulator — `services/partner-sim` — :8004

**Job:** play the outside world, on command.

- **Payment provider:** `POST /v1/payments` and `POST /v1/payments/{id}/refund`, each with an `Idempotency-Key`; the same key always returns the same result. Callers present `X-Provider-Key` (shared secret `PROVIDER_API_KEY`). Records every charge and refund so tests can prove there are no double charges.
- **Controls:** `GET/PUT /v1/config` — `failure_rate` (0–1), `latency_ms`, `outage` (returns `503`). The console's Chaos Lab drives these. Control endpoints require an `admin` JWT (verified via `JWKS_URL`).
- **Webhook receiver:** `POST /v1/hooks/{inbox}` stores headers and body; if the inbox's signing secret was registered (`PUT /v1/hooks/{inbox}/secret`), verifies the HMAC and timestamp and marks the delivery valid or invalid.
- State is in memory by design; restarting resets it.

**Config:** `DEFAULT_FAILURE_RATE=0`, `DEFAULT_LATENCY_MS=50`.

---

## Web console — `web/` — :3000 (Docker) / :5173 (dev)

React + TypeScript single-page app. Talks only to the gateway (`/v1/*`). In Docker, nginx serves the build and proxies `/v1` and `/.well-known` to `gateway:8080`; in development, Vite's proxy does the same against `localhost:8080`. Screens and design system: [ui-design.md](ui-design.md).

---

## Shared library — `libs/common` (`swiftflow_common`)

Written once, imported by every Python service.

| Module | Provides |
|--------|----------|
| `config` | `BaseServiceSettings` (pydantic-settings) |
| `db` | asyncpg pool with JSON codecs; `tenant_tx(tenant_id)` sets `app.tenant_id` with `set_config(..., true)`; `system_tx()` for cross-tenant workers |
| `migrate` | Ordered `.sql` migrations per schema with an advisory lock and `schema_migrations` table |
| `auth` | JWT verification via JWKS, `Principal`, `require_scope()` FastAPI dependency |
| `idempotency` | Request hashing; Redis-backed begin/complete with 24 h TTL |
| `ratelimit` | Redis Lua token bucket per tenant |
| `outbox` | `add_event(conn, …)` (validates against contracts, stores `traceparent`); `OutboxRelay` |
| `consumer` | `EventConsumer`: inbox, retries with jitter, DLQ, manual commits, metrics |
| `kafka` | Producer/consumer factories, topic bootstrap |
| `circuit_breaker` | Closed / open / half-open breaker with a Prometheus gauge |
| `problems` | RFC 9457 `application/problem+json` errors and handlers |
| `observability` | structlog setup, Prometheus HTTP middleware, OpenTelemetry setup and Kafka header propagation |
| `health` | Standard `/healthz`, `/readyz`, `/metrics` routes |
| `events` | `EventEnvelope` model and contract validation |

## Shared contracts — `libs/contracts`

One JSON Schema per event type in `libs/contracts/schemas/`, with sample messages in `libs/contracts/examples/`. Producers validate before writing to the outbox; contract tests validate every example. See [events.md](events.md).
