# API reference

All requests go through the gateway at `http://localhost:8080`. Every service also serves its own Swagger UI at `http://localhost:800x/docs`.

## Conventions

| Topic | Rule |
|-------|------|
| Auth | `Authorization: Bearer <jwt>` on every endpoint except `/v1/auth/token`, `/.well-known/jwks.json` and health checks |
| Tenant | Always taken from the token's `tenant_id` claim; never accepted in a body or query |
| Idempotency | `POST /v1/orders` and `POST /v1/inventory/adjustments` require `Idempotency-Key` (1–200 chars); cancel and ship accept one but are safe to repeat without it (a second call returns `409 invalid-state`). Same key + same body → stored response replayed with `Idempotent-Replayed: true`. Same key + different body → `422`. Key still in flight → `409`. Keys live 24 h |
| Errors | RFC 9457 `application/problem+json`: `type`, `title`, `status`, `detail`, `instance`, plus `request_id` |
| Rate limits | Token bucket per tenant. Over the limit → `429` with `Retry-After`. Every response carries `RateLimit-Remaining` |
| Pagination | Cursor based: `?limit=50&cursor=<opaque>`; responses include `next_cursor` (null on the last page) |
| Tracing | W3C `traceparent` accepted and returned; `X-Request-ID` accepted or generated |
| Time | ISO 8601 UTC; money in integer cents |

## Error types

| `type` | Status | When |
|--------|--------|------|
| `/problems/unauthenticated` | 401 | Missing, expired or invalid token |
| `/problems/forbidden` | 403 | Token lacks the required scope |
| `/problems/not-found` | 404 | Resource does not exist **for this tenant** |
| `/problems/idempotency-conflict` | 409 | Same key still being processed |
| `/problems/version-conflict` | 409 | `If-Match` version is stale |
| `/problems/invalid-state` | 409 | e.g. cancelling a shipped order |
| `/problems/idempotency-mismatch` | 422 | Same key reused with a different body |
| `/problems/validation` | 422 | Body failed validation (`errors` array included) |
| `/problems/rate-limited` | 429 | Tenant bucket empty |
| `/problems/upstream-unavailable` | 502 / 504 | Gateway could not reach a service |

```json
{
  "type": "/problems/invalid-state",
  "title": "Order cannot be cancelled",
  "status": 409,
  "detail": "Order 0b6e… is SHIPPED; only PENDING, RESERVED or CONFIRMED orders can be cancelled.",
  "instance": "/v1/orders/0b6e…/cancel",
  "request_id": "req_01J…"
}
```

## Scopes

| Role | Scopes |
|------|--------|
| `admin` | `admin`, `orders:read`, `orders:write`, `inventory:read`, `webhooks:read`, `webhooks:write` |
| `partner` | `orders:read`, `orders:write`, `inventory:read`, `webhooks:read`, `webhooks:write` |

---

## Auth (order service)

### `POST /v1/auth/token`
Client-credentials grant. Accepts JSON or `application/x-www-form-urlencoded`.

```json
{ "grant_type": "client_credentials", "client_id": "acme-admin", "client_secret": "…" }
```
`200`
```json
{ "access_token": "eyJ…", "token_type": "Bearer", "expires_in": 900, "scope": "admin orders:read …", "tenant_id": "…", "tenant_name": "Acme Outfitters" }
```
Claims: `iss`, `aud`, `sub` (client_id), `tenant_id`, `scope`, `role`, `iat`, `exp`, `jti`. Header `kid` identifies the signing key.

### `GET /.well-known/jwks.json`
Public Ed25519 keys (`kty: OKP`, `crv: Ed25519`). Cached by verifiers for 10 minutes.

---

## Orders (order service)

### `POST /v1/orders` — scope `orders:write`
Headers: `Idempotency-Key` (required).
```json
{
  "customer_ref": "shopify-#10412",
  "items": [ { "sku": "TEE-CLASSIC-M", "qty": 2 }, { "sku": "CAP-TRAIL", "qty": 1 } ]
}
```
`202 Accepted` — the saga continues asynchronously.
```json
{ "id": "0b6e…", "status": "PENDING", "customer_ref": "shopify-#10412", "items": [ … ], "total_cents": null, "created_at": "…" }
```
Limits: 1–50 items, `qty` 1–1000, SKUs unique within an order.

