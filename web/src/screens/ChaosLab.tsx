// Chaos lab: inject payment faults, and run the oversell test from the browser.
import { useQueryClient } from "@tanstack/react-query";
import { Loader2, Play, Square } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import { useLive } from "../api/live";
import { fetchStock, qk, useSimConfig, useStock } from "../api/resources";
import type { EventEnvelope, Order, SimConfig, SimPayments } from "../api/types";
import { Head, ProblemAlert, useToast } from "../components/ui";
import { count, seconds } from "../lib/format";
import { newKey } from "../lib/idempotency";
import { percentile, runPool, sleep } from "../lib/pool";

function Faults() {
  const cfg = useSimConfig();
  const qc = useQueryClient();
  const toast = useToast();
  const [draft, setDraft] = useState<SimConfig | null>(null);
  useEffect(() => {
    if (cfg.data && !draft) setDraft(cfg.data);
  }, [cfg.data, draft]);
  const c = draft ?? { failure_rate: 0, latency_ms: 0, outage: false };

  async function apply(patch: Partial<SimConfig>) {
    const next = { ...c, ...patch };
    setDraft(next);
    try {
      const r = await api.put<SimConfig>("/v1/sim/config", patch);
      qc.setQueryData(qk.simConfig, r.data);
    } catch (e) {
      toast.fail(e);
      setDraft(cfg.data ?? null);
    }
  }

  return (
    <div className="card">
      <div className="hazard" />
      <header>
        <span className="cap">Payment provider faults</span>
      </header>
      <div className="ctrl">
        <label className="row">
          <span>Decline rate</span>
          <input type="range" min={0} max={100} step={5} value={Math.round(c.failure_rate * 100)} onChange={(e) => setDraft({ ...c, failure_rate: Number(e.target.value) / 100 })} onPointerUp={(e) => void apply({ failure_rate: Number(e.currentTarget.value) / 100 })} onKeyUp={(e) => void apply({ failure_rate: Number(e.currentTarget.value) / 100 })} />
          <span className="mono">{Math.round(c.failure_rate * 100)} %</span>
        </label>
        <label className="row">
          <span>Added latency</span>
          <input type="range" min={0} max={5000} step={50} value={c.latency_ms} onChange={(e) => setDraft({ ...c, latency_ms: Number(e.target.value) })} onPointerUp={(e) => void apply({ latency_ms: Number(e.currentTarget.value) })} onKeyUp={(e) => void apply({ latency_ms: Number(e.currentTarget.value) })} />
          <span className="mono">{c.latency_ms} ms</span>
        </label>
        <div className="row">
          <span>Provider outage</span>
          <span>
            <button className="switch" role="switch" aria-checked={c.outage} onClick={() => void apply({ outage: !c.outage })}>
              <i />
              {c.outage ? "On" : "Off"}
            </button>
          </span>
          <span />
        </div>
        <ProblemAlert error={cfg.error} />
      </div>
      <p className="note">
        Faults you inject here are real and affect every tenant in this environment. During an outage the circuit breaker opens and orders wait in Reserved until the provider returns.
      </p>
    </div>
  );
}

interface Run {
  sku: string;
  units: number;
  orders: number;
  phase: "preparing" | "placing" | "settling" | "done" | "stopped";
  placed: number;
  failedToPlace: number;
  confirmed: number;
  rejected: number;
  cancelled: number;
  placedMs: number | null;
  settledMs: number | null;
  p95: number | null;
  after: { on_hand: number; reserved: number } | null;
  reservedBefore: number;
  doubleCharges: number | null;
  error: unknown;
}

function verdict(r: Run): "pass" | "fail" | "run" {
  if (r.phase !== "done") return r.phase === "stopped" ? "fail" : "run";
  const expected = Math.min(r.units, r.orders);
  const reservedDelta = r.after ? r.after.reserved - r.reservedBefore : -1;
  const oversold = r.after ? Math.max(0, r.after.reserved - r.after.on_hand) : 0;
  return r.confirmed === expected && reservedDelta === expected && oversold === 0 && r.doubleCharges === 0 ? "pass" : "fail";
}

