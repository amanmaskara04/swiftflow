# ADR 0005 — Tenant isolation with row-level security

**Status:** accepted · 2026-11-14

## Decision
- Every tenant-scoped table has RLS `ENABLE` + `FORCE` with a policy on `current_setting('app.tenant_id')::uuid`.
- The tenant ID comes only from the verified JWT and is set per transaction with `set_config('app.tenant_id', $1, true)`.
- Cross-tenant workers (relay, webhook dispatcher, payment processor) use `system_tx()`, which sets `app.system = on` for that transaction only.
- An unset tenant raises an error rather than returning rows.
- Cross-tenant reads by ID return `404`, not `403`, so the existence of another tenant's data is not revealed.

## Consequences
A forgotten `WHERE tenant_id = …` in application code cannot leak data. `app.system` is an escape hatch for trusted code, used in three named places and covered by tests.