### `GET /v1/orders` — scope `orders:read`
Query: `status` (repeatable), `limit` (1–100, default 50), `cursor`, `q` (matches id prefix or `customer_ref`).
```json
{ "items": [ { "id": "…", "status": "CONFIRMED", "total_cents": 5400, "item_count": 3, "created_at": "…", "updated_at": "…" } ], "next_cursor": "…" }
```

### `GET /v1/orders/{id}` — scope `orders:read`
Order with items (including warehouse and unit price once reserved), `failure_reason`, and `history`:
```json
"history": [
  { "to_status": "PENDING",   "at": "…", "reason": null, "event_type": "OrderPlaced" },
  { "to_status": "RESERVED",  "at": "…", "reason": null, "event_type": "StockReserved" },
  { "to_status": "CONFIRMED", "at": "…", "reason": null, "event_type": "PaymentApproved" }
]
```

### `GET /v1/orders/stats` — scope `orders:read`
Counts by status, orders per minute for the last 60 minutes, saga duration p50 and p95 — powers the Overview screen.

### `POST /v1/orders/{id}/cancel` — scope `orders:write`
Allowed from `PENDING`, `RESERVED`, `CONFIRMED`. `202` with the updated order; stock is released asynchronously, and a charged order is refunded asynchronously (`payment_status` goes `REFUND_PENDING` → `REFUNDED`). Otherwise `409 invalid-state`.

### `POST /v1/orders/{id}/ship` — scope `admin`
Allowed from `CONFIRMED`. `202`; inventory commits the reservation asynchronously.

---

## Inventory (inventory service)

### `POST /v1/warehouses` — scope `admin`
```json
{ "code": "BLR-1", "name": "Bengaluru North", "location": "Bengaluru, IN" }
```
`201` with the warehouse. `409` if the code exists for this tenant.

### `GET /v1/warehouses` — scope `inventory:read`

### `POST /v1/products` — scope `admin`
```json
{ "sku": "TEE-CLASSIC-M", "name": "Classic tee, medium", "price_cents": 1800 }
```

### `GET /v1/products` — scope `inventory:read`
Query: `q`, `limit`, `cursor`.

### `POST /v1/inventory/adjustments` — scope `admin`
Headers: `Idempotency-Key` (required), `If-Match: <version>` (optional).
```json
{ "sku": "TEE-CLASSIC-M", "warehouse_code": "BLR-1", "mode": "set", "quantity": 100, "reason": "cycle count" }
```
`mode`: `set` (absolute `on_hand`) or `delta` (signed change). Rejected with `409` if it would make `on_hand < reserved`. Emits `StockLevelChanged`.

### `GET /v1/inventory/{sku}/availability` — scope `inventory:read`
```json
{ "sku": "TEE-CLASSIC-M", "total_available": 140, "cached": true,
  "warehouses": [ { "code": "BLR-1", "on_hand": 100, "reserved": 12, "available": 88, "version": 41 } ] }
```

### `GET /v1/inventory/stock` — scope `inventory:read`
The full SKU × warehouse grid for the console, plus totals and an `oversold_rows` count that must always be `0`.

---

## Events and webhooks (notification service)

### `GET /v1/events` — scope `orders:read`
Query: `aggregate_id`, `type`, `limit`, `cursor`. Tenant's event feed, newest first.

### `GET /v1/events/stream` — scope `orders:read`
Server-Sent Events, authenticated with the normal `Authorization` header. The console reads the stream with `fetch` and a `ReadableStream` instead of `EventSource`, so the token never appears in a URL (URLs end up in access logs and browser history). Each message: `event: <EventType>`, `data: <envelope JSON>`. A `: keep-alive` comment every 15 s.

### `POST /v1/webhooks/subscriptions` — scope `webhooks:write`
```json
{ "url": "https://partner.example.com/hooks/swiftflow", "event_types": ["OrderConfirmed", "OrderCancelled"] }
```
`201` — **`signing_secret` is returned only here.** `event_types` may contain `"*"`.

