// Landing page: what SwiftFlow is and what it proves, beside the sign-in form.
import { Activity, ArrowUpRight, Loader2, Lock, Repeat2, Undo2 } from "lucide-react";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { useSystemInfo } from "../api/resources";
import { signIn } from "../api/session";
import { ThemeToggle } from "../components/ThemeToggle";
import { ProblemAlert } from "../components/ui";
import { reducedMotion } from "../lib/theme";

const REPO = "https://github.com/amanmaskara04/swiftflow";
const STATIONS = ["Placed", "Reserved", "Paid", "Confirmed", "Shipped"];
const X = [24, 122, 220, 318, 416];

// One order that ships, then one whose payment is declined and whose stock is released.
const FRAMES: Array<{ step: number; fail?: boolean; event: string }> = [
  { step: 0, event: "OrderPlaced" },
  { step: 1, event: "StockReserved" },
  { step: 2, event: "PaymentApproved" },
  { step: 3, event: "OrderConfirmed" },
  { step: 4, event: "OrderShipped" },
  { step: 4, event: "OrderShipped" },
  { step: 0, event: "OrderPlaced" },
  { step: 1, event: "StockReserved" },
  { step: 1, fail: true, event: "PaymentDeclined → StockReleased" },
  { step: 1, fail: true, event: "OrderCancelled, nothing oversold" },
  { step: 1, fail: true, event: "OrderCancelled, nothing oversold" },
];

function Journey() {
  const still = reducedMotion();
  const [n, setN] = useState(still ? 3 : 0);
  useEffect(() => {
    if (still) return;
    const t = setInterval(() => setN((v) => (v + 1) % FRAMES.length), 1100);
    return () => clearInterval(t);
  }, [still]);
  const f = FRAMES[n]!;
  return (
    <figure className="journey">
      <svg viewBox="0 0 440 104" role="img" aria-label="An order moves from placed to shipped; when a payment is declined, its reserved stock is released">
        {STATIONS.map((s, i) => (
          <text key={s} x={X[i]} y="12" textAnchor={i === 0 ? "start" : i === 4 ? "end" : "middle"} dx={i === 0 ? -8 : i === 4 ? 8 : 0} className={i <= f.step && !(f.fail && i > 1) ? "lbl on" : "lbl"}>
            {s.toUpperCase()}
          </text>
        ))}
        <line className="track" x1={X[0]} y1="36" x2={X[4]} y2="36" />
        <line className="fill" x1={X[0]} y1="36" x2={X[4]} y2="36" style={{ transform: `scaleX(${f.step / 4})` }} />
        <path className={f.fail ? "branch on" : "branch"} d={`M${X[1]} 36v34a10 10 0 0 0 10 10h112`} pathLength={1} />
        <g className={f.fail ? "end on" : "end"}>
          <rect x={X[1]! + 122} y="71" width="18" height="18" rx="3" />
          <text x={X[1]! + 150} y="84">CANCELLED</text>
        </g>
        {X.map((x, i) => (
          <circle key={x} cx={x} cy="36" r={i === f.step && !f.fail ? 11 : 8} className={i === f.step && !f.fail ? "node now" : i <= f.step ? "node on" : "node"} />
        ))}
      </svg>
      <figcaption className="mono" aria-hidden="true">
        <i className={f.fail ? "bad" : ""} />
        <span key={f.event}>{f.event}</span>
      </figcaption>
    </figure>
  );
}

/** Counts up to `to` once, when the page opens. */
function Count({ to }: { to: number }) {
  const [v, setV] = useState(reducedMotion() ? to : 0);
  const raf = useRef(0);
  useEffect(() => {
    if (reducedMotion()) return;
    const start = performance.now();
    const run = (now: number) => {
      const p = Math.min(1, (now - start - 350) / 1100);
      setV(Math.round(to * (p <= 0 ? 0 : 1 - Math.pow(1 - p, 3))));
      if (p < 1) raf.current = requestAnimationFrame(run);
    };
    raf.current = requestAnimationFrame(run);
    return () => cancelAnimationFrame(raf.current);
  }, [to]);
  return <>{v}</>;
}

const PROOF: Array<{ n: number; of?: string; label: string; detail: string }> = [
  { n: 0, label: "units oversold", detail: "500 concurrent orders against 100 units: exactly 100 confirmed" },
  { n: 12, of: "/12", label: "invariants hold", detail: "workers killed and the Kafka broker restarted mid-order" },
  { n: 81, label: "attacks refused", detail: "forged tokens, cross-tenant reads, injection, SSRF" },
  { n: 6, label: "services", detail: "each on its own port, joined by Kafka and one gateway" },
];

const HOW = [
  { icon: Repeat2, title: "No lost or duplicated events", text: "A transactional outbox writes each event in the same commit as the data; an idempotent inbox makes redelivery harmless." },
  { icon: Undo2, title: "Failures undo themselves", text: "An orchestrated saga releases reserved stock when a payment is declined and refunds when a paid order is cancelled." },
  { icon: Lock, title: "Tenants cannot see each other", text: "Postgres row-level security is forced on every table, and the tenant comes only from the signed token." },
  { icon: Activity, title: "Every order is traceable", text: "OpenTelemetry traces cross HTTP and Kafka; health, lag and errors show on the live system map." },
];

