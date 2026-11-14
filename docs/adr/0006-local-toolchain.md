# ADR 0006 — Local toolchain and gateway

**Status:** accepted · 2026-11-14

## Decisions
- **pip + venv on Python 3.12** instead of uv: uv isn't installed, pip is enough for six small services, and the exact set is frozen in `requirements-dev.lock.txt`. Python 3.15 on this machine is too new for reliable wheels.
- **`swiftflow.ps1`** is the task runner on Windows; a `Makefile` with the same targets serves CI and WSL.
- **A small FastAPI gateway** instead of Traefik for local and Compose runs. It adds request IDs, trace propagation, problem-JSON errors and the aggregated `/v1/system/health` that drives the console's system map. In Kubernetes, an Ingress (Traefik on k3s) can sit in front of it.
- **Docker Compose first.** Kubernetes (k3d/kind, Kustomize) is an optional later phase, with tools installed under `D:\Apps`.
- **Everything on D:**, per the machine's install policy.
