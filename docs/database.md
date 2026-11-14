# Database

One PostgreSQL 17 instance, three schemas, three login roles. A service never reads another service's schema; it learns about other data only from events.

## Roles and ownership

| Role | Owns schema | Can connect to |
|------|-------------|----------------|
| `order_svc` | `orders` | `orders` only |
| `inventory_svc` | `inventory` | `inventory` only |
| `notification_svc` | `notifications` | `notifications` only |
| `swiftflow` (superuser) | — | used only by `init.sql`, the invariant checker and the seed CLI |

`deploy/compose/postgres/init.sql` creates the roles and empty schemas on the container's first start, revokes `public` access, and sets each role's `search_path` to its own schema. Each service applies its own migrations at start-up.

## Migrations

Plain, ordered SQL files in `services/<svc>/migrations/NNN_name.sql`, applied by `swiftflow_common.migrate`:

1. Take `pg_advisory_lock(hashtext('<schema>'))` so concurrent replicas don't race.
2. Create `<schema>.schema_migrations (version text primary key, applied_at timestamptz)` if missing.
3. Apply each unapplied file in its own transaction and record it.

Migrations are forward-only. A change that would break running code is split into expand (add) and contract (remove) releases. See [ADR 0004](adr/0004-asyncpg-and-sql-migrations.md).

## Rules that hold across every schema

- Every business table has `tenant_id uuid NOT NULL` and row-level security with `FORCE`.
- No foreign keys cross schemas. `tenant_id` and `order_id` are plain UUIDs outside their home schema.
- Every producing schema has `outbox_events`; every consuming schema has `processed_events`.
- `updated_at` is maintained by the application in the same statement that changes the row.

## Schema `orders`

```sql
CREATE TABLE tenants (
  id uuid PRIMARY KEY, slug text NOT NULL UNIQUE, name text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE api_clients (                  -- no RLS: looked up before the tenant is known
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  client_id text NOT NULL UNIQUE,
  secret_hash text NOT NULL,                -- argon2id
  role text NOT NULL CHECK (role IN ('admin','partner')),
  scopes text[] NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  revoked_at timestamptz
);

CREATE TABLE signing_keys (                 -- used only if JWT_PRIVATE_KEY_PEM is not provided
  kid text PRIMARY KEY, private_pem text NOT NULL, public_jwk jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(), retired_at timestamptz
);

CREATE TABLE orders (
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL,
  status text NOT NULL CHECK (status IN ('PENDING','RESERVED','CONFIRMED','SHIPPED','REJECTED','CANCELLED')),
  customer_ref text,
  idempotency_key text NOT NULL,
  request_hash text NOT NULL,
  total_cents bigint,
  failure_reason text,
  payment_id text,
  payment_status text NOT NULL DEFAULT 'NONE'
    CHECK (payment_status IN ('NONE','CHARGED','REFUND_PENDING','REFUNDED')),
  payment_attempts int NOT NULL DEFAULT 0,
  next_payment_at timestamptz,              -- due time for a charge (RESERVED) or refund (REFUND_PENDING); doubles as the processor's lease
  version int NOT NULL DEFAULT 0,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  finalized_at timestamptz,                 -- first terminal or CONFIRMED state; saga duration
  UNIQUE (tenant_id, idempotency_key)
);
CREATE INDEX ON orders (tenant_id, created_at DESC, id DESC);
CREATE INDEX ON orders (next_payment_at) WHERE status = 'RESERVED' OR payment_status = 'REFUND_PENDING';

CREATE TABLE order_items (
  id uuid PRIMARY KEY,
  order_id uuid NOT NULL REFERENCES orders(id),
  tenant_id uuid NOT NULL,
  sku text NOT NULL,
  qty int NOT NULL CHECK (qty > 0),
  warehouse_code text,                      -- filled from StockReserved
  unit_price_cents bigint
);

CREATE TABLE order_history (
  id bigserial PRIMARY KEY,
  order_id uuid NOT NULL REFERENCES orders(id),
  tenant_id uuid NOT NULL,
  from_status text, to_status text NOT NULL,
  reason text, event_type text,
  at timestamptz NOT NULL DEFAULT now()
);
```

## Schema `inventory`

```sql
CREATE TABLE warehouses (
  id uuid PRIMARY KEY, tenant_id uuid NOT NULL,
  code text NOT NULL, name text NOT NULL, location text,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, code)
);

CREATE TABLE products (
  id uuid PRIMARY KEY, tenant_id uuid NOT NULL,
  sku text NOT NULL, name text NOT NULL,
  price_cents bigint NOT NULL CHECK (price_cents >= 0),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, sku)
);

CREATE TABLE stock_levels (
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL,
  product_id uuid NOT NULL REFERENCES products(id),
  warehouse_id uuid NOT NULL REFERENCES warehouses(id),
  on_hand int NOT NULL CHECK (on_hand >= 0),
  reserved int NOT NULL DEFAULT 0 CHECK (reserved >= 0),
  version int NOT NULL DEFAULT 0,
  updated_at timestamptz NOT NULL DEFAULT now(),
  CHECK (reserved <= on_hand),              -- last line of defence against overselling
  UNIQUE (product_id, warehouse_id)
);

CREATE TABLE reservations (
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL,
  order_id uuid NOT NULL,
  product_id uuid NOT NULL REFERENCES products(id),
  warehouse_id uuid NOT NULL REFERENCES warehouses(id),
  qty int NOT NULL CHECK (qty > 0),
  status text NOT NULL CHECK (status IN ('ACTIVE','RELEASED','COMMITTED')),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (order_id, product_id, warehouse_id)
);
CREATE INDEX ON reservations (order_id) WHERE status = 'ACTIVE';

CREATE TABLE adjustments (                  -- audit trail for every manual stock change
  id uuid PRIMARY KEY, tenant_id uuid NOT NULL,
  stock_level_id uuid NOT NULL REFERENCES stock_levels(id),
  mode text NOT NULL CHECK (mode IN ('set','delta')),
  quantity int NOT NULL, on_hand_before int NOT NULL, on_hand_after int NOT NULL,
  reason text, actor text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
```

