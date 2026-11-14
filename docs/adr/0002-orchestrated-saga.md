# ADR 0002 — Orchestrated saga in the Order service

**Status:** accepted · 2026-11-14

## Context
Placing an order spans inventory and payment. A distributed transaction (2PC) across Postgres schemas, Kafka and an external provider is neither available nor desirable.

## Decision
Order orchestrates: it reacts to inventory results, calls payment, and issues compensations (`OrderCancelled` → stock released). Payment runs in a lease-based processor loop, not inside the Kafka handler.

## Alternatives
- **Choreography** (each service reacts to the others): fewer central dependencies, but no single place answers "where is order X stuck?".
- **Payment inside the consumer:** simpler, but a slow provider would hold a row lock and stall a partition.

## Consequences
Order knows every saga step. Every transition is recorded in `order_history`, which feeds the console's saga rail.
