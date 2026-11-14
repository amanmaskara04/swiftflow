# SwiftFlow — Free-Tier Architecture & Build Guide

Nov 14, 2026 · @Noir

## 1. Project overview and goals

SwiftFlow is a multi-tenant order fulfillment and inventory platform that never oversells, built from three event-driven microservices, deployed and tested end to end for $0 with no credit card anywhere. This guide is the single reference from empty repo to deployed product.

**Problem.** Multi-warehouse retailers and 3PLs oversell because stock counts drift between warehouses and sales channels. Enterprise WMS products (Manhattan Associates, SAP) cost far more than a mid-size business can pay.

**Users.** Ops teams at multi-warehouse e-commerce brands, 3PL operators, and marketplace sellers syncing stock across Shopify, Amazon and in-house stores.

**What the finished project must prove** (each is a demo you can run in an interview):

1. No overselling: 500 concurrent orders against 100 units of stock reserve exactly 100.
2. No lost or duplicated events: kill any pod mid-order and every order still ends in a correct final state.
3. Failure recovery: a failed payment releases reserved stock automatically (saga compensation).
4. Tenant isolation: one tenant cannot read another's rows, even with a hand-written query.
5. Operability: every pull request deploys the whole platform to a fresh Kubernetes cluster in CI and runs end-to-end checks; traces, dashboards and alerts show what happened.

## 2. Architecture

SwiftFlow is three FastAPI services (Order, Inventory, Notification) that never call each other. Every hand-off between them is a Kafka event, and each service owns its own Postgres schema.

&#91;embedded content: system architecture · 3 services, 1 event bus\]

Clients reach the services only through Traefik. Physically the cluster runs one Postgres 17 instance and one Redis instance, but each service connects with its own database role and sees only its own schema. The only synchronous outbound call is Order to the payment provider, wrapped in a circuit breaker.

### The order saga

The Order service orchestrates each order: it reacts to the inventory and payment results and issues the next step, or the compensation when a step fails.

&#91;embedded content: order saga · 2 decisions, 1 compensation\]

Every transition is driven by an event on Kafka, so a crash at any point resumes from the last committed event rather than losing the order. Cancelling a confirmed order before shipping takes the same release path.

### Key design decisions

- **Orchestration, not choreography.** One service holds each order's state, so you can always answer "where is order X stuck?"
- **Correctness lives in Postgres.** The conditional stock update (section 4) prevents overselling; Redis only speeds things up.
- **Outbox + inbox = effectively-once.** The outbox guarantees no event is lost; the `processed_events` table makes redelivery harmless.
- **Payment is idempotent too.** Order sends `order_id` as the payment idempotency key, so a retry never charges twice.
- **Scale by process, not by service.** API, relay and consumer are separate Deployments, so consumers scale on Kafka lag without touching the API.

## 3. Complete tech stack

Every item below is open source or free with no credit card. The only accounts you need are GitHub and, optionally, Azure for Students.

### Application

| Layer | Technology | Used for |
| --- | --- | --- |
| Language | Python 3.12+ | All three services |
| Dependency manager | uv | Fast installs, one lockfile per service |
| Web framework | FastAPI + Uvicorn | REST APIs, Swagger UI at `/docs` |
| Validation | Pydantic v2 | Request/response models, event payload models, settings |
| Database access | SQLAlchemy 2.x (async) + asyncpg | Queries, transactions, row versioning |
| Migrations | Alembic | One migration history per service schema |
| Kafka client | aiokafka | Async producer (outbox relay) and consumers |
| Redis client | redis-py (asyncio) | Idempotency cache, rate limiter, availability cache |
| HTTP client | httpx | Mock payment calls, webhook delivery |
| Retries | tenacity | Exponential backoff with jitter |
| Circuit breaker | Hand-written (\~60 lines) | Payment and webhook calls; writing it yourself is a strong interview talking point |
| Auth | PyJWT + argon2-cffi | Sign/verify JWTs, hash API secrets |
| Tracing | OpenTelemetry SDK (FastAPI, SQLAlchemy, httpx instrumentation) | One trace per order across HTTP, Kafka and every consumer |
| Logging | structlog | JSON logs carrying `trace_id`, `tenant_id`, `order_id` |
| Metrics | prometheus-client | `/metrics` endpoint on every service |

### Data and messaging (all inside the cluster)

| Technology | Used for |
| --- | --- |
| PostgreSQL 17 (official image) | One database, three schemas: `orders`, `inventory`, `notifications` |
| Apache Kafka 4.x in KRaft mode, run by the Strimzi operator | Event bus between services; no ZooKeeper |
| Redis 7+ (official image) | Idempotency responses, per-tenant rate limits, hot availability cache |

### Platform and delivery

| Technology | Used for |
| --- | --- |
| Docker + Docker Compose | Fast inner-loop development |
| k3d (laptop) and kind (CI) | Real Kubernetes clusters that cost nothing |
| Terraform (kind, helm, kubernetes providers; azurerm for the optional cloud host) | The whole platform from one `terraform apply`: cluster, Kafka, Redis, Postgres, monitoring, Argo CD |
| Traefik | Ingress, routing `/v1/orders`, `/v1/inventory`, `/v1/webhooks` |
| Kustomize | Base manifests + `local`, `ci` and `azure` overlays |
| Argo CD | GitOps: the cluster pulls its desired state from the repo |
| KEDA | Autoscale consumers on Kafka consumer lag |
| GitHub Actions | Lint, test, scan, build, deploy to a throwaway cluster, end-to-end tests |
| GitHub Container Registry (ghcr.io) | Docker images |
| Trivy | Image and dependency vulnerability scan, SBOM, in CI |
| Cloudflare Quick Tunnel | Public HTTPS demo URL with no account and no domain |
| Azure for Students (optional) | Always-on cloud host, $100 credit, no card |

### Observability and testing

