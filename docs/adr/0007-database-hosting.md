# ADR 0007 — Database for deployment: PostgreSQL, optionally hosted on Supabase

**Status:** accepted · 2026-11-14

## Context
The site will be deployed publicly. Options considered: self-hosted PostgreSQL, Supabase, MongoDB.

## Decision
Stay on **PostgreSQL**. For deployment, either run it next to the services or use a **managed Postgres host such as Supabase**. Supabase is Postgres underneath, so no code changes are needed.

## Why not MongoDB
The design depends on Postgres features that MongoDB does not provide in the same form:
- A conditional `UPDATE` under a row lock, plus `CHECK (reserved <= on_hand)`. This is the oversell guarantee.
- Multi-row, multi-table ACID transactions that commit the business row and the outbox row together.
- Row-level security with `FORCE`. This is the tenant-isolation guarantee.
- Per-service schemas and roles, `FOR UPDATE SKIP LOCKED`, `ON CONFLICT DO NOTHING`.

Switching would mean redesigning the core claims, not swapping a driver.

## Notes for Supabase
- Use only the Postgres database. Supabase Auth, its auto-generated REST API and its RLS helpers (`auth.uid()`) are not used; SwiftFlow keeps its own roles, JWTs and policies.
- Create the three service roles and schemas by running `init.sql` in the SQL editor.
- Prefer the direct or session-mode connection string. With a transaction-mode pooler, disable asyncpg's statement cache (`statement_cache_size=0`).
- Check the current free-plan limits (database size, connections, pausing of inactive projects) before relying on it for a demo.

## Consequences
Kafka and Redis still need a home in deployment; the database choice doesn't remove that. Deployment targets are chosen in the platform phase.
