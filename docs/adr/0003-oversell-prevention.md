# ADR 0003 — Oversell prevention with a conditional update

**Status:** accepted · 2026-11-14

## Context
The original guide's reservation query includes `AND version = :expected_version` with up to 3 retries. Under heavy contention on one SKU (500 orders for one row), most transactions read a version that is stale by the time they write. They would exhaust their 3 retries and be **rejected while stock is still available**. Nothing would be oversold, but there would be false rejections, and the "exactly 100 of 500" proof would fail.

## Decision
- The hot reservation path uses only `WHERE id = $id AND on_hand - reserved >= $qty`. Under READ COMMITTED, a waiting transaction re-evaluates the predicate against the newly committed row, so the check is atomic without a version comparison.
- `version` is still incremented on every change and is used for **admin adjustments** via `If-Match`, so a person editing a count cannot silently overwrite a concurrent change.
- `CHECK (reserved <= on_hand)` remains the backstop.
- Rows of a multi-item order are locked in `(product_id, warehouse_id)` order to avoid deadlocks.
- A 0-row result means "not enough here": try the next warehouse, and count it in `swiftflow_reservation_conflicts_total`.

## Consequences
Exactly N confirmations for N units under any concurrency, with no retry storms. Throughput on one hot SKU is bounded by row-lock hand-off, which is acceptable and measurable.