| Technology | Used for |
| --- | --- |
| kube-prometheus-stack (Prometheus, Grafana, Alertmanager) | Metrics, dashboards, alerts |
| Grafana Tempo + OpenTelemetry Collector | Stores and shows distributed traces |
| Strimzi Kafka Exporter | Consumer lag per group and partition |
| pytest + pytest-asyncio | Unit tests |
| Testcontainers | Real Postgres, Redis and Kafka in integration tests |
| jsonschema | Event contract tests |
| Locust | Load tests |
| ruff + mypy + pre-commit | Lint, format, type checks |

**Stretch (only after the core is done):** Loki for log search, Debezium CDC to replace the polling outbox relay.

## 4. Database design

One Postgres instance holds three schemas, and each service owns exactly one. A service never reads another service's schema; it learns about other data only through Kafka events.

**Rules that hold across all schemas**

- Every business table has a `tenant_id` column, protected by row-level security (RLS).
- No foreign keys cross schemas. `tenant_id` and `order_id` are plain UUIDs outside their home schema.
- Each service connects as its own Postgres role, granted rights on its own schema only.
- Every schema has its own `outbox_events` (events to publish) and `processed_events` (events already consumed).

### Schema `orders` (owned by Order service)

```sql
CREATE TABLE orders.tenants (
  id          uuid PRIMARY KEY,
  name        text NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE orders.api_clients (          -- partner credentials
  id           uuid PRIMARY KEY,
  tenant_id    uuid NOT NULL REFERENCES orders.tenants(id),
  client_id    text NOT NULL UNIQUE,
  secret_hash  text NOT NULL,               -- argon2 hash, never the secret
  scopes       text[] NOT NULL,             -- e.g. {orders:write,inventory:read}
  revoked_at   timestamptz
);

CREATE TABLE orders.orders (
  id               uuid PRIMARY KEY,
  tenant_id        uuid NOT NULL,
  status           text NOT NULL CHECK (status IN
                   ('PENDING','RESERVED','CONFIRMED','SHIPPED','REJECTED','CANCELLED')),
  idempotency_key  text NOT NULL,
  request_hash     text NOT NULL,           -- detects same key, different body
  failure_reason   text,
  version          int  NOT NULL DEFAULT 0,
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, idempotency_key)
);

CREATE TABLE orders.order_items (
  id         uuid PRIMARY KEY,
  order_id   uuid NOT NULL REFERENCES orders.orders(id),
  tenant_id  uuid NOT NULL,
  sku        text NOT NULL,
  qty        int  NOT NULL CHECK (qty > 0)
);
```

### Schema `inventory` (owned by Inventory service)

```sql
CREATE TABLE inventory.warehouses (
  id uuid PRIMARY KEY, tenant_id uuid NOT NULL,
  code text NOT NULL, name text NOT NULL, location text,
  UNIQUE (tenant_id, code)
);

CREATE TABLE inventory.products (
  id uuid PRIMARY KEY, tenant_id uuid NOT NULL,
  sku text NOT NULL, name text NOT NULL,
  UNIQUE (tenant_id, sku)
);

CREATE TABLE inventory.stock_levels (
  id            uuid PRIMARY KEY,
  tenant_id     uuid NOT NULL,
  product_id    uuid NOT NULL REFERENCES inventory.products(id),
  warehouse_id  uuid NOT NULL REFERENCES inventory.warehouses(id),
  on_hand       int  NOT NULL CHECK (on_hand >= 0),
  reserved      int  NOT NULL CHECK (reserved >= 0),
  version       int  NOT NULL DEFAULT 0,      -- optimistic locking
  CHECK (reserved <= on_hand),                -- last line of defence against overselling
  UNIQUE (product_id, warehouse_id)
);

CREATE TABLE inventory.reservations (
  id            uuid PRIMARY KEY,
  tenant_id     uuid NOT NULL,
  order_id      uuid NOT NULL,
  product_id    uuid NOT NULL,
  warehouse_id  uuid NOT NULL,
  qty           int  NOT NULL CHECK (qty > 0),
  status        text NOT NULL CHECK (status IN ('ACTIVE','RELEASED','COMMITTED')),
  created_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (order_id, product_id, warehouse_id)  -- a redelivered event cannot reserve twice
);
```

### Schema `notifications` (owned by Notification service)

```sql
CREATE TABLE notifications.webhook_subscriptions (
  id uuid PRIMARY KEY, tenant_id uuid NOT NULL,
  url text NOT NULL, event_types text[] NOT NULL,
  signing_secret text NOT NULL, active boolean NOT NULL DEFAULT true
);

CREATE TABLE notifications.webhook_deliveries (
  id               uuid PRIMARY KEY,
  tenant_id        uuid NOT NULL,
  subscription_id  uuid NOT NULL REFERENCES notifications.webhook_subscriptions(id),
  event_id         uuid NOT NULL,
  status           text NOT NULL CHECK (status IN ('PENDING','DELIVERED','FAILED')),
  attempts         int  NOT NULL DEFAULT 0,
  next_attempt_at  timestamptz,
  last_error       text,
  UNIQUE (subscription_id, event_id)
);
```

### Shared shape, created in every schema

```sql
CREATE TABLE <schema>.outbox_events (
  id              bigserial PRIMARY KEY,     -- publish order
  event_id        uuid NOT NULL UNIQUE,
  tenant_id       uuid NOT NULL,
  aggregate_id    uuid NOT NULL,             -- becomes the Kafka message key
  event_type      text NOT NULL,
  payload         jsonb NOT NULL,
  headers         jsonb NOT NULL DEFAULT '{}',  -- traceparent, saved at write time
  created_at      timestamptz NOT NULL DEFAULT now(),
  published_at    timestamptz
);
CREATE INDEX ON <schema>.outbox_events (id) WHERE published_at IS NULL;

CREATE TABLE <schema>.processed_events (   -- the "inbox": makes consumers idempotent
  consumer      text NOT NULL,
  event_id      uuid NOT NULL,
  processed_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (consumer, event_id)
);
```

### The stock reservation query (the heart of "no overselling")

