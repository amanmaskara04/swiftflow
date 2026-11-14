-- Notification service schema (`notifications`). Applied by swiftflow_common.migrate.

CREATE TABLE event_log (
  event_id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL,
  event_type text NOT NULL,
  topic text NOT NULL,
  aggregate_id uuid NOT NULL,
  payload jsonb NOT NULL,
  occurred_at timestamptz NOT NULL,
  received_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX event_log_aggregate_idx ON event_log (tenant_id, aggregate_id, occurred_at);
CREATE INDEX event_log_received_idx ON event_log (tenant_id, received_at DESC);

CREATE TABLE webhook_subscriptions (
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL,
  url text NOT NULL,
  event_types text[] NOT NULL,
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
  last_status_code int,
  last_error text,
  created_at timestamptz NOT NULL DEFAULT now(),
  delivered_at timestamptz,
  UNIQUE (subscription_id, event_id)
);
CREATE INDEX webhook_deliveries_due_idx ON webhook_deliveries (next_attempt_at) WHERE status = 'PENDING';
CREATE INDEX webhook_deliveries_tenant_idx ON webhook_deliveries (tenant_id, created_at DESC, id DESC);

CREATE TABLE processed_events (
  consumer text NOT NULL,
  event_id uuid NOT NULL,
  processed_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (consumer, event_id)
);

DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['event_log','webhook_subscriptions','webhook_deliveries'] LOOP
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
