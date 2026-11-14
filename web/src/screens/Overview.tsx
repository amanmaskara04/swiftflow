// Overview: the line board (every in-flight order at its station), the oversold
// odometer, a quiet KPI row, orders per minute, and the live event tape.
import { useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { useLive, useLiveEvents, type TapeItem } from "../api/live";
import { fetchOrderPage, qk, useHealth, useStats, useStock } from "../api/resources";
import type { OrderSummary } from "../api/types";
import { STATIONS } from "../components/SagaRail";
import { Empty, ErrorState, Head } from "../components/ui";
import { clock, count, money, seconds, shortId } from "../lib/format";

const STATION_EVENT = ["OrderPlaced", "StockReserved", "payment approved", "OrderConfirmed", "OrderShipped"];
const X = ["10%", "30%", "50%", "70%", "90%"];
const MAX_TOKENS = 6;

function LineBoard() {
  const stats = useStats();
  const inflight = useQuery({
    queryKey: qk.inflight,
    queryFn: ({ signal }) => fetchOrderPage({ status: ["PENDING", "RESERVED"], limit: 100 }, signal),
    refetchInterval: 15_000,
  });
  // Orders that were just paid sit at the Paid station for a moment before they count as confirmed.
  const [paid, setPaid] = useState<Array<{ id: string; until: number }>>([]);
  useLiveEvents((e) => {
    if (e.event_type === "OrderConfirmed") setPaid((cur) => [...cur.slice(-40), { id: e.aggregate_id, until: Date.now() + 1400 }]);
  });
  useEffect(() => {
    if (!paid.length) return;
    const t = setTimeout(() => setPaid((cur) => cur.filter((p) => p.until > Date.now())), 500);
    return () => clearTimeout(t);
  }, [paid]);

  const orders: OrderSummary[] = inflight.data?.items ?? [];
  const at: string[][] = [
    orders.filter((o) => o.status === "PENDING").map((o) => o.id),
    orders.filter((o) => o.status === "RESERVED").map((o) => o.id),
    paid.map((p) => p.id),
    [],
    [],
  ];
  const c = stats.data?.counts ?? {};
  const figures = [at[0]!.length, at[1]!.length, at[2]!.length, c.CONFIRMED ?? 0, c.SHIPPED ?? 0];

  return (
    <div className="line">
      <svg className="rail-svg" width="100%" height="24" aria-hidden="true">
        <line x1="10%" y1="12" x2="90%" y2="12" stroke="var(--ink)" strokeWidth="6" strokeLinecap="round" />
        {X.map((x, i) => (
          <circle key={x} cx={x} cy="12" r="9" fill={at[i]!.length ? "var(--signal)" : "var(--sheet)"} stroke="var(--ink)" strokeWidth="6" />
        ))}
      </svg>
      {/* The two compensation branches leave the line between stations and run down to their terminals. */}
      <svg aria-hidden="true" style={{ position: "absolute", left: 0, top: 12, bottom: 27, width: "100%", height: "calc(100% - 39px)", pointerEvents: "none" }}>
        <line x1="20%" y1="0" x2="20%" y2="100%" stroke="var(--stop)" strokeWidth="6" strokeLinecap="round" />
        <line x1="60%" y1="0" x2="60%" y2="100%" stroke="var(--stop)" strokeWidth="6" strokeLinecap="round" />
      </svg>
      <div className="cols" style={{ marginTop: 12 }}>
        {STATIONS.map((name, i) => (
          <div className="stn" key={name}>
            <div className="cap">{name}</div>
            <div className="fig">{count(figures[i])}</div>
            <div className="evt">{STATION_EVENT[i]}</div>
            <div className="tokens">
              {at[i]!.slice(0, MAX_TOKENS).map((id) => (
                <Link key={id} to={`/orders/${id}`} className="tok new" style={{ textDecoration: "none" }}>
                  {shortId(id, 4)}
                </Link>
              ))}
              {at[i]!.length > MAX_TOKENS && <span className="tok more">+{at[i]!.length - MAX_TOKENS}</span>}
            </div>
          </div>
        ))}
      </div>
      <div className="terms">
        <div className="term" style={{ left: "calc(20% - 9px)", top: 18 }}>
          <span style={{ width: 18, height: 18, background: "var(--stop)", borderRadius: 3, flex: "none" }} />
          <div>
            <div className="cap">Rejected<span className="wide-only"> · no stock</span></div>
            <b>{count(c.REJECTED ?? 0)}</b>
          </div>
        </div>
        <div className="term" style={{ left: "calc(60% - 9px)", top: 18 }}>
          <span style={{ width: 18, height: 18, boxShadow: "inset 0 0 0 4px var(--stop)", background: "var(--sheet)", borderRadius: 3, flex: "none" }} />
          <div>
            <div className="cap">Cancelled<span className="wide-only"> · stock released</span></div>
            <b>{count(c.CANCELLED ?? 0)}</b>
          </div>
        </div>
      </div>
    </div>
  );
}

function Odometer() {
  const stock = useStock();
  const stats = useStats();
  const oversold = stock.data?.oversold_rows ?? 0;
  const total = Object.values(stats.data?.counts ?? {}).reduce((a, b) => a + (b ?? 0), 0);
  const digits = String(Math.min(9999, oversold)).padStart(4, "0").split("");
  return (
    <div className={`odo${oversold ? " bad" : ""}`} role="status" aria-label={`Oversold units: ${oversold}`}>
      <span className="cap">Oversold units</span>
      <div className="wheels" aria-hidden="true">
        {digits.map((d, i) => (
          <div className="wheel" key={i}>
            <div className="strip" style={{ transform: `translateY(-${Number(d) * 10}%)` }}>
              {"0123456789".split("").map((n) => (
                <span key={n}>{n}</span>
              ))}
            </div>
          </div>
        ))}
      </div>
      {oversold === 0 ? (
        <p>
          <span className="ok">Stock is true.</span> Reserved has never exceeded on hand across {count(total)} orders.
        </p>
      ) : (
        <p>
          <span className="bad-t">Stock is wrong.</span> {oversold} stock rows have more reserved than on hand.
        </p>
      )}
    </div>
  );
}

function Kpis() {
  const stats = useStats();
  const stock = useStock();
  const health = useHealth();
  const lastHour = (stats.data?.per_minute ?? []).reduce((a, m) => a + m.total, 0);
  const circuit = health.data?.nodes.find((n) => n.id === "order-worker")?.checks?.payment_circuit;
  const circuitText = circuit === "open" ? "Open" : circuit === "half_open" ? "Probing" : circuit === "closed" ? "Closed" : "—";
  const t = stock.data?.totals;
  return (
    <div className="grid kpis" style={{ marginBottom: 16 }}>
      <div className="card kpi">
        <span className="cap">Placed to settled, p95</span>
        <div className="fig">
          {seconds(stats.data?.saga_p95_ms)} <small>s</small>
        </div>
        <small>p50 {seconds(stats.data?.saga_p50_ms)} s · last 60 min</small>
      </div>
      <div className="card kpi">
        <span className="cap">Orders, last 60 min</span>
        <div className="fig">{count(lastHour)}</div>
        <small>{(lastHour / 60).toFixed(1)} per minute</small>
      </div>
      <div className="card kpi">
        <span className="cap">Units reserved</span>
        <div className="fig">{count(t?.reserved)}</div>
        <small>of {count(t?.on_hand)} on hand</small>
      </div>
      <div className="card kpi">
        <span className="cap">Payment circuit</span>
        <div className="fig" style={{ color: circuit === "open" ? "var(--stop)" : circuit === "closed" ? "var(--go)" : undefined }}>
          {circuitText}
        </div>
        <small>{circuit === "open" ? "orders wait in Reserved" : circuit === "half_open" ? "testing the provider" : "provider reachable"}</small>
      </div>
    </div>
  );
}

function PerMinute() {
  const stats = useStats();
  const [asTable, setAsTable] = useState(false);
  const data = useMemo(() => {
    const by = new Map((stats.data?.per_minute ?? []).map((m) => [new Date(m.minute).getTime(), m]));
    const now = Math.floor(Date.now() / 60000) * 60000;
    return Array.from({ length: 60 }, (_, i) => {
      const ts = now - (59 - i) * 60000;
      const m = by.get(ts)?.counts ?? {};
      return {
        t: clock(new Date(ts).toISOString(), false).slice(0, 5),
        confirmed: (m.CONFIRMED ?? 0) + (m.SHIPPED ?? 0),
        stopped: (m.REJECTED ?? 0) + (m.CANCELLED ?? 0),
        inflight: (m.PENDING ?? 0) + (m.RESERVED ?? 0),
      };
    });
  }, [stats.data]);
  const any = data.some((d) => d.confirmed + d.stopped + d.inflight > 0);
  return (
    <div className="card">
      <header>
        <span className="cap">Orders per minute</span>
        <button className="linkbtn aside" style={{ fontSize: ".75rem" }} onClick={() => setAsTable((v) => !v)}>
          {asTable ? "View as chart" : "View as table"}
        </button>
      </header>
      {stats.isError ? (
        <ErrorState error={stats.error} retry={() => void stats.refetch()} />
      ) : !any ? (
        <Empty title="No orders in the last hour">Place one from Orders, or run the oversell test in Chaos lab.</Empty>
      ) : asTable ? (
        <div style={{ maxHeight: 240, overflow: "auto" }}>
          <table>
            <thead>
              <tr>
                <th>Minute</th>
                <th className="r">Confirmed</th>
                <th className="r">Rejected or cancelled</th>
                <th className="r">In flight</th>
              </tr>
            </thead>
            <tbody>
              {data.filter((d) => d.confirmed + d.stopped + d.inflight > 0).map((d) => (
                <tr key={d.t}>
                  <td className="mono">{d.t}</td>
                  <td className="r mono">{d.confirmed}</td>
                  <td className="r mono">{d.stopped}</td>
                  <td className="r mono">{d.inflight}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <>
          <div className="chartwrap">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={data} barCategoryGap={2} margin={{ top: 4, right: 0, bottom: 0, left: -18 }}>
                <CartesianGrid vertical={false} stroke="var(--rule)" />
                <XAxis dataKey="t" tick={{ fontSize: 10, fill: "var(--ink-2)", fontFamily: "var(--mono)" }} tickLine={false} axisLine={{ stroke: "var(--rule)" }} interval={9} />
                <YAxis allowDecimals={false} tick={{ fontSize: 10, fill: "var(--ink-2)", fontFamily: "var(--mono)" }} tickLine={false} axisLine={false} />
                <Tooltip
                  cursor={{ fill: "var(--sunk)" }}
                  contentStyle={{ background: "var(--sheet)", border: "1px solid var(--rule)", borderRadius: 6, fontSize: 12 }}
                  labelStyle={{ fontFamily: "var(--mono)" }}
                />
                <Bar dataKey="confirmed" name="Confirmed" stackId="a" fill="var(--go)" isAnimationActive={false} />
                <Bar dataKey="stopped" name="Rejected or cancelled" stackId="a" fill="var(--stop)" isAnimationActive={false} />
                <Bar dataKey="inflight" name="In flight" stackId="a" fill="var(--signal)" isAnimationActive={false} />
              </BarChart>
            </ResponsiveContainer>
          </div>
          <div className="legend">
            <span>
              <i style={{ background: "var(--go)" }} /> Confirmed
            </span>
            <span>
              <i style={{ background: "var(--stop)" }} /> Rejected or cancelled
            </span>
            <span>
              <i style={{ background: "var(--signal)" }} /> In flight
            </span>
          </div>
        </>
      )}
    </div>
  );
}

const DOT: Record<string, string> = {
  OrderPlaced: "var(--signal)",
  StockReserved: "var(--route)",
  OrderConfirmed: "var(--go)",
  OrderShipped: "var(--ink)",
  StockCommitted: "var(--ink)",
  StockReservationFailed: "var(--stop)",
  OrderCancelled: "var(--stop)",
  StockReleased: "var(--stop)",
};

function tapeDetail(t: TapeItem): string {
  const p = t.env.payload as Record<string, unknown>;
  if (t.env.event_type === "StockLevelChanged") return `${String(p.sku)} · ${String(p.warehouse_code)} · ${String(p.available)} available`;
  const bits = [`#${shortId(t.env.aggregate_id)}`];
  if (typeof p.total_cents === "number") bits.push(money(p.total_cents));
  if (typeof p.reason === "string") bits.push(p.reason.replace(/_/g, " "));
  return bits.join(" · ");
}

function EventTape() {
  const { tape, status } = useLive();
  const now = Date.now();
  return (
    <div className="card">
      <header>
        <span className="cap">Event tape</span>
        <span className="aside mono">{status === "live" ? "live" : status}</span>
      </header>
      {tape.length === 0 ? (
        <Empty title="No events yet">Events appear here the moment a service publishes them.</Empty>
      ) : (
        <div className="tape">
          <ul>
            {tape.slice(0, 60).map((t) => (
              <li key={t.env.event_id} className={t.live && now - t.seenAt < 2000 ? "new" : ""}>
                <time className="mono">{clock(t.env.occurred_at)}</time>
                <div>
                  <b>
                    <i style={{ background: DOT[t.env.event_type] ?? "var(--ink-2)" }} />
                    {t.env.event_type}
                  </b>
                  <small className="mono">{tapeDetail(t)}</small>
                </div>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

export function Overview() {
  return (
    <>
      <Head title="Overview" sub="Every order, at the station it has reached, right now." />
      <div className="card" style={{ marginBottom: 16 }}>
        <header>
          <span className="cap">The line</span>
          <span className="aside mono">orders.events + inventory.events</span>
        </header>
        <div className="board">
          <LineBoard />
          <Odometer />
        </div>
      </div>
      <Kpis />
      <div className="grid two">
        <PerMinute />
        <EventTape />
      </div>
    </>
  );
}