```sql
UPDATE inventory.stock_levels
SET    reserved = reserved + :qty,
       version  = version + 1
WHERE  product_id   = :product_id
  AND  warehouse_id = :warehouse_id
  AND  version      = :expected_version
  AND  on_hand - reserved >= :qty;
-- 1 row updated  -> reserved
-- 0 rows updated -> re-read the row; retry up to 3 times, then emit StockReservationFailed
```

The `on_hand - reserved >= :qty` condition is evaluated under Postgres's row lock, so two concurrent updates can never both take the last unit. The `version` check catches any other change made between your read and your write. The `CHECK (reserved <= on_hand)` constraint rejects the write even if application code has a bug.

### Row-level security (tenant isolation)

```sql
ALTER TABLE inventory.stock_levels ENABLE ROW LEVEL SECURITY;
ALTER TABLE inventory.stock_levels FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON inventory.stock_levels
  USING (tenant_id = current_setting('app.tenant_id')::uuid);
```

Repeat for every tenant-scoped table. At the start of every transaction the service runs `SET LOCAL app.tenant_id = '<tenant from JWT>'`. A query without that setting fails rather than returning other tenants' rows.

## 5. API and event design

Clients talk to SwiftFlow over REST; the services talk to each other only through Kafka events. Every write endpoint requires an `Idempotency-Key` header, and every endpoint except `/v1/auth/token` and the health checks requires a JWT.

### REST endpoints

| Method + path | Service | Scope | Notes |
| --- | --- | --- | --- |
| `POST /v1/auth/token` | Order | none | Client-credentials grant: `client_id` + `client_secret` in, 15-minute JWT out |
| `POST /v1/orders` | Order | `orders:write` | Returns `202 Accepted` + `order_id`, status `PENDING`; the saga runs asynchronously |
| `GET /v1/orders/{id}` | Order | `orders:read` | Current status + failure reason |
| `GET /v1/orders?status=&cursor=&limit=` | Order | `orders:read` | Cursor pagination, newest first |
| `POST /v1/orders/{id}/cancel` | Order | `orders:write` | Allowed until `SHIPPED`; releases stock |
| `POST /v1/orders/{id}/ship` | Order | `admin` | Marks shipped; inventory commits the reservation |
| `POST /v1/warehouses`, `POST /v1/products` | Inventory | `admin` | Catalogue setup |
| `POST /v1/inventory/adjustments` | Inventory | `admin` | Set or change `on_hand` for a SKU in a warehouse |
| `GET /v1/inventory/{sku}/availability` | Inventory | `inventory:read` | Per-warehouse `on_hand - reserved`; Redis cache, 5-second TTL |
| `POST /v1/webhooks/subscriptions` | Notification | `webhooks:write` | URL + event types; returns the signing secret once |
| `GET`, `DELETE /v1/webhooks/subscriptions/{id}` | Notification | `webhooks:write` | Manage subscriptions |
| `GET /v1/webhooks/deliveries?status=FAILED` | Notification | `webhooks:read` | Inspect failed deliveries |
| `POST /v1/webhooks/deliveries/{id}/retry` | Notification | `webhooks:write` | Manual replay |
| `GET /healthz`, `GET /readyz`, `GET /metrics` | All | none | Liveness, readiness (DB + Kafka reachable), Prometheus |

### Conventions

- **Idempotency.** Same key + same body → the stored response is returned, nothing runs twice. Same key + different body → `422`. Keys live 24 hours in Redis; the `UNIQUE (tenant_id, idempotency_key)` constraint is the backstop if Redis loses them.
- **Errors.** RFC 9457 `application/problem+json` bodies with a stable `type` URI per error.
- **Rate limits.** Token bucket per tenant in Redis; over the limit → `429` with `Retry-After`.
- **Tracing.** W3C `traceparent` header accepted and returned; the same trace ID travels in Kafka message headers.

### Kafka topics

| Topic | Message key | Producer | Consumers | Event types |
| --- | --- | --- | --- | --- |
| `orders.events` | `order_id` | Order | Inventory, Notification | `OrderPlaced`, `OrderConfirmed`, `OrderCancelled`, `OrderShipped` |
| `inventory.events` | `order_id` | Inventory | Order, Notification | `StockReserved`, `StockReservationFailed`, `StockReleased`, `StockCommitted` |
| `inventory.stock-changes` | `tenant_id:sku` | Inventory | Notification | `StockLevelChanged` (drives channel-sync webhooks) |
| `<topic>.dlq` | original key | Any consumer | You, via Grafana alert | Original message + `error`, `attempts`, `consumer` headers |

Keying by `order_id` keeps every event of one order on one partition, so a consumer sees them in order. Keying stock changes by `tenant_id:sku` does the same per SKU. Use 6 partitions per topic, so up to 6 consumer pods per group can share the work. A single broker means replication factor 1; say in interviews that production would use 3 brokers with `min.insync.replicas=2`.

### Event envelope

```json
{
  "event_id": "7f9c2b0e-…",
  "event_type": "StockReserved",
  "schema_version": 1,
  "occurred_at": "2026-11-14T10:15:00Z",
  "tenant_id": "…",
  "aggregate_id": "<order_id>",
  "payload": { "items": [{ "sku": "TSHIRT-M", "warehouse_id": "…", "qty": 2 }] }
}
```

Each event type has a JSON Schema file in `libs/contracts/`. Producers validate before writing to the outbox; consumer tests validate sample messages against the same files. That is the contract test.

## 6. Authentication and security

Partners authenticate with the OAuth2 client-credentials grant and receive a short-lived JWT signed by the Order service. The other services verify it with a public key, so no service shares a signing secret.

### Token flow

1. An admin creates a client with `swiftflow-admin create-client --tenant acme --role partner`. The CLI prints `client_id` and `client_secret` once and stores only the argon2 hash.
2. The partner calls `POST /v1/auth/token` with those credentials.
3. The Order service returns a JWT signed with an Ed25519 private key, valid 15 minutes. There are no refresh tokens: a client simply asks for a new one.
4. Inventory and Notification fetch the public key from `GET /.well-known/jwks.json` (cached) and verify every request locally.

**JWT claims:** `iss`, `aud`, `sub` (client\_id), `tenant_id`, `scope`, `iat`, `exp`, `jti`.

