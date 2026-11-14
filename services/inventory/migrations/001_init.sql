-- Inventory service schema (`inventory`). Applied by swiftflow_common.migrate.

CREATE TABLE warehouses (
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL,
  code text NOT NULL,
  name text NOT NULL,
  location text,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, code)
);

CREATE TABLE products (
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL,
  sku text NOT NULL,
  name text NOT NULL,
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
CREATE INDEX reservations_active_idx ON reservations (order_id) WHERE status = 'ACTIVE';

CREATE TABLE adjustments (
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL,
  stock_level_id uuid NOT NULL REFERENCES stock_levels(id),
  mode text NOT NULL CHECK (mode IN ('set','delta')),
  quantity int NOT NULL,
  on_hand_before int NOT NULL,
  on_hand_after int NOT NULL,
  reason text,
  actor text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

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

DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['warehouses','products','stock_levels','reservations','adjustments'] LOOP
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
