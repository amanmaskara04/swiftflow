// Shell: sign-in gate, dark sidebar, top bar with the health strip, routes.
import { useQueryClient } from "@tanstack/react-query";
import { Activity, AlertTriangle, Boxes, List, LogOut, Network, Webhook } from "lucide-react";
import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import { Link, NavLink, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { LiveProvider, useLive } from "./api/live";
import { useHealth, useSimConfig } from "./api/resources";
import { getSession, minutesLeft, signOut, subscribe, type Session } from "./api/session";
import { ThemeToggle } from "./components/ThemeToggle";
import { ChaosLab } from "./screens/ChaosLab";
import { Orders } from "./screens/Orders";
import { Overview } from "./screens/Overview";
import { SignIn } from "./screens/SignIn";
import { Stock } from "./screens/Stock";
import { SystemMap } from "./screens/SystemMap";
import { Webhooks } from "./screens/Webhooks";

let snapshot: Session | null = getSession();
function useSession(): Session | null {
  return useSyncExternalStore(
    (cb) =>
      subscribe((s) => {
        snapshot = s;
        cb();
      }),
    () => snapshot,
  );
}

const STRIP: Array<[id: string, label: string]> = [
  ["gateway", "gateway"],
  ["order-api", "order"],
  ["inventory-api", "inventory"],
  ["notification-api", "notification"],
  ["partner-sim", "partner-sim"],
  ["kafka", "kafka"],
];

function HealthStrip() {
  const health = useHealth();
  const nodes = new Map((health.data?.nodes ?? []).map((n) => [n.id, n]));
  return (
    <div className="health" aria-label="Service health" aria-live="polite">
      {STRIP.map(([id, label]) => {
        const n = nodes.get(id);
        const state = health.isError ? "down" : !n ? "unknown" : n.status === "up" ? "" : n.status === "down" ? "down" : "unknown";
        return (
          <span key={id} className={state} title={`${label}: ${state || "up"}${n?.latency_ms != null ? `, ${n.latency_ms} ms` : ""}${n?.error ? `, ${n.error}` : ""}`}>
            <i />
            <b>{label}</b> {n?.port ?? ""}
            <span className="sr-only">{state || "up"}</span>
          </span>
        );
      })}
    </div>
  );
}

function LiveDot() {
  const { status } = useLive();
  const cls = status === "live" ? "on" : status === "reconnecting" ? "retry" : "";
  return (
    <span className={`livedot ${cls}`} title="Live event stream">
      <i />
      {status === "live" ? "live" : status}
    </span>
  );
}

function FaultBar() {
  const cfg = useSimConfig();
  const c = cfg.data;
  if (!c || (!c.outage && c.failure_rate === 0 && c.latency_ms <= 500)) return null;
  const parts = [c.outage && "payment provider outage", c.failure_rate > 0 && `${Math.round(c.failure_rate * 100)} % of payments declined`, c.latency_ms > 500 && `${c.latency_ms} ms added latency`].filter(Boolean);
  return (
    <div className="faultbar" role="status">
      <AlertTriangle size={15} />
      <span>
        <b>Fault injected:</b> {parts.join(", ")}.
      </span>
      <Link to="/chaos">Open chaos lab</Link>
    </div>
  );
}

function Shell({ session }: { session: Session }) {
  const [, tick] = useState(0);
  useEffect(() => {
    const t = setInterval(() => tick((n) => n + 1), 30_000);
    return () => clearInterval(t);
  }, []);
  const admin = session.scopes.includes("admin");
  // Re-run the entry animation when the section changes, not when a drawer opens within it.
  const section = useLocation().pathname.split("/")[1] ?? "";
  return (
    <div className="app">
      <a className="skip" href="#main">
        Skip to content
      </a>
      <aside className="side">
        <div className="brand">
          <b>
            SWIFT<i>FLOW</i>
          </b>
          <span>console · {location.port === "5173" ? "dev" : "local"}</span>
        </div>
        <nav className="nav" aria-label="Main">
          <NavLink to="/" end>
            <Activity /> Overview
          </NavLink>
          <NavLink to="/orders">
            <List /> Orders
          </NavLink>
          <NavLink to="/stock">
            <Boxes /> Stock
          </NavLink>
          <NavLink to="/webhooks">
            <Webhook /> Webhooks
          </NavLink>
          <div className="sep">Under the hood</div>
          <NavLink to="/system">
            <Network /> System map
          </NavLink>
          {admin && (
            <NavLink to="/chaos">
              <AlertTriangle /> Chaos lab
            </NavLink>
          )}
        </nav>
        <div className="who">
          <div>
            {session.clientId}
            <small>
              {session.role} · token {minutesLeft()} min left
            </small>
          </div>
          <button aria-label="Sign out" title="Sign out" onClick={() => signOut()}>
            <LogOut size={15} />
          </button>
        </div>
      </aside>
      <div style={{ minWidth: 0 }}>
        <div className="top">
          <span className="tenant">{session.tenantName || "Tenant"}</span>
          <LiveDot />
          <HealthStrip />
          <ThemeToggle />
        </div>
        {admin && <FaultBar />}
        <main id="main" key={section} className="enter">
          <Routes>
            <Route path="/" element={<Overview />} />
            <Route path="/orders" element={<Orders />} />
            <Route path="/orders/:id" element={<Orders />} />
            <Route path="/stock" element={<Stock />} />
            <Route path="/webhooks" element={<Webhooks />} />
            <Route path="/system" element={<SystemMap />} />
            <Route path="/chaos" element={admin ? <ChaosLab /> : <Navigate to="/" replace />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </main>
      </div>
    </div>
  );
}

export function App() {
  const session = useSession();
  const qc = useQueryClient();
  // Never show one tenant's cached data to the next client that signs in.
  const had = useRef(session !== null);
  useEffect(() => {
    if (had.current && !session) qc.clear();
    had.current = session !== null;
  }, [session, qc]);
  if (!session) return <SignIn />;
  return (
    <LiveProvider key={session.clientId}>
      <Shell session={session} />
    </LiveProvider>
  );
}
