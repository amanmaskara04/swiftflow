# ADR 0001 — Event-driven microservices with a schema per service

**Status:** accepted · 2026-11-14

## Context
SwiftFlow must show independent deployability, failure isolation and recovery. A single process would be simpler, but it could not show crash-mid-saga recovery or independent scaling.

## Decision
- Three domain services (order, inventory, notification) plus a gateway, a partner simulator and a web console, each with its own port, image and health checks.
- Domain services communicate **only** through Kafka. No service-to-service HTTP.
- One Postgres instance, one schema and one login role per service. No cross-schema reads or foreign keys.
- Each domain service runs as two processes from one image: `api` and `worker`.

## Consequences
- Eventual consistency: an order is `PENDING` for a few hundred milliseconds. The API returns `202` and the console shows progress live.
- More moving parts (Kafka, relays, consumers) to run and observe; mitigated by the system map, metrics and traces.
- A schema can move to its own database later with only a new `DATABASE_URL`.
