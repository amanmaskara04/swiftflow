# Environment audit

Checked on 2026-11-14 on the development machine (Windows 11, 16 logical CPUs, 15.7 GB RAM).

**Policy:** all software, SDKs, caches and project files live on **D:**. Nothing was installed to C: for this project.

## Already present (reused)

| Tool | Version | Location | Notes |
|------|---------|----------|-------|
| Docker Desktop | 29.8.1, Compose v5.5.1 | `D:\tools\Docker` | VM memory 8 GB, 16 CPUs. Started for this audit |
| Python | 3.12.10 | `D:\tools\Python312` | Used as the base for the project venv |
| Python | 3.15.2 | `C:\Python315` (pre-existing system default) | **Not used**: too new for reliable wheels (asyncpg, pydantic-core). The project pins 3.12 |
| Node.js / npm | 24.22.1 / 11.19.1 | `C:\Program Files\nodejs` (pre-existing) | npm prefix `D:\npm-global`, cache `D:\cache\npm` |
| pip cache | — | `D:\cache\pip` | Set via `pip config` and `PIP_CACHE_DIR` |
| Git | — | `C:\Program Files\Git` (pre-existing) | |
| kubectl | bundled with Docker | `D:\tools\Docker\resources\bin` | For the optional Kubernetes phase |
| WSL 2 | Ubuntu + docker-desktop distros | — | Not required; everything runs from PowerShell |

## Installed for this project (all on D:)

| What | Location |
|------|----------|
| Python virtual environment (3.12.10) | `D:\Portfolio\Swiftflow\.venv` |
| Backend libraries (below) | inside the venv; exact versions frozen in `requirements-dev.lock.txt` |
| Frontend libraries (below) | `D:\Portfolio\Swiftflow\web\node_modules` |
| Docker images | Docker Desktop's disk on D: |

### Backend libraries (venv)

| Package | Version | Purpose |
|---------|---------|---------|
| fastapi / starlette | 0.162.7 / 1.10.0 | REST APIs |
| uvicorn[standard] | 0.54.0 | ASGI server (httptools, watchfiles, websockets) |
| pydantic / pydantic-settings | 2.14.3 / 2.16.3 | Models, settings |
| asyncpg | 0.32.0 | Postgres driver |
| aiokafka | 0.15.0 | Kafka producer and consumer |
| redis | 8.1.0 | Redis client (asyncio) |
| httpx | 0.28.1 | Gateway proxy, payment and webhook calls |
| tenacity | 9.2.1 | Retries with backoff |
| PyJWT[crypto] / cryptography | 2.15.1 / 50.0.2 | Ed25519 JWTs (verified working) |
| argon2-cffi | 25.1.0 | Client secret hashing |
| structlog | 26.1.0 | JSON logs |
| prometheus-client | 0.26.0 | Metrics |
| jsonschema | 4.26.0 | Event contracts |
| opentelemetry-sdk + OTLP HTTP exporter | 1.45.2 | Tracing |
| opentelemetry-instrumentation-fastapi / httpx / asyncpg | 0.66b2 | Auto-instrumentation |
| pytest / pytest-asyncio | 9.2.0 / 1.5.0 | Tests |
| ruff | 0.17.5 | Lint and format |

### Frontend libraries (`web/`)

| Package | Version | Purpose |
|---------|---------|---------|
| react / react-dom | 18.3.1 | UI |
| react-router-dom | 6.30.6 | Routing |
| @tanstack/react-query | 5.107.1 | Server state |
| recharts | 3.10.1 | Charts |
| lucide-react | 1.73.0 | Icons |
| clsx | 2.1.1 | Class names |
| @fontsource-variable/archivo, @fontsource/instrument-sans, @fontsource/ibm-plex-mono | 5.3.0 | Bundled fonts |
| vite / @vitejs/plugin-react | 6.4.6 / 4.7.0 | Build and dev server (esbuild 0.25.12 verified) |
| typescript | 7.0.2 | Type checking |
| tailwindcss / postcss / autoprefixer | 3.4.19 / 8.5.33 / 10.6.2 | Styling |

### Docker images pulled

| Image | Size | Role |
|-------|------|------|
| `postgres:17-alpine` | 424 MB | Database |
| `redis:7-alpine` | 59 MB | Cache, idempotency, rate limits, pub/sub |
| `apache/kafka:4.1.0` | 678 MB | Event bus (KRaft) |
| `python:3.12-slim` | 180 MB | Base image for every Python service |
| `node:22-alpine` | 239 MB | Builds the console |
| `nginx:1.27-alpine` | 75 MB | Serves the console |

## Not installed (not needed for the core build)

| Tool | Needed for | Plan |
|------|-----------|------|
| uv | Faster installs (the guide's choice) | pip + venv used instead; see [ADR 0006](adr/0006-local-toolchain.md) |
| make | Makefile targets | `swiftflow.ps1` replaces it on Windows; the Makefile is for CI/WSL |
| k3d, kind, helm, terraform | Kubernetes phase (optional) | Install to `D:\Apps\<name>` when that phase starts |
| cloudflared | Public demo URL | Install to `D:\Apps\cloudflared` when needed |
| Prometheus, Grafana, Jaeger images | `observability` profile | Pulled on first `up -Observability` |

## Re-creating the environment

```powershell
D:\tools\Python312\python.exe -m venv D:\Portfolio\Swiftflow\.venv
D:\Portfolio\Swiftflow\.venv\Scripts\pip install -r D:\Portfolio\Swiftflow\requirements-dev.lock.txt
cd D:\Portfolio\Swiftflow\web; npm ci
```
