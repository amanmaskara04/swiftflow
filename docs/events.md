# Events

Services communicate only through these events. Each event type has a JSON Schema in `libs/contracts/schemas/<EventType>.v1.json`; producers validate before writing to the outbox, and the contract test suite validates every file in `libs/contracts/examples/`.

## Topics

| Topic | Key | Producer | Consumers (group) | Event types |
|-------|-----|----------|-------------------|-------------|
| `orders.events` | `order_id` | order | inventory (`inventory-reservations`), notification (`notification-feed`) | `OrderPlaced`, `OrderConfirmed`, `OrderCancelled`, `OrderShipped` |
| `inventory.events` | `order_id` | inventory | order (`order-saga`), notification (`notification-feed`) | `StockReserved`, `StockReservationFailed`, `StockReleased`, `StockCommitted` |
| `inventory.stock-changes` | `tenant_id:sku` | inventory | notification (`notification-feed`) | `StockLevelChanged` |
| `<topic>.dlq` | original key | any consumer | operators | original message + `error`, `attempts`, `consumer`, `failed_at` headers |

6 partitions per topic, replication factor 1 (single local broker). Topics are created by each worker at start-up if missing.

## Envelope

Every message value is this JSON object. Kafka headers carry `traceparent` (and `tracestate` when present), `event_type` and `tenant_id` so routing and tracing never need to parse the body.

```json
{
  "event_id": "7f9c2b0e-0c1a-4c1e-9a55-2a6f3f1f8d10",
  "event_type": "StockReserved",
  "schema_version": 1,
  "occurred_at": "2026-11-14T10:15:00.123Z",
  "tenant_id": "6f1d…",
  "aggregate_id": "0b6e…",
  "producer": "inventory",
  "payload": { }
}
```

| Field | Rule |
|-------|------|
| `event_id` | UUID; the deduplication key for every consumer |
| `schema_version` | Integer; additive changes keep the version, breaking changes add a new schema file and topic consumers handle both during migration |
| `aggregate_id` | Order ID for order and reservation events; product ID for `StockLevelChanged` |

## Event types

### `OrderPlaced` — order → `orders.events`
```json
{ "order_id": "…", "customer_ref": "shopify-#10412",
  "items": [ { "sku": "TEE-CLASSIC-M", "qty": 2 } ] }
```

### `StockReserved` — inventory → `inventory.events`
```json
{ "order_id": "…",
  "items": [ { "sku": "TEE-CLASSIC-M", "qty": 2, "warehouse_id": "…", "warehouse_code": "BLR-1", "unit_price_cents": 1800 } ],
  "total_cents": 3600 }
```

### `StockReservationFailed` — inventory → `inventory.events`
```json
{ "order_id": "…", "reason": "insufficient_stock", "sku": "TEE-CLASSIC-M", "requested": 2, "available": 0 }
```
`reason` is one of `insufficient_stock`, `unknown_sku`.

### `OrderConfirmed` — order → `orders.events`
```json
{ "order_id": "…", "total_cents": 3600, "payment_id": "pay_…" }
```

### `OrderCancelled` — order → `orders.events`
```json
{ "order_id": "…", "reason": "payment_declined", "previous_status": "RESERVED" }
```
`reason`: `payment_declined`, `customer_request`. Refunds are an Order-internal concern (recorded in `order_history` as `PaymentRefunded`) and do not produce a Kafka event.

### `OrderShipped` — order → `orders.events`
```json
{ "order_id": "…" }
```

### `StockReleased` — inventory → `inventory.events`
```json
{ "order_id": "…", "items": [ { "sku": "…", "warehouse_id": "…", "qty": 2 } ] }
```
Empty `items` when there was nothing to release (cancel before reservation).

### `StockCommitted` — inventory → `inventory.events`
```json
{ "order_id": "…", "items": [ { "sku": "…", "warehouse_id": "…", "qty": 2 } ] }
```

### `StockLevelChanged` — inventory → `inventory.stock-changes`
```json
{ "sku": "TEE-CLASSIC-M", "warehouse_code": "BLR-1", "on_hand": 98, "reserved": 10, "available": 88, "cause": "reservation" }
```
`cause`: `adjustment`, `reservation`, `release`, `commit`. Partners subscribe to this to keep channel listings in sync.

## Handling rules

| Consumer | Event | Effect | Idempotency |
|----------|-------|--------|-------------|
| inventory | `OrderPlaced` | Reserve all items or none; emit `StockReserved` or `StockReservationFailed` | inbox + `UNIQUE (order_id, product_id, warehouse_id)` |
| inventory | `OrderCancelled` | Release `ACTIVE` reservations; emit `StockReleased` | inbox + only `ACTIVE` rows change |
| inventory | `OrderShipped` | Commit reservations; emit `StockCommitted` | inbox + only `ACTIVE` rows change |
| order | `StockReserved` | If order is `PENDING` → `RESERVED`, schedule payment. If it was cancelled meanwhile → nothing: the `OrderCancelled` already follows `OrderPlaced` on the same partition, so inventory releases the reservation right after making it | inbox + state guard |
| order | `StockReservationFailed` | If `PENDING` → `REJECTED` | inbox + state guard |
| notification | all | Append to `event_log`, publish to Redis, queue matching webhook deliveries | inbox + `event_log` primary key + `UNIQUE (subscription_id, event_id)` |

### Retry and dead-lettering
1. Handler raises → transaction rolls back (inbox row included) → retry after `0.2 s × 2^attempt` with full jitter.
2. After 3 attempts, or immediately for an envelope that fails to parse or validate, the original message goes to `<topic>.dlq` with error headers, and the offset moves on.
3. `swiftflow_events_consumed_total{consumer, result}` counts `ok`, `duplicate`, `retry`, `dlq`.