### Roles and scopes

| Role | Scopes granted | Who |
| --- | --- | --- |
| `admin` | everything, including `admin` | You, seed scripts, warehouse operations |
| `partner` | `orders:read`, `orders:write`, `inventory:read`, `webhooks:read`, `webhooks:write` | Integrating stores and marketplaces |

Each endpoint declares its scope with one FastAPI dependency, e.g. `Depends(require_scope("orders:write"))`. The tenant always comes from the token, never from the request body, and is set with `SET LOCAL app.tenant_id` for row-level security.

### Webhook security

- **Signing.** Every delivery carries `SwiftFlow-Signature: t=<unix time>,v1=<HMAC-SHA256 of t + "." + body>`. Receivers reject signatures older than 5 minutes, which blocks replays.
- **SSRF protection.** Subscription URLs must be `https`, and the resolved IP must not be private, loopback or link-local. Check again at delivery time, not only at subscription time.

### Secrets

- Local: a gitignored `.env` file; commit only `.env.example`.
- Cluster: Kubernetes Secrets created by Terraform from a gitignored `terraform.tfvars`.
- CI: GitHub Actions secrets; the CI cluster generates throwaway keys on every run.

### Supply chain and runtime hardening

- Pin every third-party GitHub Action to a full commit SHA, not a tag. In March 2026 attackers [force-pushed 76 of 77 version tags of `aquasecurity/trivy-action`](https://osv.dev/vulnerability/GHSA-69fq-xp46-6x23) to steal CI secrets; SHA-pinned workflows were unaffected.
- Dependabot raises update PRs for actions, Python packages and base images.
- Trivy fails the build on any fixable `CRITICAL` vulnerability.
- Containers run as a non-root user, with a read-only root filesystem and all Linux capabilities dropped.
- A NetworkPolicy per namespace allows only the traffic in the architecture diagram (k3s enforces NetworkPolicies out of the box).

## 7. Local development setup

You build and debug everything on your laptop with Docker Compose, then run the same images on a local Kubernetes cluster (k3d). Nothing here needs an account or a card.

### Tools to install (all free)

Docker, uv, kubectl, k3d, Helm, Terraform, cloudflared, and Make. On Windows, run everything inside WSL2.

**Laptop memory:** 8 GB is enough for Docker Compose without the observability profile. The full k3d stack (Kafka, monitoring, tracing, Argo CD) needs about 16 GB. With less, use Compose day to day and let CI run the Kubernetes deployment (section 8).

### Repository layout (one monorepo)

```text
swiftflow/
├── services/
│   ├── order/            app/{api,domain,saga,consumers}, migrations/, tests/, Dockerfile
│   ├── inventory/
│   └── notification/
├── libs/
│   ├── common/           auth, DB session + RLS, outbox relay, consumer base, idempotency,
│   │                     circuit breaker, logging, tracing, metrics
│   └── contracts/        JSON Schema for every event type
├── deploy/
│   ├── compose/          docker-compose.yml, .env.example
│   ├── k8s/base/         Deployments, Services, Ingress, KEDA ScaledObjects, NetworkPolicies
│   ├── k8s/overlays/     local/, ci/, azure/
│   └── terraform/
│       ├── platform/     cluster + Helm releases: Strimzi, Postgres, Redis, monitoring, Tempo, KEDA, Argo CD
│       └── azure/        optional cloud VM and network
├── loadtest/             locustfile.py
├── chaos/                pod-kill scripts + invariant checker
├── scripts/              seed data, swiftflow-admin CLI
├── docs/                 architecture.md, adr/, runbooks/, results/
├── .github/workflows/    ci.yml, e2e.yml
└── Makefile
```

`libs/common` is a uv workspace package that all three services import, so the outbox, consumer and auth code is written once.

### One image, three processes per service

Each service ships one Docker image that starts in one of three modes:

| Mode | Command | Job |
| --- | --- | --- |
| `api` | `uvicorn app.main:app` | Serves REST requests |
| `relay` | `python -m app.relay` | Publishes unpublished outbox rows to Kafka |
| `consumer` | `python -m app.consumer` | Reads Kafka, updates the DB, writes its own outbox rows |

In Kubernetes each mode is its own Deployment, so consumers can scale on lag without scaling the API.

### Docker Compose stack

`deploy/compose/docker-compose.yml` runs Postgres 17, Redis, Kafka (official `apache/kafka` image, single KRaft node), and the 8 SwiftFlow processes. A `--profile observability` flag adds Prometheus, Grafana, Tempo and the OpenTelemetry Collector.

### Make targets

| Command | What it does |
| --- | --- |
| `make up` | Start the Compose stack |
| `make migrate` | Run Alembic for all three schemas |
| `make seed` | Create a demo tenant, 2 warehouses, 20 SKUs, an admin client and a partner client |
| `make test` | Unit + integration tests (Testcontainers) |
| `make k8s-up` | `terraform apply` in `deploy/terraform/platform`: creates a k3d cluster with everything installed |
| `make k8s-down` | Destroy the k3d cluster |
| `make tunnel` | `cloudflared tunnel --url http://localhost:8080`: a temporary public HTTPS URL for demos |
| `make load` | Run the Locust oversell scenario |
| `make chaos` | Kill pods mid-saga, then run the invariant checker |

Local URLs: API through Traefik at `http://localhost:8080`, Swagger UI at `/v1/orders/docs` (and the same path for each service), Grafana at `http://localhost:3000`.

## 8. CI/CD pipeline

Every pull request builds the images, deploys the whole platform to a throwaway Kubernetes cluster inside GitHub Actions, and proves the system works before merge. Keep the repository public: standard GitHub-hosted runners are [free and unlimited for public repositories](https://docs.github.com/en/actions/reference/runners/github-hosted-runners), with 4 CPUs and 16 GB of RAM on Linux.

### `ci.yml`: on every pull request and push to `main`

1. **Lint:** `ruff check`, `ruff format --check`, `mypy` (matrix over the 3 services).
2. **Test:** `pytest` unit + integration tests; Testcontainers starts real Postgres, Redis and Kafka. Fail under 80% line coverage.
3. **Contracts:** validate every sample event in `libs/contracts/examples/` against its schema.
4. **Build:** `docker buildx` one image per service, tagged with the commit SHA, with layer caching.
5. **Scan:** Trivy scans each image; fail on fixable `CRITICAL` findings; upload a CycloneDX SBOM as a build artifact.

### `e2e.yml`: after `ci.yml` passes

6. **Cluster:** `terraform apply` on `deploy/terraform/platform` with the kind provider: a fresh cluster with Strimzi Kafka, Postgres, Redis, Traefik, KEDA and Prometheus.
7. **Deploy:** `kind load docker-image` the new images, then `kubectl apply -k deploy/k8s/overlays/ci`; wait for every rollout.
8. **Prove it:** run the end-to-end pytest suite against Traefik, then a 60-second headless Locust oversell run, then the pod-kill chaos script, then the invariant checker (section 11).
9. **Report:** write the results table (orders placed, oversold count, p95 latency, invariant status) to the job summary and save it under `docs/results/`.

The runner is discarded at the end, so the cluster costs nothing and never drifts. Keep the whole job under 15 minutes.

### On merge to `main`

10. Push the images to `ghcr.io/<you>/swiftflow-<service>:<sha>` using the built-in `GITHUB_TOKEN`.
11. A job bound to a GitHub Environment named `production` waits for your manual approval. That is the release gate.
12. After approval, the job commits the new image tag to `deploy/k8s/overlays/local` and `overlays/azure`. Argo CD, running in your k3d cluster or the optional Azure VM, sees the commit and rolls the new version out.

### Pipeline rules

- Every third-party action is pinned to a full commit SHA (section 6).
- Workflows default to `permissions: contents: read`. Only the publish job gets `packages: write`, and only the tag-bump job gets `contents: write`.
- Cache `uv` downloads and Docker layers so a typical run stays fast.
- A failed E2E run uploads pod logs and a `kubectl get events` dump as artifacts, so you can debug without re-running.

## 9. Deployment

The same Terraform modules and Kustomize manifests deploy SwiftFlow to three places: your laptop (k3d), CI (kind, section 8), and optionally an Azure VM. None of them needs a credit card.

### Target A: local k3d cluster (required)

1. `make k8s-up` runs `terraform apply` in `deploy/terraform/platform`. It creates a k3d cluster named `swiftflow` with 1 server and 2 agent nodes, and maps `localhost:8080` to Traefik.
2. The same apply installs, through the Helm and Kubernetes providers:
   - Strimzi operator, then a `Kafka` resource with one dual-role (broker + controller) `KafkaNodePool` replica in KRaft mode, and `KafkaTopic` resources for every topic in section 5.
   - Postgres 17 as a StatefulSet using the official image, with a 5 GB volume. Avoid Bitnami charts: Bitnami [moved its free images to an unmaintained legacy repository in 2025](https://www.docker.com/blog/broadcoms-new-bitnami-restrictions-migrate-easily-with-docker/).
   - Redis as a Deployment using the official image.
   - kube-prometheus-stack, Grafana Tempo, the OpenTelemetry Collector and KEDA.
   - Argo CD, plus one `Application` that tracks `deploy/k8s/overlays/local` with auto-sync and self-heal.
   - Kubernetes Secrets for the DB passwords, JWT signing key and webhook secrets, read from `terraform.tfvars`.
3. Argo CD syncs the app. Alembic migrations run first as an Argo CD `PreSync` hook Job, so the API never starts against an old schema.
4. `make seed` creates the demo tenant and clients.
5. **Public demo:** `make tunnel` starts a [Cloudflare Quick Tunnel](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/trycloudflare/) and prints a random `https://….trycloudflare.com` URL. It needs no Cloudflare account or domain, and stops when you press Ctrl-C. Use it for interviews and demo videos, not as a permanent link.

The 2 agent nodes let you run a real node-failure test locally: `docker stop k3d-swiftflow-agent-1`.

### Target B: Azure for Students VM (optional, for an always-on URL)

[Azure for Students](https://azure.microsoft.com/en-us/free/students) gives full-time university students $100 of credit for 12 months, renewable each year while enrolled, with no credit card. Sign up with your SRM email.

1. `terraform apply` in `deploy/terraform/azure` creates a resource group, a virtual network, a network security group that opens only ports 80 and 443, a public IP with a free DNS label (`swiftflow-<you>.<region>.cloudapp.azure.com`), and one Ubuntu VM with 8 GB RAM, such as a B-series burstable size.
2. cloud-init on the VM installs k3s.
3. Run the `platform` module against the VM's kubeconfig, skipping the kind cluster resource. Add cert-manager with a Let's Encrypt issuer for HTTPS on the DNS label.
4. Argo CD tracks `deploy/k8s/overlays/azure`.

**Protect the credit:**

- Set budget alerts at 25%, 50% and 75% of the $100 in Cost Management.
- Deallocate the VM when you are not demoing (`az vm deallocate`); a deallocated VM stops charging for compute.
- Check the VM's hourly price for your region in the Azure pricing calculator before you create it, and work out how many hours the credit buys.
- With no card on file, nothing is billed beyond the credit: when it runs out, the subscription's services stop.

### Memory budget for one cluster (approximate requests)

| Component | Memory |
| --- | --- |
| k3s/k3d system + Traefik | \~0.8 GB |
| Kafka broker (1 GB heap) + Strimzi operator + exporter | \~2.0 GB |
| Postgres + Redis | \~0.7 GB |
| 8 SwiftFlow pods (\~150 MB each) | \~1.2 GB |
| Prometheus + Grafana + Alertmanager + exporters | \~1.5 GB |
| Tempo + OpenTelemetry Collector | \~0.5 GB |
| Argo CD + KEDA | \~0.9 GB |
| **Total** | **\~7.6 GB** |

This is why 16 GB is the laptop recommendation and 8 GB is the Azure VM floor. On an 8 GB VM, set Prometheus retention to 2 days and Kafka heap to 768 MB.

## 10. Monitoring and observability

You can answer "what happened to order X, and is the system healthy?" from Grafana alone. Metrics show the trend, traces show one order's path, and logs carry the same `trace_id` so you can jump between them.

### Custom metrics (exposed by every service on `/metrics`)

| Metric | Type | Why it matters |
| --- | --- | --- |
| `swiftflow_orders_total{status}` | counter | Order outcomes per minute |
| `swiftflow_saga_duration_seconds` | histogram | Time from `PENDING` to a final state |
| `swiftflow_reservation_conflicts_total` | counter | Optimistic-lock retries; rises under contention |
| `swiftflow_outbox_unpublished` and `swiftflow_outbox_oldest_age_seconds` | gauge | A stuck relay shows here first |
| `swiftflow_events_consumed_total{consumer,result}` | counter | `result` = `ok`, `duplicate`, `retry`, `dlq` |
| `swiftflow_webhook_deliveries_total{status}` | counter | Partner delivery health |
| `swiftflow_circuit_breaker_state{target}` | gauge | 0 closed, 1 half-open, 2 open |
| HTTP request count and latency by route and status | histogram | Rate, errors, duration (RED) per endpoint |
| `kafka_consumergroup_lag` (from Kafka Exporter) | gauge | Backlog per consumer group and partition; also drives KEDA |

### Grafana dashboards (JSON in `deploy/k8s/base/dashboards/`, provisioned automatically)

1. **Order flow:** orders per minute by status, saga p95, and an "oversold units" stat that must always read 0.
2. **Kafka:** lag per group and partition, messages in and out per topic, DLQ message count.
3. **Service health:** request rate, error rate and p95 latency per service; pod restarts; DB pool usage.
4. **Resilience:** outbox age, circuit breaker state, webhook retries.

Turn on exemplars, so a click on a latency spike opens the trace of a slow request in Tempo.

### Alerts (PrometheusRule)

| Alert | Condition |
| --- | --- |
| `ConsumerLagHigh` | lag > 1,000 messages for 5 minutes |
| `HighErrorRate` | 5xx responses > 2% of requests over 5 minutes |
| `OutboxStuck` | oldest unpublished outbox row > 60 seconds for 2 minutes |
| `DeadLetterReceived` | any new message on a `.dlq` topic |
| `SagaSlow` | saga p95 > 10 seconds for 10 minutes |
| `CircuitOpen` | a circuit breaker open for more than 2 minutes |

Alertmanager can post to a Discord channel through a webhook, which is free; otherwise read the alerts in Grafana.

### Distributed tracing

- Auto-instrument FastAPI, SQLAlchemy and httpx with the OpenTelemetry SDK; export over OTLP to the Collector, which forwards to Tempo.
- When a service writes an outbox row, store the current `traceparent` in the row's `headers`. The relay copies it into the Kafka message headers, and the consumer starts its span from it. This link is what makes one order show up as one trace, even though the relay publishes later.
- Put `trace_id` into every structlog line so logs, metrics and traces connect.

## 11. Testing strategy

The tests exist to prove the five claims in section 1, so every claim has a named test that fails loudly when it breaks. CI runs all of them on every pull request.

### Test layers

| Layer | Tool | Runs in | What it covers |
| --- | --- | --- | --- |
| Unit | pytest | `ci.yml` | Order state machine (every allowed and forbidden transition), circuit breaker, idempotency hashing, scope checks, webhook signatures |
| Integration | pytest + Testcontainers | `ci.yml` | Real Postgres, Redis and Kafka: concurrency, outbox, RLS, DLQ (scenarios below) |
| Contract | jsonschema | `ci.yml` | Every event a producer emits matches the schema its consumers test against |
| End-to-end | pytest over HTTP | `e2e.yml` | Full saga on the kind cluster |
| Load | Locust | `e2e.yml` + local | No overselling under concurrency; latency |
| Chaos | shell script + invariant checker | `e2e.yml` + local | Pod and broker failures mid-saga |

### Integration scenarios (must exist)

- **Concurrent reservation:** 200 concurrent reservations of 1 unit against 50 in stock leave exactly 50 `ACTIVE` reservations and `reserved = 50`.
- **Redelivered event:** the same `OrderPlaced` delivered twice creates one reservation, and the second is counted as `duplicate`.
- **Relay crash:** write 1,000 outbox rows, kill the relay halfway, restart it. All 1,000 `event_id`s reach Kafka; duplicates are allowed, losses are not.
- **Poison message:** a malformed payload is retried 3 times, lands on the `.dlq` topic with error headers, and the consumer keeps processing the next messages.
- **Idempotency key:** same key + same body returns the same response and creates one order; same key + different body returns `422`.
- **Tenant isolation:** a session for tenant A reads zero rows of tenant B; a session with no `app.tenant_id` set gets an error.

### End-to-end scenarios

A small `payment-sim` container (about 30 lines of FastAPI) stands in for a payment provider. It takes a configurable failure rate and delay, and records every charge so tests can check for double charges.

- Happy path: order reaches `CONFIRMED` within 10 seconds, then `SHIPPED` after the ship call.
- Payment fails: order ends `CANCELLED`, and the stock is available again.
- Out of stock: order ends `REJECTED` with a reason.
- Webhook: a test receiver gets `OrderConfirmed` with a valid signature.
- Payment provider down: the circuit opens, orders wait, and they complete once `payment-sim` recovers.

### Load test: the oversell proof

Seed one SKU with 100 units. Locust runs 500 users who each place a 1-unit order within 60 seconds, each with a unique idempotency key, and payment success set to 100%.

**Pass:** exactly 100 orders `CONFIRMED`, 400 `REJECTED`, `reserved = 100`, oversold units = 0. Record throughput and p95 latency in `docs/results/`.

A second scenario runs steady traffic for 5 minutes to show KEDA adding consumer pods as lag grows.

### Chaos test

`chaos/kill_mid_saga.sh` starts the load test, then every 10 seconds deletes a random pod from the Order consumer, Inventory consumer and Order relay. It also deletes the Kafka broker pod once. Locally, it can also stop a whole k3d agent node. After traffic stops and queues drain, it runs the invariant checker.

### Invariant checker (`chaos/check_invariants.py`)

Each check is one SQL query or API call, and any failure exits non-zero, which fails CI.

1. No stock row has `reserved > on_hand` or a negative value.
2. For every stock row, `reserved` equals the sum of its `ACTIVE` reservations.
3. Every order older than 2 minutes is in `CONFIRMED`, `SHIPPED`, `REJECTED` or `CANCELLED`.
4. `CANCELLED` and `REJECTED` orders hold no `ACTIVE` reservations.
5. `payment-sim` recorded at most one successful charge per order (Order sends `order_id` as the payment idempotency key).
6. No unpublished outbox rows remain, and every `.dlq` message is accounted for.

## 12. Build roadmap

Build in 8 phases over 12 weeks. Each phase ends with a pass/fail exit check, and you do not start the next phase until it passes. The correctness core (phases 1–3) comes before Kubernetes on purpose: it is what interviews probe hardest.

&#91;embedded content: build roadmap · 8 phases over 12 weeks\]

Tick the boxes below as you go; each phase's exit check is the gate to the next.

### Phase 0 — Foundation (week 1)

- [ ] Public GitHub repo, monorepo layout from section 7, uv workspace
- [ ] FastAPI skeletons for the 3 services with `/healthz`, `/readyz`, `/metrics`
- [ ] Docker Compose with Postgres, Redis, Kafka and the services
- [ ] `ci.yml` with lint and unit tests; every action SHA-pinned; Dependabot on
- [ ] `docs/adr/0001-architecture.md`: why microservices, Kafka, schema-per-service

**Exit check:** `make up` brings every health check green, and CI passes on the first pull request.

### Phase 1 — Inventory core (weeks 2–3)

- [ ] `inventory` schema and Alembic migrations
- [ ] Warehouse, product, adjustment and availability endpoints
- [ ] Reservation query with optimistic locking and up to 3 retries
- [ ] Redis availability cache with a 5-second TTL
- [ ] Concurrent-reservation integration test

**Exit check:** 200 concurrent reservations against 50 units leave exactly 50 reserved.

### Phase 2 — Orders, idempotency and outbox (weeks 4–5)

- [ ] `orders` schema; `POST` and `GET` order endpoints
- [ ] Idempotency keys in Redis, backed by the unique constraint
- [ ] Outbox relay: `SELECT … FOR UPDATE SKIP LOCKED`, batches of 100, producer with `acks=all` and idempotence on
- [ ] Shared consumer base in `libs/common`: inbox check, retries with backoff, DLQ
- [ ] JSON Schemas for every event, plus contract tests

**Exit check:** the relay-crash, redelivered-event and poison-message tests pass.

### Phase 3 — Saga and notifications (weeks 6–7)

- [ ] Order state machine and saga steps in the Order consumer
- [ ] `payment-sim`, the hand-written circuit breaker, retries with jitter
- [ ] Compensation: payment failure or cancel releases the reservation
- [ ] Ship flow commits the reservation
- [ ] Notification service: subscriptions, signed webhooks, retry schedule, SSRF guard

**Exit check:** happy-path, payment-failure and out-of-stock end-to-end tests pass on Compose.

### Phase 4 — Auth, tenancy and rate limits (week 8)

- [ ] `api_clients`, token endpoint, Ed25519-signed JWT, JWKS endpoint
- [ ] Scope checks on every endpoint; RLS on every tenant table
- [ ] Per-tenant token-bucket rate limiter
- [ ] `swiftflow-admin` CLI for tenants and clients

**Exit check:** the tenant-isolation test passes; missing token gives `401`, wrong scope gives `403`, too many requests give `429`.

### Phase 5 — Kubernetes, Terraform and CI/CD (weeks 9–10)

- [ ] Kustomize base and `local`, `ci`, `azure` overlays
- [ ] Terraform `platform` module: cluster, Strimzi Kafka, Postgres, Redis, Argo CD, KEDA
- [ ] `e2e.yml` on kind; Trivy scan and SBOM; push to ghcr.io
- [ ] `production` environment approval gate; Argo CD syncing from Git

**Exit check:** a pull request deploys to kind and passes end-to-end tests in under 15 minutes; an approved merge rolls out to your k3d cluster with no manual `kubectl`.

### Phase 6 — Observability and autoscaling (week 11)

- [ ] kube-prometheus-stack, the 4 dashboards, the 6 alerts
- [ ] OpenTelemetry tracing, Tempo, `traceparent` carried through the outbox
- [ ] KEDA `ScaledObject` per consumer, scaling on lag

**Exit check:** one order shows as a single trace across all 3 services, and the steady-load test adds consumer pods.

### Phase 7 — Prove and publish (week 12)

- [ ] Oversell load test, chaos script and invariant checker running in `e2e.yml`
- [ ] Results tables committed to `docs/results/`
- [ ] README: architecture diagram, results, one-command quick start, links to ADRs
- [ ] 3-minute demo video recorded through the Cloudflare tunnel
- [ ] Optional: Azure for Students VM via Terraform

**Exit check:** the README shows green results from CI, and someone else can run `make up` and `make k8s-up` from a clean clone.

## 13. Cost breakdown

Total cost: $0, and no step asks for a credit or debit card. The only accounts are GitHub (required) and Azure for Students (optional, verified with your university email).

| Component | Why it is free | Limit to watch | Card? |
| --- | --- | --- | --- |
| GitHub repo, Actions, ghcr.io | Standard hosted runners are free and unlimited on public repositories | Keep the repo public | No |
| Docker, k3d, kind, kubectl, Helm, Terraform, cloudflared | Open source | Docker Desktop's free licence covers personal and education use; on Linux or WSL2 you can use Docker Engine instead | No |
| Kafka, Strimzi, Postgres, Redis, Prometheus, Grafana, Tempo, Argo CD, KEDA, Trivy | Open source, running in your own cluster | Your laptop's RAM (section 9) | No |
| Cloudflare Quick Tunnel | No account needed | Random URL, no uptime guarantee, ends when the process stops | No |
| Azure for Students (optional) | $100 credit for 12 months, renewable while enrolled | Credit runs out if the VM runs 24/7; deallocate it when idle | No |

### What would cost money (avoid these)

- Making the repository private: private repos draw on a monthly Actions minutes quota.
- Upgrading Azure for Students to pay-as-you-go, which asks for a card.
- Paid add-ons or Azure Marketplace offers, which the student credit does not cover.

### Options ruled out, and why

- **Oracle Cloud Always Free:** sign-up [requires a credit or debit card for identity verification](https://www.issoh.co.jp/tech/details/17531/). Oracle also [halved the free Arm allowance to 2 OCPU and 12 GB in 2026](https://docs.oracle.com/en-us/iaas/Content/FreeTier/resourceref.htm).
- **Neon free Postgres:** the free plan gives [100 CU-hours per project per month](https://neon.com/faqs/free-plan-limits-and-quotas), and compute [only suspends after 5 minutes with no active queries](https://neon.com/docs/introduction/compute-lifecycle). The outbox relay polls every second, so the database never sleeps. At 0.25 CU running all month (about 730 hours) that is about 182 CU-hours, which would exhaust the quota partway through the month.
- **AWS, GCP, standard Azure free trials:** all ask for a card at sign-up.

## 14. Resume bullets and interview prep

Put your own measured numbers from `docs/results/` into the bullets; never quote numbers you have not produced.

### Resume entry

**SwiftFlow — Distributed Order Fulfillment Platform** · Python, FastAPI, Kafka, PostgreSQL, Redis, Kubernetes, Terraform, Argo CD, OpenTelemetry · `github.com/<you>/swiftflow`

- Built a multi-tenant, event-driven order fulfillment platform of 3 microservices on Kafka, using the transactional outbox, idempotent consumers and an orchestrated saga with compensating transactions.
- Prevented overselling with optimistic locking and conditional updates in PostgreSQL: 0 units oversold across 500 concurrent orders for 100 units, with all invariants holding under scripted pod and broker failures.
- Provisioned the platform with Terraform and shipped it via GitOps (Argo CD); every pull request deploys to a throwaway Kubernetes cluster in GitHub Actions for end-to-end, load and chaos tests, gated by Trivy scans and SHA-pinned actions.
- Traced each order across HTTP and Kafka with OpenTelemetry and Grafana Tempo; autoscaled consumers on Kafka lag with KEDA.

Say **"effectively-once processing"**, not "exactly-once". Delivery is at-least-once and the consumers are idempotent; interviewers often test whether you know the difference.

### Likely interview questions

| Question | Core of your answer | Section |
| --- | --- | --- |
| How do you prevent overselling when two orders hit the same SKU? | Conditional `UPDATE` evaluated under Postgres's row lock, a `version` check, and a `CHECK` constraint as backstop. Redis is not part of correctness. | 4 |
| What happens if Inventory crashes mid-saga? | Its Kafka offset was never committed, so the event is redelivered after restart; the inbox table drops duplicates; the saga continues. The chaos test proves it. | 11 |
| Why Kafka instead of calling Inventory directly? | Decoupling, buffering during outages, replay, independent scaling. The cost is eventual consistency and more moving parts. | 2, 5 |
| How do you handle a poison-pill message? | 3 bounded retries with backoff, then the `.dlq` topic with error headers, an alert, and manual replay after the fix. | 5, 10 |
| How does the outbox pattern solve the dual-write problem? | The business row and the event row commit in one transaction; a relay publishes afterwards, at least once. | 4 |
| Is it exactly-once? | No: at-least-once delivery plus idempotent consumers. Kafka's transactions give exactly-once only inside Kafka, not across a database. | 4, 11 |
| Why not a Redis distributed lock (Redlock)? | Lock leases can expire during a GC pause or network delay, letting two holders proceed. The database already serializes the write, so a lock adds risk without adding safety. | 4 |
| Orchestration or choreography for the saga? | Orchestration in the Order service: one place shows each order's state, which makes failures easy to debug. The trade-off is that Order knows every step. | 2 |
| How is one tenant isolated from another? | Row-level security with `FORCE`, `SET LOCAL app.tenant_id` from the JWT, and a separate DB role per service. | 4, 6 |
| What breaks first under load, and how would you know? | The single broker and single Postgres instance. Lag and pool-saturation dashboards show it; consumers scale only up to the partition count (6). | 9, 10 |
| How would this scale to multiple regions? | Give each region ownership of its warehouses' stock, replicate events between regions, and route orders to the owning region. Trade-off: cross-region orders get slower. | — |

## Sources

- [GitHub-hosted runners reference](https://docs.github.com/en/actions/reference/runners/github-hosted-runners) — free, unlimited standard runners on public repos
- [Azure for Students](https://azure.microsoft.com/en-us/free/students) — $100 credit, no credit card
- [Cloudflare Quick Tunnels](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/trycloudflare/) — no account or domain needed
- [Oracle Always Free resources](https://docs.oracle.com/en-us/iaas/Content/FreeTier/resourceref.htm) — 2026 Arm limits
- [Oracle Cloud Free Tier sign-up requirements (issoh.co.jp)](https://www.issoh.co.jp/tech/details/17531/) — card required
- [Neon Free plan limits](https://neon.com/faqs/free-plan-limits-and-quotas) and [compute lifecycle](https://neon.com/docs/introduction/compute-lifecycle)
- [Docker blog on Bitnami catalog changes](https://www.docker.com/blog/broadcoms-new-bitnami-restrictions-migrate-easily-with-docker/)
- [Trivy supply chain advisory GHSA-69fq-xp46-6x23](https://osv.dev/vulnerability/GHSA-69fq-xp46-6x23)
- [Strimzi 0.46.0 release notes](https://newreleases.io/project/github/strimzi/strimzi-kafka-operator/release/0.46.0) — ZooKeeper support removed, KRaft only
