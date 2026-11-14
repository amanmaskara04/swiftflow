-- Order service schema (`orders`). Applied by swiftflow_common.migrate.

CREATE TABLE tenants (
  id uuid PRIMARY KEY,
  slug text NOT NULL UNIQUE,
  name text NOT NULL,
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
  kid text PRIMARY KEY,
  private_pem text NOT NULL,
  public_jwk jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  retired_at timestamptz
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
  next_payment_at timestamptz,
  version int NOT NULL DEFAULT 0,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  finalized_at timestamptz,
  UNIQUE (tenant_id, idempotency_key)
);
CREATE INDEX orders_tenant_created_idx ON orders (tenant_id, created_at DESC, id DESC);
CREATE INDEX orders_payment_due_idx ON orders (next_payment_at)
  WHERE status = 'RESERVED' OR payment_status = 'REFUND_PENDING';

CREATE TABLE order_items (
  id uuid PRIMARY KEY,
  order_id uuid NOT NULL REFERENCES orders(id),
  tenant_id uuid NOT NULL,
  sku text NOT NULL,
  qty int NOT NULL CHECK (qty > 0),
  warehouse_code text,
  unit_price_cents bigint
);
CREATE INDEX order_items_order_idx ON order_items (order_id);

CREATE TABLE order_history (
  id bigserial PRIMARY KEY,
  order_id uuid NOT NULL REFERENCES orders(id),
  tenant_id uuid NOT NULL,
  from_status text,
  to_status text NOT NULL,
  reason text,
  event_type text,
  at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX order_history_order_idx ON order_history (order_id, id);

CREATE TABLE outbox_events (
  id bigserial PRIMARY KEY,
  event_id uuid NOT NULL UNIQUE,
  tenant_id uuid NOT NULL,
  topic text NOT NULL,
  message_key text NOT NULL,
  event_type text NOT NULL,
  envelope jsonb NOT NULL,
  headers jsonb NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT now(),
  published_at timestamptz
);
CREATE INDEX outbox_unpublished_idx ON outbox_events (id) WHERE published_at IS NULL;

CREATE TABLE processed_events (
  consumer text NOT NULL,
  event_id uuid NOT NULL,
  processed_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (consumer, event_id)
);

-- Row-level security on every tenant-scoped table, FORCEd so the owner role is bound too.
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['orders','order_items','order_history'] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format($p$
      CREATE POLICY tenant_isolation ON %I
        USING (current_setting('app.system', true) = 'on'
               OR tenant_id = current_setting('app.tenant_id')::uuid)
        WITH CHECK (current_setting('app.system', true) = 'on'
               OR tenant_id = current_setting('app.tenant_id')::uuid)
    $p$, t);
  END LOOP;
END $$;