### `GET /v1/webhooks/subscriptions` · `GET /v1/webhooks/subscriptions/{id}` · `DELETE /v1/webhooks/subscriptions/{id}`

### `POST /v1/webhooks/subscriptions/{id}/test` — scope `webhooks:write`
Queues a `WebhookTest` delivery to the subscription.

### `GET /v1/webhooks/deliveries` — scope `webhooks:read`
Query: `status` (`PENDING`, `DELIVERED`, `FAILED`), `subscription_id`, `limit`, `cursor`.

### `POST /v1/webhooks/deliveries/{id}/retry` — scope `webhooks:write`
Resets a `FAILED` delivery to `PENDING`, due now.

### Webhook request format
```
POST <subscription url>
Content-Type: application/json
SwiftFlow-Event: OrderConfirmed
SwiftFlow-Delivery: <delivery id>
SwiftFlow-Signature: t=1763115300,v1=5f2b…   (hex HMAC-SHA256 of "<t>.<raw body>")

<event envelope JSON>
```
Receivers should reject signatures older than 5 minutes and compare with a constant-time function.

---

## Partner simulator (through the gateway at `/v1/sim/*`)

The simulator has two faces. Its **provider endpoints** are called directly by the order and notification workers on `:8004` and are **not routed by the gateway**. Its **control endpoints** are routed by the gateway under `/v1/sim/*` and require a JWT with the `admin` scope (verified via JWKS, like every other service).

| Method + path | Via gateway | Auth | Purpose |
|---------------|-------------|------|---------|
| `POST :8004/v1/payments` | no | `X-Provider-Key` shared secret | Charge (`Idempotency-Key` required) → `200 {status: approved|declined, payment_id}` or `503` during an outage |
| `POST :8004/v1/payments/{ref}/refund` | no | `X-Provider-Key` | Refund (`Idempotency-Key` required). `ref` is the payment ID, or the charge's idempotency key when the caller never learned the payment ID; if no charge exists yet it is voided, so a late charge with that key takes no money |
| `POST :8004/v1/hooks/{inbox}` | no | none (it is a public webhook receiver; the signature is the proof) | Receive a webhook |
| `GET /v1/sim/payments` | yes | `admin` | Recorded charges and refunds, plus `double_charges` (must be 0) |
| `GET /v1/sim/config` · `PUT /v1/sim/config` | yes | `admin` | `{ "failure_rate": 0.3, "latency_ms": 200, "outage": false }` |
| `PUT /v1/sim/hooks/{inbox}/secret` | yes | `admin` | Register a signing secret for verification |
| `GET /v1/sim/hooks/{inbox}` | yes | `admin` | Received deliveries with `signature_valid` |

Inbox names are namespaced by tenant on the control side (`<tenant_slug>-<name>`), so one tenant's admin cannot read another's received webhooks. The gateway routes `/v1/sim/*` only when `ENVIRONMENT` is `local` or `ci`.

---

## System (gateway)

### `GET /v1/system/info` — no auth
```json
{ "environment": "local", "version": "0.1.0",
  "demo_clients": [ { "client_id": "acme-admin", "client_secret": "acme-admin-secret", "role": "admin", "tenant": "Acme Outfitters" } ],
  "webhook_test_receiver_url": "http://partner-sim:8004/v1/hooks" }
```
`demo_clients` is populated from the `DEMO_CLIENTS` setting and is an empty list unless `ENVIRONMENT=local`. `webhook_test_receiver_url` is the simulator's address **as the notification worker sees it** (differs between Docker and host runs).

`GET /v1/system/health` requires any valid token.

### `GET /v1/system/health`
```json
{
  "status": "degraded",
  "checked_at": "…",
  "nodes": [
    { "id": "order-api", "kind": "api", "port": 8001, "status": "up", "latency_ms": 4, "checks": { "postgres": "up", "redis": "up" } },
    { "id": "inventory-worker", "kind": "worker", "port": 9102, "status": "down", "error": "connect timeout" }
  ]
}
```