## Schema `notifications`

```sql
CREATE TABLE event_log (
  event_id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL,
  event_type text NOT NULL, topic text NOT NULL,
  aggregate_id uuid NOT NULL,
  payload jsonb NOT NULL,
  occurred_at timestamptz NOT NULL,
  received_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON event_log (tenant_id, aggregate_id, occurred_at);
CREATE INDEX ON event_log (tenant_id, received_at DESC);

CREATE TABLE webhook_subscriptions (
  id uuid PRIMARY KEY, tenant_id uuid NOT NULL,
  url text NOT NULL, event_types text[] NOT NULL,
  signing_secret text NOT NULL,             -- needed to sign; production would encrypt with a KMS key
  active boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE webhook_deliveries (
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL,
  subscription_id uuid NOT NULL REFERENCES webhook_subscriptions(id),
  event_id uuid NOT NULL,
  event_type text NOT NULL,
  status text NOT NULL CHECK (status IN ('PENDING','DELIVERED','FAILED')),
  attempts int NOT NULL DEFAULT 0,
  next_attempt_at timestamptz,
  last_status_code int, last_error text,
  created_at timestamptz NOT NULL DEFAULT now(),
  delivered_at timestamptz,
  UNIQUE (subscription_id, event_id)
);
CREATE INDEX ON webhook_deliveries (next_attempt_at) WHERE status = 'PENDING';
```

## Shared shape (in each schema that needs it)

```sql
CREATE TABLE outbox_events (
  id bigserial PRIMARY KEY,                 -- publish order
  event_id uuid NOT NULL UNIQUE,
  tenant_id uuid NOT NULL,
  topic text NOT NULL,
  message_key text NOT NULL,
  event_type text NOT NULL,
  envelope jsonb NOT NULL,
  headers jsonb NOT NULL DEFAULT '{}',      -- traceparent captured at write time
  created_at timestamptz NOT NULL DEFAULT now(),
  published_at timestamptz
);
CREATE INDEX ON outbox_events (id) WHERE published_at IS NULL;

CREATE TABLE processed_events (
  consumer text NOT NULL,
  event_id uuid NOT NULL,
  processed_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (consumer, event_id)
);
```

A housekeeping task deletes published outbox rows older than 7 days and inbox rows older than 14 days (longer than Kafka's retention, so a replay is still deduplicated).

## Row-level security

Applied to every tenant-scoped table (`orders`, `order_items`, `order_history`, `warehouses`, `products`, `stock_levels`, `reservations`, `adjustments`, `event_log`, `webhook_subscriptions`, `webhook_deliveries`):

```sql
ALTER TABLE stock_levels ENABLE ROW LEVEL SECURITY;
ALTER TABLE stock_levels FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON stock_levels
  USING (
    current_setting('app.system', true) = 'on'
    OR tenant_id = current_setting('app.tenant_id')::uuid
  )
  WITH CHECK (
    current_setting('app.system', true) = 'on'
    OR tenant_id = current_setting('app.tenant_id')::uuid
  );
```

| Session state | Result |
|---------------|--------|
| `tenant_tx(A)`: `set_config('app.tenant_id', A, true)` | Sees and writes only tenant A's rows |
| `system_tx()`: `app.system = on` and `app.tenant_id` = the nil UUID, both for the transaction only | Cross-tenant, used by relays, dispatchers and the payment processor. The nil UUID is set because SQL does not guarantee `OR` short-circuits, so the cast must never fail |
| Nothing set (a hand-written query) | Fails closed with an error: `unrecognized configuration parameter "app.tenant_id"` on a fresh connection, or `invalid input syntax for type uuid: ""` on a pooled connection whose earlier transaction-local setting has reverted to empty |

Settings are transaction-local (`is_local = true`), so a pooled connection never carries one tenant's setting into the next request. RLS guards against application bugs that forget a tenant filter; the service role itself is trusted. See [ADR 0005](adr/0005-tenant-isolation.md).

## The reservation transaction

```sql
-- per item, in (product_id, warehouse_id) order, inside one transaction
UPDATE stock_levels
SET    reserved = reserved + $1, version = version + 1, updated_at = now()
WHERE  id = $2 AND on_hand - reserved >= $1
RETURNING on_hand, reserved, version;
-- 0 rows → try the next warehouse with enough availability; none left → roll back,
-- emit StockReservationFailed
INSERT INTO reservations (…) VALUES (…) ON CONFLICT (order_id, product_id, warehouse_id) DO NOTHING;
```

Release and commit:

```sql
-- release (cancel / payment declined)
UPDATE stock_levels s SET reserved = s.reserved - r.qty, version = s.version + 1
FROM reservations r WHERE r.order_id = $1 AND r.status = 'ACTIVE' AND s.product_id = r.product_id AND s.warehouse_id = r.warehouse_id;
UPDATE reservations SET status = 'RELEASED' WHERE order_id = $1 AND status = 'ACTIVE';

-- commit (shipped)
UPDATE stock_levels … SET on_hand = on_hand - r.qty, reserved = reserved - r.qty …;
UPDATE reservations SET status = 'COMMITTED' WHERE order_id = $1 AND status = 'ACTIVE';
```
