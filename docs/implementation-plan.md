# Implementation plan

**Status (2026-11-15): phases 1–7 are done**, except OpenTelemetry tracing end to end in Jaeger and the Grafana dashboard, which are wired but not yet verified. Phase 8: the CI workflow is written but has not yet run on GitHub; Kubernetes is not started.

The build order for turning these docs into the working system. Each phase ends with an exit check that must pass before the next phase starts. The correctness core comes before the UI and the platform work on purpose: it is what interviewers probe hardest.

## Target repository layout

```text
Swiftflow/
├── README.md
├── swiftflow.ps1                 task runner (Windows)    · Makefile (CI / WSL)
├── requirements-dev.lock.txt
├── libs/
│   ├── common/                   swiftflow_common (pyproject.toml, tests/)
│   └── contracts/                schemas/*.v1.json, examples/*.json, tests/
├── services/
│   ├── gateway/                  app/main.py, routes.py, health.py · Dockerfile · requirements.txt · tests/
│   ├── order/                    app/{main,config,worker,auth,orders,saga,payments,repository}.py · migrations/ · tests/
│   ├── inventory/                app/{main,config,worker,catalog,stock,reservations}.py · migrations/ · tests/
│   ├── notification/             app/{main,config,worker,feed,stream,webhooks,dispatcher,ssrf}.py · migrations/ · tests/
│   └── partner-sim/              app/main.py · tests/
├── web/                          Vite + React console (see ui-design.md)
├── deploy/
│   ├── compose/                  docker-compose.yml, .env.example, postgres/init.sql, prometheus/, grafana/
│   └── k8s/                      base/ + overlays/{local,ci} (Kustomize)
├── scripts/                      seed.py, admin.py (swiftflow-admin CLI), dev.ps1
├── loadtest/                     oversell.py, locustfile.py
├── chaos/                        kill_mid_saga.ps1, check_invariants.py
├── tests/                        integration/, e2e/
├── docs/                         this documentation, adr/, results/
└── .github/workflows/ci.yml
```

## Phase 1 — Foundation

- [ ] `libs/common`: config, db (pool, `tenant_tx`, `system_tx`), migrate, problems, observability (logging + metrics middleware), health routes
- [ ] `deploy/compose`: Postgres (with `init.sql` roles and schemas), Redis, Kafka KRaft with host and internal listeners
- [ ] Skeletons for all six services with `/healthz`, `/readyz`, `/metrics`, each on its port
- [ ] `swiftflow.ps1` with `infra`, `up`, `down`, `dev`, `ps`, `logs`

**Exit check:** `swiftflow.ps1 infra` + `swiftflow.ps1 dev` brings all services up on 8001–8004 and 8080; every `/readyz` is green.

## Phase 2 — Inventory core

- [ ] Migrations: warehouses, products, stock_levels, reservations, adjustments, outbox, inbox, RLS
- [ ] Catalogue, adjustment (with `If-Match`), availability (Redis cache) and stock-grid endpoints
- [ ] Reservation, release and commit functions (the SQL in database.md)
- [ ] `test_concurrent_reservation`, `test_multi_item_all_or_nothing`, `test_tenant_isolation`

**Exit check:** 200 concurrent reservations against 50 units leave exactly 50 reserved.

## Phase 3 — Events: outbox, relay, consumer, contracts

- [ ] `libs/contracts` schemas and examples for all nine event types; contract tests
- [ ] `outbox.add_event` with validation and `traceparent`; `OutboxRelay` (SKIP LOCKED, batch 100, idempotent producer)
- [ ] `EventConsumer` (inbox, retries with jitter, DLQ, manual commit, metrics); topic bootstrap
- [ ] Inventory worker handling `OrderPlaced`, `OrderCancelled`, `OrderShipped`

**Exit check:** relay-crash, redelivered-event and poison-message tests pass.

## Phase 4 — Orders, auth and the saga

- [ ] Order migrations; `api_clients`, `signing_keys`; token endpoint; JWKS; `require_scope`
- [ ] Idempotency store and per-tenant rate limiter in `libs/common`
- [ ] Order endpoints (create, list with cursor, get with history, stats, cancel, ship)
- [ ] Order worker: relay, `order-saga` consumer, payment processor with lease and circuit breaker
- [ ] Partner-sim: payments with idempotency, config controls, webhook receiver with signature check
- [ ] `scripts/seed.py` and `scripts/admin.py`

**Exit check:** happy path, payment declined and out-of-stock all reach the right final state via HTTP against the services directly.

## Phase 5 — Notification and gateway

- [ ] Notification migrations; feed consumer; Redis fan-out; SSE stream; subscriptions; dispatcher with signing, retry schedule and SSRF guard
- [ ] Gateway: path routing, streaming proxy, request IDs, CORS, problem responses, `/v1/system/health`
- [ ] Full Compose stack: every service containerised with health checks and start-up ordering

**Exit check:** `swiftflow.ps1 up` + `seed` from a clean clone; an order placed through `:8080` produces a signed webhook that the simulator marks valid.

## Phase 6 — Web console

- [ ] Tokens, fonts, shell (rail, top bar, health strip), sign-in with demo clients
- [ ] `SagaRail`, `StatusBadge`, `BinCard`, `EventTape`, `Kpi`, drawer and dialog primitives
- [ ] Screens: Overview, Orders (+ drawer, new order), Stock (+ adjust), Webhooks, System map, Chaos Lab (+ oversell test)
- [ ] nginx image for Docker; Vite proxy for dev

**Exit check:** the full demo script in [interview-guide.md](interview-guide.md) runs from the console alone, in light and dark themes, at 360 px and 1440 px.

## Phase 7 — Prove it

- [ ] `loadtest/oversell.py`, `loadtest/locustfile.py`
- [ ] `chaos/kill_mid_saga.ps1`, `chaos/check_invariants.py`
- [ ] e2e test suite through the gateway
- [ ] OpenTelemetry tracing across HTTP and Kafka; `observability` Compose profile with a provisioned Grafana dashboard
- [ ] Results written to `docs/results/`

**Exit check:** oversell test shows 100 / 400 / 0 oversold; chaos run ends with all invariants green; one order appears as one trace in Jaeger.

## Phase 8 — Platform and polish (optional, after the core works)

- [x] `.github/workflows/ci.yml` (SHA-pinned actions, lint, tests, build, Trivy, Compose e2e) — written, not yet run on GitHub
- [ ] Kustomize base + overlays; install k3d/kind/helm to `D:\Apps` and deploy locally
- [ ] README results table and screenshots; demo recording

**Exit check:** CI is green on a pull request, and someone else can run `swiftflow.ps1 up` from a clean clone.
