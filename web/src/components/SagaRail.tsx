// The saga rail: an order's lifecycle as a transit line. Stations are
// Placed → Reserved → Paid → Confirmed → Shipped; a compensation leaves the line
// as a red branch to its terminal (Rejected, or Released → Cancelled).
import type { HistoryEntry, OrderStatus } from "../api/types";
import { clock } from "../lib/format";

export const STATIONS = ["Placed", "Reserved", "Paid", "Confirmed", "Shipped"] as const;

export interface RailState {
  /** Last station reached on the main line (0-4). */
  reached: number;
  /** Station the red branch leaves from, or null when the order is on the main line. */
  failAt: number | null;
  terminal: "REJECTED" | "CANCELLED" | null;
  done: boolean;
}

interface OrderLike {
  status: OrderStatus;
  total_cents?: number | null;
  failure_reason?: string | null;
  payment_status?: string | null;
  history?: HistoryEntry[];
}

export function railState(o: OrderLike): RailState {
  const seen = new Set((o.history ?? []).map((h) => h.to_status));
  switch (o.status) {
    case "PENDING":
      return { reached: 0, failAt: null, terminal: null, done: false };
    case "RESERVED":
      return { reached: 1, failAt: null, terminal: null, done: false };
    case "CONFIRMED":
      return { reached: 3, failAt: null, terminal: null, done: false };
    case "SHIPPED":
      return { reached: 4, failAt: null, terminal: null, done: true };
    case "REJECTED":
      return { reached: 0, failAt: 0, terminal: "REJECTED", done: true };
    case "CANCELLED": {
      const wasConfirmed = seen.has("CONFIRMED") || o.payment_status === "REFUNDED" || o.payment_status === "REFUND_PENDING";
      const wasReserved = seen.has("RESERVED") || o.total_cents != null;
      const at = wasConfirmed ? 3 : wasReserved ? 1 : 0;
      return { reached: at, failAt: at, terminal: "CANCELLED", done: true };
    }
  }
}

function stationTimes(history: HistoryEntry[] | undefined): Array<string | null> {
  const t: Array<string | null> = [null, null, null, null, null];
  for (const h of history ?? []) {
    if (h.to_status === "PENDING") t[0] = h.at;
    if (h.to_status === "RESERVED") t[1] = h.at;
    if (h.to_status === "CONFIRMED") t[2] = t[3] = h.at;
    if (h.to_status === "SHIPPED") t[4] = h.at;
  }
  return t;
}

const DISPLAY = "var(--display)";

export function SagaRail({ order, size = "compact" }: { order: OrderLike; size?: "compact" | "full" }) {
  const s = railState(order);
  const full = size === "full";
  const w = full ? 480 : 150;
  const pad = full ? 36 : 7;
  const step = (w - 2 * pad) / 4;
  const y = full ? 14 : 9;
  const r = full ? 7 : 4;
  const h = full ? (s.failAt !== null ? 150 : 58) : 18;
  const by = y + 78; // the branch runs below the station labels
  const sw = full ? 4 : 3;
  const x = (i: number) => pad + step * i;
  const times = full ? stationTimes(order.history) : [];
  const label = `${order.status.toLowerCase()}: reached ${STATIONS[s.reached]}${s.terminal ? `, then ${s.terminal.toLowerCase()}` : ""}`;
  const cancelEntry = (order.history ?? []).find((e) => e.to_status === "CANCELLED" || e.to_status === "REJECTED");
  const branchDir = s.failAt !== null && s.failAt >= 3 ? -1 : 1; // branch runs left when it leaves late stations

  return (
    <svg width={w} height={h} viewBox={`0 0 ${w} ${h}`} role="img" aria-label={label} style={{ maxWidth: "100%" }}>
      <line x1={x(0)} y1={y} x2={x(4)} y2={y} stroke="var(--rule)" strokeWidth={sw} strokeLinecap="round" />
      <line x1={x(0)} y1={y} x2={x(s.reached)} y2={y} stroke="var(--ink)" strokeWidth={sw} strokeLinecap="round" />

      {s.failAt !== null && !full && (
        <>
          <line x1={x(s.failAt)} y1={y} x2={x(s.failAt) + step - 5} y2={y} stroke="var(--stop)" strokeWidth={sw} />
          <rect x={x(s.failAt) + step - 5} y={y - 4} width={8} height={8} rx={1} fill="var(--stop)" />
        </>
      )}

      {s.failAt !== null && full && (
        <g>
          <path
            d={`M${x(s.failAt)} ${y + (full ? 46 : 0)}V${by - 10}a10 10 0 0 ${branchDir === 1 ? 0 : 1} ${10 * branchDir} 10h${step * 1.5 * branchDir}`}
            fill="none"
            stroke="var(--stop)"
            strokeWidth={4}
            strokeLinecap="round"
          />
          {s.terminal === "CANCELLED" && s.failAt > 0 && (
            <>
              <circle cx={x(s.failAt) + step * 0.75 * branchDir} cy={by} r={r} fill="var(--stop)" />
              <text x={x(s.failAt) + step * 0.75 * branchDir} y={by + 24} textAnchor="middle" fontFamily={DISPLAY} fontWeight={700} fontSize={10} letterSpacing=".06em" fill="var(--stop)">
                RELEASED
              </text>
            </>
          )}
          <rect x={x(s.failAt) + (step * 1.5 + 4) * branchDir - 7} y={by - 7} width={14} height={14} rx={2} fill="var(--stop)" />
          <text x={x(s.failAt) + (step * 1.5 + 4) * branchDir} y={by + 24} textAnchor="middle" fontFamily={DISPLAY} fontWeight={700} fontSize={10} letterSpacing=".06em" fill="var(--stop)">
            {s.terminal}
          </text>
          <text x={x(s.failAt) + (step * 1.5 + 4) * branchDir} y={by + 38} textAnchor="middle" fontFamily="var(--mono)" fontSize={9.5} fill="var(--ink-2)">
            {cancelEntry ? clock(cancelEntry.at) : ""}
          </text>
          <text x={x(s.failAt) + (step * 1.5 + 4) * branchDir} y={by + 50} textAnchor="middle" fontFamily="var(--mono)" fontSize={9.5} fill="var(--ink-2)">
            {(order.failure_reason ?? "").replace(/_/g, " ").split(":")[0]}
            {order.payment_status === "REFUNDED" ? " · refunded" : ""}
          </text>
        </g>
      )}

      {STATIONS.map((name, i) => {
        const done = i <= s.reached;
        const here = i === s.reached && s.failAt === null && !s.done;
        return (
          <g key={name}>
            <circle
              cx={x(i)}
              cy={y}
              r={here ? r + 2 : r}
              fill={here ? "var(--signal)" : done ? "var(--ink)" : "var(--sheet)"}
              stroke={done ? "var(--ink)" : "var(--rule)"}
              strokeWidth={full ? 3 : 2}
            />
            {full && (
              <>
                <text x={x(i)} y={y + 26} textAnchor="middle" fontFamily={DISPLAY} fontWeight={700} fontSize={10} letterSpacing=".06em" fill={done ? "var(--ink)" : "var(--ink-2)"}>
                  {name.toUpperCase()}
                </text>
                {done && times[i] && (
                  <text x={x(i)} y={y + 40} textAnchor="middle" fontFamily="var(--mono)" fontSize={9.5} fill="var(--ink-2)">
                    {clock(times[i])}
                  </text>
                )}
              </>
            )}
          </g>
        );
      })}
    </svg>
  );
}