function OversellTest() {
  const stock = useStock();
  const live = useLive();
  const qc = useQueryClient();
  const [sku, setSku] = useState("LOAD-TEST-1");
  const [units, setUnits] = useState(100);
  const [orders, setOrders] = useState(500);
  const [run, setRun] = useState<Run | null>(null);
  const abort = useRef<AbortController | null>(null);
  const rows = stock.data?.rows ?? [];
  const busy = run !== null && ["preparing", "placing", "settling"].includes(run.phase);

  async function start() {
    const ctrl = new AbortController();
    abort.current = ctrl;
    const state: Run = { sku, units, orders, phase: "preparing", placed: 0, failedToPlace: 0, confirmed: 0, rejected: 0, cancelled: 0, placedMs: null, settledMs: null, p95: null, after: null, reservedBefore: 0, doubleCharges: null, error: null };
    const push = () => setRun({ ...state });
    push();
    const mine = new Map<string, "open" | "CONFIRMED" | "REJECTED" | "CANCELLED">();
    const early = new Map<string, "CONFIRMED" | "REJECTED" | "CANCELLED">(); // events that beat the 202 response
    const settle = (id: string, s: "CONFIRMED" | "REJECTED" | "CANCELLED") => {
      if (!mine.has(id)) {
        early.set(id, s);
        return;
      }
      if (mine.get(id) !== "open") return;
      mine.set(id, s);
      if (s === "CONFIRMED") state.confirmed++;
      else if (s === "REJECTED") state.rejected++;
      else state.cancelled++;
    };
    const off = live.subscribe((e: EventEnvelope) => {
      if (e.event_type === "OrderConfirmed") settle(e.aggregate_id, "CONFIRMED");
      else if (e.event_type === "StockReservationFailed") settle(e.aggregate_id, "REJECTED");
      else if (e.event_type === "OrderCancelled") settle(e.aggregate_id, "CANCELLED");
    });
    const ticker = setInterval(push, 200);
    try {
      // 1. Clear faults and make exactly `units` available in one warehouse.
      await api.put("/v1/sim/config", { failure_rate: 0, outage: false, latency_ms: 20 });
      const grid = await fetchStock(ctrl.signal);
      const row = grid.rows.find((r) => r.sku === sku);
      const cell = row?.cells[0];
      if (!row || !cell) throw new Error(`${sku} has no stock row yet. Set a count for it on the Stock screen first.`);
      const elsewhere = row.total_available - cell.available;
      if (elsewhere > units) throw new Error(`${sku} already has ${elsewhere} units available in other warehouses; pick a lower-stock SKU or more units.`);
      state.reservedBefore = cell.reserved;
      await api.post("/v1/inventory/adjustments", { sku, warehouse_code: cell.warehouse_code, mode: "set", quantity: cell.reserved + units - elsewhere, reason: "oversell test" });

      // 2. Fire every order, each with its own idempotency key.
      state.phase = "placing";
      const t0 = performance.now();
      const latencies: number[] = [];
      await runPool(orders, 64, async () => {
          const s = performance.now();
          try {
            const r = await api.post<Order>("/v1/orders", { customer_ref: "oversell-test", items: [{ sku, qty: 1 }] }, { idempotencyKey: newKey("oversell"), signal: ctrl.signal, retry429: 5 });
            latencies.push(performance.now() - s);
            mine.set(r.data.id, "open");
            state.placed++;
            const e = early.get(r.data.id);
            if (e) settle(r.data.id, e);
          } catch {
            state.failedToPlace++;
          }
        }, ctrl.signal);
      state.placedMs = performance.now() - t0;
      state.p95 = percentile(latencies, 95);

      // 3. Wait for every order to settle: the live stream first, polling as the fallback.
      state.phase = "settling";
      const deadline = performance.now() + 120_000;
      let quiet = 0;
      while (!ctrl.signal.aborted && performance.now() < deadline) {
        const open = [...mine.entries()].filter(([, s]) => s === "open").map(([id]) => id);
        if (open.length === 0) break;
        await sleep(500, ctrl.signal);
        if (++quiet >= 3) {
          await runPool(Math.min(open.length, 24), 6, async (i) => {
              try {
                const o = await api.get<Order>(`/v1/orders/${open[i]}`, undefined, ctrl.signal);
                if (o.status === "CONFIRMED" || o.status === "SHIPPED") settle(o.id, "CONFIRMED");
                else if (o.status === "REJECTED" || o.status === "CANCELLED") settle(o.id, o.status);
              } catch {
                /* keep waiting */
              }
            }, ctrl.signal);
        }
      }
      state.settledMs = performance.now() - t0;
      if (ctrl.signal.aborted) {
        state.phase = "stopped";
        return;
      }

      // 4. Read the truth back from inventory and the payment provider.
      await sleep(600);
      const after = (await fetchStock()).rows.find((r) => r.sku === sku)?.cells.find((c) => c.warehouse_code === cell.warehouse_code);
      state.after = after ? { on_hand: after.on_hand, reserved: after.reserved } : null;
      state.doubleCharges = (await api.get<SimPayments>("/v1/sim/payments")).double_charges;
      state.phase = "done";
    } catch (e) {
      state.error = e;
      state.phase = "stopped";
    } finally {
      clearInterval(ticker);
      off();
      push();
      void qc.invalidateQueries({ queryKey: qk.stock });
      void qc.invalidateQueries({ queryKey: qk.ordersAll });
    }
  }

  const r = run;
  const inFlight = r ? r.placed - r.confirmed - r.rejected - r.cancelled : 0;
  const v = r ? verdict(r) : "run";
  const oversold = r?.after ? Math.max(0, r.after.reserved - r.after.on_hand) + Math.max(0, r.confirmed - Math.min(r.units, r.orders)) : r ? Math.max(0, r.confirmed - r.units) : 0;
  const done = r ? r.confirmed + r.rejected + r.cancelled : 0;

  return (
    <>
      <div className="card">
        <header>
          <span className="cap">Oversell test</span>
          <span className="aside mono">{sku}</span>
        </header>
        <div className="ctrl">
          <label className="row">
            <span>Product</span>
            <select className="select" value={sku} disabled={busy} onChange={(e) => setSku(e.target.value)}>
              {!rows.some((x) => x.sku === sku) && <option value={sku}>{sku}</option>}
              {rows.map((x) => (
                <option key={x.sku} value={x.sku}>
                  {x.sku} — {x.name}
                </option>
              ))}
            </select>
            <span />
          </label>
          <label className="row">
            <span>Units in stock</span>
            <input type="range" min={10} max={500} step={10} value={units} disabled={busy} onChange={(e) => setUnits(Number(e.target.value))} />
            <span className="mono">{units}</span>
          </label>
          <label className="row">
            <span>Orders fired at once</span>
            <input type="range" min={50} max={1000} step={50} value={orders} disabled={busy} onChange={(e) => setOrders(Number(e.target.value))} />
            <span className="mono">{orders}</span>
          </label>
          <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
            {busy ? (
              <button className="btn danger" onClick={() => abort.current?.abort()}>
                <Square /> Stop test
              </button>
            ) : (
              <button className="btn primary" onClick={() => void start()}>
                <Play /> Run oversell test
              </button>
            )}
            <span className="muted" style={{ fontSize: ".8125rem" }}>
              Sets {units} units available, then places {orders} one-unit orders at once. Exactly {Math.min(units, orders)} must be confirmed.
            </span>
          </div>
        </div>
      </div>

      {r && (
        <div className="card" style={{ marginTop: 16 }}>
          <header>
            <span className="cap">{busy ? "Running" : "Last run"}</span>
            <span className={`stamp${v === "fail" ? " fail" : v === "run" ? " run" : ""}`}>{v === "pass" ? "PASSED" : v === "fail" ? (r.phase === "stopped" ? "STOPPED" : "FAILED") : r.phase.toUpperCase()}</span>
            {busy && <Loader2 className="spin" size={16} />}
            <span className="aside mono">
              {r.sku} · {r.units} units · {r.orders} orders
            </span>
          </header>
          <div className="grid result">
            <div>
              <span className="cap">Confirmed</span>
              <div className="fig" style={{ color: "var(--go)" }}>{count(r.confirmed)}</div>
            </div>
            <div>
              <span className="cap">Rejected</span>
              <div className="fig">{count(r.rejected + r.cancelled)}</div>
            </div>
            <div>
              <span className="cap">In flight</span>
              <div className="fig">{count(Math.max(0, inFlight))}</div>
            </div>
            <div>
              <span className="cap">Oversold units</span>
              <div className="fig" style={{ color: oversold ? "var(--stop)" : "var(--go)" }}>{count(oversold)}</div>
            </div>
          </div>
          <div className="progress" role="progressbar" aria-valuemin={0} aria-valuemax={r.orders} aria-valuenow={done} aria-label="Orders settled">
            <i style={{ width: `${(r.confirmed / r.orders) * 100}%`, background: "var(--go)" }} />
            <i style={{ width: `${((r.rejected + r.cancelled) / r.orders) * 100}%`, background: "var(--ink)" }} />
            <i style={{ width: `${(Math.max(0, inFlight) / r.orders) * 100}%`, background: "var(--signal)" }} />
          </div>
          {r.error != null && (
            <div style={{ padding: "0 18px 16px" }}>
              <ProblemAlert error={r.error} />
            </div>
          )}
          <p className="note mono">
            {r.after ? `stock after: on hand ${r.after.on_hand} · reserved ${r.after.reserved} (${r.after.reserved - r.reservedBefore} by this run) · available ${r.after.on_hand - r.after.reserved}` : "stock after: waiting for orders to settle"}
            {r.doubleCharges !== null ? ` · double charges ${r.doubleCharges}` : ""}
            <br />
            {r.placedMs !== null ? `placed ${r.placed} in ${seconds(r.placedMs)} s` : `placing ${r.placed} of ${r.orders}`}
            {r.failedToPlace ? ` (${r.failedToPlace} not accepted)` : ""}
            {r.settledMs !== null ? ` · all settled in ${seconds(r.settledMs)} s` : ""}
            {r.p95 !== null ? ` · p95 accept latency ${Math.round(r.p95)} ms` : ""}
          </p>
        </div>
      )}
    </>
  );
}

export function ChaosLab() {
  return (
    <>
      <Head title="Chaos lab" sub="Break the payment provider on purpose, then watch the system recover." />
      <div className="grid two" style={{ alignItems: "start" }}>
        <div>
          <OversellTest />
        </div>
        <Faults />
      </div>
    </>
  );
}