const STACK = ["FastAPI", "Kafka", "PostgreSQL", "Redis", "React", "Docker", "OpenTelemetry"];

export function SignIn() {
  const info = useSystemInfo();
  const [clientId, setClientId] = useState("");
  const [secret, setSecret] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await signIn(clientId.trim(), secret);
    } catch (err) {
      setError(err);
      setBusy(false);
    }
  }

  const demo = info.data?.demo_clients ?? [];
  return (
    <div className="landing">
      <div className="belt" aria-hidden="true" />
      <header className="hero">
        <div className="bar rise" style={{ "--i": 0 } as React.CSSProperties}>
          <b className="word">
            SWIFT<i>FLOW</i>
          </b>
          <a className="repo" href={REPO} target="_blank" rel="noreferrer">
            Source on GitHub <ArrowUpRight size={14} />
          </a>
        </div>
        <h1 className="rise" style={{ "--i": 1 } as React.CSSProperties}>
          Order fulfillment that <em>never oversells.</em>
        </h1>
        <p className="lede rise" style={{ "--i": 2 } as React.CSSProperties}>
          A multi-tenant order and inventory platform built as six services on an event bus. Stock is reserved, paid for and shipped through a saga
          that stays correct when processes crash, messages repeat and payments fail.
        </p>
        <div className="rise" style={{ "--i": 3 } as React.CSSProperties}>
          <Journey />
        </div>
      </header>

      <section className="details" aria-label="About the project">
        <div className="cap rise" style={{ "--i": 4 } as React.CSSProperties}>
          Measured, not claimed
        </div>
        <dl className="proof">
          {PROOF.map((p, i) => (
            <div key={p.label} className="rise" style={{ "--i": 5 + i } as React.CSSProperties}>
              <dt>
                <span className="fig">
                  <Count to={p.n} />
                  {p.of && <small>{p.of}</small>}
                </span>
                {p.label}
              </dt>
              <dd>{p.detail}</dd>
            </div>
          ))}
        </dl>

        <div className="cap rise" style={{ "--i": 9 } as React.CSSProperties}>
          How it stays correct
        </div>
        <ul className="how">
          {HOW.map((h, i) => (
            <li key={h.title} className="rise" style={{ "--i": 10 + i } as React.CSSProperties}>
              <h.icon size={18} aria-hidden="true" />
              <div>
                <b>{h.title}</b>
                <p>{h.text}</p>
              </div>
            </li>
          ))}
        </ul>

        <div className="techs rise" style={{ "--i": 14 } as React.CSSProperties}>
          {STACK.map((s) => (
            <span key={s} className="tag">
              {s}
            </span>
          ))}
        </div>
      </section>

      <main className="formside">
        <div className="themeslot">
          <ThemeToggle />
        </div>
        <div className="formcol rise" style={{ "--i": 2 } as React.CSSProperties}>
          <div>
            <h2>Open the console</h2>
            <p className="muted" style={{ margin: "6px 0 0" }}>
              Sign in with an API client's ID and secret. You get a 15-minute token for that client's tenant.
            </p>
          </div>
          <form onSubmit={submit}>
            <label className="field">
              <span>Client ID</span>
              <input className="input mono" value={clientId} onChange={(e) => setClientId(e.target.value)} autoComplete="username" required maxLength={100} spellCheck={false} />
            </label>
            <label className="field">
              <span>Client secret</span>
              <input className="input mono" type="password" value={secret} onChange={(e) => setSecret(e.target.value)} autoComplete="current-password" required maxLength={200} />
            </label>
            <ProblemAlert error={error} />
            <div>
              <button className="btn primary" disabled={busy || !clientId || !secret}>
                {busy && <Loader2 className="spin" />} Sign in
              </button>
            </div>
          </form>
          {demo.length > 0 && (
            <div className="card">
              <header>
                <span className="cap">Demo clients</span>
                <span className="aside mono">pick one to fill the form</span>
              </header>
              <table className="stack">
                <thead>
                  <tr>
                    <th>Client</th>
                    <th>Role</th>
                    <th>Tenant</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {demo.map((d) => (
                    <tr key={d.client_id} className={d.client_id === clientId ? "sel" : ""}>
                      <td data-label="Client" className="mono">
                        {d.client_id}
                      </td>
                      <td data-label="Role">{d.role}</td>
                      <td data-label="Tenant">{d.tenant}</td>
                      <td className="r">
                        <button
                          type="button"
                          className="btn sm"
                          onClick={() => {
                            setClientId(d.client_id);
                            setSecret(d.client_secret);
                            setError(null);
                          }}
                        >
                          Use
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {info.isError && <p className="muted">The gateway isn't answering on this address. Start the stack, then reload.</p>}
        </div>
      </main>
    </div>
  );
}
