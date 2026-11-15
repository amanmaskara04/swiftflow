# Local development

Two ways to run SwiftFlow. Both start every service on its own port; the difference is where the services run.

| Mode | Infra (Postgres, Redis, Kafka) | Services | Best for |
|------|-------------------------------|----------|----------|
| **A. Full Docker** | Docker | Docker | Demos, "does it all work", closest to production |
| **B. Hybrid** | Docker | On the host, one terminal per service | Day-to-day coding with auto-reload and debuggers |

## Prerequisites

Everything is installed on **D:** (see [environment.md](environment.md)).

| Tool | Location | Used for |
|------|----------|----------|
| Docker Desktop 29 + Compose v5 | `D:\tools\Docker` | Infra and full stack |
| Python 3.12.10 | `D:\tools\Python312` | Base interpreter for the venv |
| Project venv | `D:\Portfolio\Swiftflow\.venv` | All backend libraries |
| Node 24 + npm 11 | system install (cache `D:\cache\npm`) | Web console |

Docker Desktop must be running (`docker info` succeeds). Give it at least 6 GB of memory; it currently has 8 GB.

## Task runner

`swiftflow.ps1` at the repo root wraps every common command (a `Makefile` with the same targets exists for WSL and CI).

| Command | What it does |
|---------|--------------|
| `.\swiftflow.ps1 up` | Build images and start the full stack (mode A) |
| `.\swiftflow.ps1 up -Observability` | Same, plus Prometheus, Grafana and Jaeger |
| `.\swiftflow.ps1 down` | Stop the stack, keep data |
| `.\swiftflow.ps1 reset` | Stop and delete all volumes |
| `.\swiftflow.ps1 infra` | Start only Postgres, Redis and Kafka (mode B) |
| `.\swiftflow.ps1 dev` | Start every service on the host in its own terminal window (mode B) |
| `.\swiftflow.ps1 seed` | Create demo tenants, clients, warehouses, products and stock (safe to re-run) |
| `.\swiftflow.ps1 test` | Lint, unit and contract tests (no infra needed) |
| `.\swiftflow.ps1 test-int` | Integration, security and end-to-end tests against the running, seeded stack |
| `.\swiftflow.ps1 load` | Oversell proof: 500 concurrent orders against 100 units |
| `.\swiftflow.ps1 chaos` | Kill worker containers mid-saga, then run the invariant checker |
| `.\swiftflow.ps1 check` | Invariant checker only |
| `.\swiftflow.ps1 logs [service]` | Follow logs |
| `.\swiftflow.ps1 ps` | Show every container and port |

## Mode A — full Docker

```powershell
cd D:\Portfolio\Swiftflow
copy deploy\compose\.env.example deploy\compose\.env   # first time only
.\swiftflow.ps1 up
.\swiftflow.ps1 seed
start http://localhost:3000
```

Compose start-up order is enforced with health checks: `postgres`, `redis`, `kafka` → `*-api` (runs migrations) → `*-worker` → `gateway` → `web`.

## Mode B — hybrid (services on host ports)

```powershell
.\swiftflow.ps1 infra     # Postgres :5432, Redis :6379, Kafka :9092
.\swiftflow.ps1 dev       # opens 9 terminals
```

`dev` runs, each with `--reload` where it applies:

| Window | Command |
|--------|---------|
| order-api | `uvicorn app.main:app --port 8001` in `services/order` |
| order-worker | `python -m app.worker` (metrics :9101) |
| inventory-api | `uvicorn app.main:app --port 8002` |
| inventory-worker | `python -m app.worker` (metrics :9102) |
| notification-api | `uvicorn app.main:app --port 8003` |
| notification-worker | `python -m app.worker` (metrics :9103) |
| partner-sim | `uvicorn app.main:app --port 8004` |
| gateway | `uvicorn app.main:app --port 8080` |
| web | `npm run dev` in `web/` (:5173) |

Each service reads its settings from `services/<svc>/.env.local` (created by `dev` from `.env.example` if missing). The only differences from Docker are host names: `localhost:5432`, `localhost:6379`, `localhost:9092`, `http://localhost:8001`, and so on.

You can also run any single service alone, for example the inventory API:

```powershell
cd services\inventory
..\..\.venv\Scripts\uvicorn app.main:app --port 8002 --reload
start http://localhost:8002/docs
```

## URLs

| What | URL |
|------|-----|
| Console | http://localhost:3000 (Docker) · http://localhost:5173 (dev) |
| Gateway | http://localhost:8080 |
| Swagger UI per service | http://localhost:8001/docs · :8002/docs · :8003/docs · :8004/docs |
| System health | http://localhost:8080/v1/system/health |
| Prometheus · Grafana · Jaeger | http://localhost:9090 · http://localhost:3001 (admin/admin) · http://localhost:16686 |

## Demo credentials (local only)

| Client ID | Secret | Tenant | Role |
|-----------|--------|--------|------|
| `acme-admin` | `acme-admin-secret` | Acme Outfitters | admin |
| `acme-partner` | `acme-partner-secret` | Acme Outfitters | partner |
| `globex-admin` | `globex-admin-secret` | Globex Supply | admin |

Override them in `deploy/compose/.env`. They are created by `seed`, never by migrations, and the login screen lists them only when `ENVIRONMENT=local`.

## Configuration reference

All settings are environment variables (pydantic-settings), documented per service in [services.md](services.md). Shared ones:

| Variable | Default (host) | Meaning |
|----------|----------------|---------|
| `ENVIRONMENT` | `local` | `local`, `ci`, `production` |
| `LOG_LEVEL` / `LOG_FORMAT` | `INFO` / `console` | `json` in Docker |
| `DATABASE_URL` | `postgresql://<role>:<pw>@localhost:5432/swiftflow` | Each service uses its own role |
| `REDIS_URL` | `redis://127.0.0.1:6379/0` | |
| `KAFKA_BOOTSTRAP` | `localhost:9092` | `kafka:29092` inside Docker |
| `JWKS_URL` | `http://localhost:8001/.well-known/jwks.json` | Inventory and notification |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | unset (tracing off) | `http://jaeger:4318` with the observability profile |
| `RATE_LIMIT_RPS` / `RATE_LIMIT_BURST` | `200` / `1000` | Per tenant |

## Troubleshooting

| Symptom | Cause and fix |
|---------|---------------|
| `failed to connect to the docker API … dockerDesktopLinuxEngine` | Docker Desktop isn't running. Start `D:\tools\Docker\Docker Desktop.exe` and wait for `docker info` to succeed |
| Port already in use | `Get-NetTCPConnection -LocalPort 8001` to find the process; stop it or change the port in `.env` |
| Orders stay `PENDING` | A worker isn't running, or Kafka isn't reachable. Check the System map, then `.\swiftflow.ps1 logs inventory-worker` |
| Orders stay `RESERVED` | The payment provider is in outage or the circuit is open. Check Chaos Lab |
| Kafka listener errors on the host | Host clients must use `localhost:9092`; containers use `kafka:29092` |
| `unrecognized configuration parameter "app.tenant_id"` | Expected for a query that doesn't set a tenant: RLS is failing closed |
