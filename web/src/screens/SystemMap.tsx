// System map: the live architecture, drawn from /v1/system/health. Event pulses run
// along the Kafka bus as events arrive on the stream.
import { useState } from "react";
import { useLive, useLiveEvents } from "../api/live";
import { useHealth } from "../api/resources";
import type { HealthNode } from "../api/types";
import { TOPIC_OF } from "../api/types";
import { ErrorState, Head, StatusBadge } from "../components/ui";
import { clock, shortId } from "../lib/format";
import { reducedMotion } from "../lib/theme";

const SERVICES = [
  { api: "order-api", worker: "order-worker", name: "ORDER", x: 60, jobs: "relay · saga · payments" },
  { api: "inventory-api", worker: "inventory-worker", name: "INVENTORY", x: 320, jobs: "relay · reservations" },
  { api: "notification-api", worker: "notification-worker", name: "NOTIFICATION", x: 580, jobs: "feed · webhooks" },
];
const PRODUCER_X: Record<string, number> = { order: 160, inventory: 420 };
const TOPICS = [
  ["orders.events", "order → inventory, notification"],
  ["inventory.events", "inventory → order, notification"],
  ["inventory.stock-changes", "inventory → notification"],
] as const;

function cls(n: HealthNode | undefined): string {
  return !n ? " unknown" : n.status === "up" ? "" : n.status === "down" ? " down" : " unknown";
}

function Light({ n, cx, cy }: { n?: HealthNode; cx: number; cy: number }) {
  const fill = !n || n.status === "unknown" ? "var(--ink-2)" : n.status === "up" ? "var(--go)" : "var(--stop)";
  return n?.status === "down" ? <rect x={cx - 4} y={cy - 4} width={8} height={8} fill={fill} /> : <circle cx={cx} cy={cy} r={4} fill={fill} />;
}

export function SystemMap() {
  const health = useHealth();
  const { lastByTopic } = useLive();
  const [pulses, setPulses] = useState<Array<{ id: string; from: number; to: number }>>([]);
  const nodes = new Map((health.data?.nodes ?? []).map((n) => [n.id, n]));
  const get = (id: string) => nodes.get(id);

  useLiveEvents((e) => {
    if (reducedMotion()) return;
    const from = PRODUCER_X[e.producer] ?? 160;
    const to = from === 160 ? 420 : 160;
    const id = e.event_id;
    setPulses((cur) => [...cur.slice(-11), { id, from, to }, { id: id + "n", from, to: 680 }]);
    setTimeout(() => setPulses((cur) => cur.filter((p) => p.id !== id && p.id !== id + "n")), 1700);
  });

  const down = (health.data?.nodes ?? []).filter((n) => n.status === "down");
  return (
    <>
      <Head title="System map" sub="Six services on six ports. The three in the middle never call each other: every hand-off is an event on Kafka." />
      {health.isError && <ErrorState error={health.error} retry={() => void health.refetch()} />}
      <div className="card map" style={{ marginBottom: 16 }}>
          <svg viewBox="0 0 1010 470" role="img" aria-label={`Architecture diagram. ${down.length ? `${down.map((d) => d.id).join(", ")} down.` : "All nodes up."}`}>
            {/* edges */}
            <path className="l" d="M420 52v26M420 122v24M160 146h520M160 146v30M420 146v30M680 146v30M160 262v70M420 262v70M680 262v70M160 340v64M420 340v64M680 340v64M250 414h340" />
            <path className="ext" d="M780 219h60" />
            <path className="ext" d="M110 262v26h805v-26" />
            <text className="lbl" x="786" y="211">webhook</text>
            <text className="lbl" x="800" y="304">charge · refund</text>

            <g>
              <rect className="n" x="340" y="14" width="160" height="38" rx="6" />
              <text className="t" x="356" y="38">CONSOLE</text>
              <text x="450" y="38">:{location.port || "3000"}</text>
            </g>
            <g>
              <rect className={`n${cls(get("gateway"))}`} x="340" y="78" width="160" height="44" rx="6" />
              <text className="t" x="356" y="105">GATEWAY</text>
              <text x="450" y="105">:8080</text>
              <Light n={get("gateway")} cx={490} cy={88} />
            </g>

            {SERVICES.map((s) => {
              const a = get(s.api);
              const w = get(s.worker);
              return (
                <g key={s.api}>
                  <rect className={`n${cls(a)}`} x={s.x} y="176" width="200" height="86" rx="6" />
                  <text className="t" x={s.x + 16} y="200">{s.name}</text>
                  <text x={s.x + 150} y="200">:{a?.port ?? ""}</text>
                  <Light n={a} cx={s.x + 190} cy={186} />
                  <rect className={`w${w?.status === "down" ? " down" : ""}`} x={s.x + 10} y="212" width="180" height="40" rx="4" />
                  <text x={s.x + 20} y="229">worker :{w?.port ?? ""}</text>
                  <text className="sub" x={s.x + 20} y="244">{w?.status === "down" ? (w.error ?? "down") : s.jobs}</text>
                  <Light n={w} cx={s.x + 180} cy={222} />
                </g>
              );
            })}

            <g>
              <rect className={`n${cls(get("partner-sim"))}`} x="840" y="176" width="150" height="86" rx="6" strokeDasharray="5 4" />
              <text className="t" x="854" y="200">PARTNER-SIM</text>
              <text x="854" y="229">:8004</text>
              <text className="sub" x="854" y="244">payments · receiver</text>
              <Light n={get("partner-sim")} cx={980} cy={186} />
            </g>

            {/* the bus */}
            <line className="bus" x1="60" y1="336" x2="780" y2="336" stroke={get("kafka")?.status === "down" ? "var(--stop)" : undefined} />
            <text className="t" x="60" y="322">KAFKA :9092</text>
            <text className="lbl" x="560" y="322">6 partitions per topic</text>
            {pulses.map((p) => (
              <circle key={p.id} className="pulse" r="7" cx={p.from} cy="336" style={{ ["--dx" as string]: `${p.to - p.from}px` }} />
            ))}

            <g>
              <rect className={`n${cls(get("postgres"))}`} x="160" y="404" width="180" height="40" rx="20" />
              <text className="t" x="184" y="429">POSTGRES</text>
              <text x="280" y="429">:5432</text>
            </g>
            <g>
              <rect className={`n${cls(get("redis"))}`} x="500" y="404" width="180" height="40" rx="20" />
              <text className="t" x="530" y="429">REDIS</text>
              <text x="620" y="429">:6379</text>
            </g>
          </svg>
        </div>
      <div className="grid two">
          <div className="card topics">
            <header>
              <span className="cap">Topics</span>
              <span className="aside mono">last event seen</span>
            </header>
            <ul>
              {TOPICS.map(([topic, flow]) => {
                const last = lastByTopic[topic];
                return (
                  <li key={topic}>
                    <span className="mono" style={{ fontWeight: 500 }}>{topic}</span>
                    <small className="muted">{flow}</small>
                    <small className="mono muted">{last ? `${clock(last.occurred_at)} · ${last.event_type} · #${shortId(last.aggregate_id)}` : "nothing since you opened the console"}</small>
                  </li>
                );
              })}
            </ul>
          </div>
          <div className="card">
            <header>
              <span className="cap">Nodes</span>
              <span className="aside mono">checked every 5 s</span>
            </header>
            <table>
              <tbody>
                {(health.data?.nodes ?? []).map((n) => (
                  <tr key={n.id}>
                    <td className="mono">{n.id}</td>
                    <td className="mono muted">:{n.port}</td>
                    <td className="mono r muted">{n.latency_ms != null ? `${n.latency_ms} ms` : ""}</td>
                    <td className="r">
                      <StatusBadge status={n.status === "up" ? "UP" : n.status === "down" ? "DOWN" : "UNKNOWN"} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
      </div>
      <p className="muted" style={{ fontSize: ".75rem" }}>Topic routing: {Object.keys(TOPIC_OF).length} event types across 3 topics, each with a dead-letter topic.</p>
    </>
  );
}
