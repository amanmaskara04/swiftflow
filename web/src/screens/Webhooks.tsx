// Webhooks: endpoints (with a one-time signing secret), deliveries with retry,
// and the simulator's test receiver showing whether each signature verified.
import { useQueryClient } from "@tanstack/react-query";
import { Loader2, Plus, Send, Trash2 } from "lucide-react";
import { useState } from "react";
import { api } from "../api/client";
import { qk, useDeliveries, useReceiver, useSubscriptions, useSystemInfo } from "../api/resources";
import { getSession, isAdmin } from "../api/session";
import { EVENT_TYPES, type WebhookSubscription } from "../api/types";
import { Copy, Dialog, Empty, ErrorState, Head, Loading, ProblemAlert, StatusBadge, useToast } from "../components/ui";
import { clock, dateTime, json, relative, shortId } from "../lib/format";

const INBOX = "demo";

function tenantSlug(): string {
  try {
    const part = getSession()?.token.split(".")[1] ?? "";
    const b64 = part.replace(/-/g, "+").replace(/_/g, "/");
    const claims = JSON.parse(atob(b64.padEnd(Math.ceil(b64.length / 4) * 4, "="))) as { tenant_slug?: string };
    return claims.tenant_slug ?? "";
  } catch {
    return "";
  }
}

function SecretPanel({ sub, onClose }: { sub: WebhookSubscription; onClose: () => void }) {
  return (
    <Dialog title="Endpoint added" onClose={onClose} footer={<button className="btn primary" onClick={onClose}>I've copied the secret</button>}>
      <div className="notice warn">
        <span>
          Copy the signing secret now — it won't be shown again. Use it to check the <span className="mono">SwiftFlow-Signature</span> header on every delivery.
        </span>
      </div>
      <div className="field">
        <span>Signing secret</span>
        <div className="keybox">
          <span>{sub.signing_secret}</span>
          <Copy text={sub.signing_secret ?? ""} label="Copy secret" />
        </div>
      </div>
      <div className="muted mono" style={{ wordBreak: "break-all" }}>
        {sub.url}
      </div>
    </Dialog>
  );
}

function AddEndpoint({ onClose, onCreated }: { onClose: () => void; onCreated: (s: WebhookSubscription) => void }) {
  const [url, setUrl] = useState("https://");
  const [types, setTypes] = useState<string[]>(["OrderConfirmed", "OrderCancelled"]);
  const [all, setAll] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  async function save() {
    setBusy(true);
    setError(null);
    try {
      const r = await api.post<WebhookSubscription>("/v1/webhooks/subscriptions", { url: url.trim(), event_types: all ? ["*"] : types });
      onCreated(r.data);
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog
      title="Add endpoint"
      onClose={onClose}
      wide
      footer={
        <>
          <button className="btn ghost" onClick={onClose}>
            Close
          </button>
          <button className="btn primary" disabled={busy || url.trim().length < 9 || (!all && types.length === 0)} onClick={() => void save()}>
            {busy && <Loader2 className="spin" />} Add endpoint
          </button>
        </>
      }
    >
      <label className="field">
        <span>Endpoint URL</span>
        <input className="input mono" value={url} onChange={(e) => setUrl(e.target.value)} maxLength={2000} spellCheck={false} data-autofocus />
        <small>Must be https and reachable from the internet. Private and local addresses are refused.</small>
      </label>
      <fieldset className="field" style={{ border: 0, padding: 0, margin: 0 }}>
        <span>Events to send</span>
        <label style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <input type="checkbox" checked={all} onChange={(e) => setAll(e.target.checked)} /> Every event
        </label>
        {!all && (
          <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
            {EVENT_TYPES.map((t) => (
              <button key={t} type="button" className="chip" aria-pressed={types.includes(t)} onClick={() => setTypes((cur) => (cur.includes(t) ? cur.filter((x) => x !== t) : [...cur, t]))}>
                {t}
              </button>
            ))}
          </div>
        )}
      </fieldset>
      <ProblemAlert error={error} />
    </Dialog>
  );
}

function Endpoints() {
  const subs = useSubscriptions();
  const info = useSystemInfo();
  const qc = useQueryClient();
  const toast = useToast();
  const admin = isAdmin();
  const [adding, setAdding] = useState(false);
  const [created, setCreated] = useState<WebhookSubscription | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const receiver = info.data?.webhook_test_receiver_url;
  const refresh = () => void qc.invalidateQueries({ queryKey: qk.subs });

  async function useTestReceiver() {
    setBusy("receiver");
    try {
      const r = await api.post<WebhookSubscription>("/v1/webhooks/subscriptions", { url: `${receiver}/${tenantSlug()}-${INBOX}`, event_types: ["*"] });
      // Give the simulator the secret so it can verify signatures like a real partner would.
      await api.put(`/v1/sim/hooks/${INBOX}/secret`, { secret: r.data.signing_secret });
      await api.post(`/v1/webhooks/subscriptions/${r.data.id}/test`);
      toast.ok("Endpoint added", "The test receiver now verifies every delivery's signature.");
      refresh();
    } catch (e) {
      toast.fail(e);
    } finally {
      setBusy(null);
    }
  }

  async function run(id: string, what: "test" | "delete") {
    setBusy(id + what);
    try {
      if (what === "test") {
        await api.post(`/v1/webhooks/subscriptions/${id}/test`);
        toast.ok("Test delivery queued");
        void qc.invalidateQueries({ queryKey: ["webhooks", "deliveries"] });
      } else {
        await api.del(`/v1/webhooks/subscriptions/${id}`);
        toast.ok("Endpoint removed");
        refresh();
      }
    } catch (e) {
      toast.fail(e);
    } finally {
      setBusy(null);
    }
  }

  const items = subs.data ?? [];
  return (
    <>
      <div className="filters">
        <button className="btn primary" onClick={() => setAdding(true)}>
          <Plus /> Add endpoint
        </button>
        {admin && receiver && (
          <button className="btn" disabled={busy === "receiver"} onClick={() => void useTestReceiver()}>
            {busy === "receiver" && <Loader2 className="spin" />} Use the test receiver
          </button>
        )}
      </div>
      <div className="card">
        {subs.isLoading ? (
          <Loading label="Loading endpoints" />
        ) : subs.isError ? (
          <ErrorState error={subs.error} retry={() => void subs.refetch()} />
        ) : items.length === 0 ? (
          <Empty title="No endpoints yet">Add one to be told the moment an order is confirmed, cancelled or shipped.</Empty>
        ) : (
          <table className="stack">
            <thead>
              <tr>
                <th>Endpoint</th>
                <th>Events</th>
                <th>Added</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {items.map((s) => (
                <tr key={s.id}>
                  <td data-label="Endpoint" className="mono" style={{ wordBreak: "break-all" }}>
                    {s.url}
                  </td>
                  <td data-label="Events">
                    <span style={{ display: "inline-flex", gap: 4, flexWrap: "wrap" }}>
                      {s.event_types.map((t) => (
                        <span key={t} className="tag">
                          {t === "*" ? "every event" : t}
                        </span>
                      ))}
                    </span>
                  </td>
                  <td data-label="Added" className="mono">
                    {dateTime(s.created_at)}
                  </td>
                  <td className="r" style={{ whiteSpace: "nowrap" }}>
                    <button className="btn sm ghost" disabled={busy !== null} onClick={() => void run(s.id, "test")}>
                      <Send /> Send test
                    </button>{" "}
                    <button className="btn sm danger" disabled={busy !== null} onClick={() => void run(s.id, "delete")} aria-label="Remove endpoint">
                      <Trash2 />
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
      {adding && (
        <AddEndpoint
          onClose={() => setAdding(false)}
          onCreated={(s) => {
            setAdding(false);
            setCreated(s);
            refresh();
          }}
        />
      )}
      {created && <SecretPanel sub={created} onClose={() => setCreated(null)} />}
    </>
  );
}

function Deliveries() {
  const [status, setStatus] = useState("");
  const d = useDeliveries(status);
  const qc = useQueryClient();
  const toast = useToast();
  const items = d.data?.pages.flatMap((p) => p.items) ?? [];

  async function retry(id: string) {
    try {
      await api.post(`/v1/webhooks/deliveries/${id}/retry`);
      toast.ok("Delivery queued again");
      void qc.invalidateQueries({ queryKey: ["webhooks", "deliveries"] });
    } catch (e) {
      toast.fail(e);
    }
  }

  return (
    <>
      <div className="toolbar">
        <div className="pills" role="group" aria-label="Filter by delivery status">
          {[["", "All"], ["PENDING", "Pending"], ["DELIVERED", "Delivered"], ["FAILED", "Failed"]].map(([v, l]) => (
            <button key={v} aria-pressed={status === v} onClick={() => setStatus(v!)}>
              {v && <i className={`dot ${v}`} />}
              {l}
            </button>
          ))}
        </div>
      </div>
      <div className="card">
        {d.isLoading ? (
          <Loading label="Loading deliveries" />
        ) : d.isError ? (
          <ErrorState error={d.error} retry={() => void d.refetch()} />
        ) : items.length === 0 ? (
          <Empty title="No deliveries">Deliveries appear here once an endpoint is added and an event happens.</Empty>
        ) : (
          <>
            <table className="stack">
              <thead>
                <tr>
                  <th>Status</th>
                  <th>Event</th>
                  <th className="r">Attempts</th>
                  <th>Next attempt</th>
                  <th>Last response</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {items.map((x) => (
                  <tr key={x.id}>
                    <td data-label="Status">
                      <StatusBadge status={x.status === "PENDING" ? "QUEUED" : x.status} />
                    </td>
                    <td data-label="Event">
                      {x.event_type} <span className="mono muted">#{shortId(x.event_id)}</span>
                    </td>
                    <td data-label="Attempts" className="mono r">
                      {x.attempts}
                    </td>
                    <td data-label="Next attempt" className="mono">
                      {x.status === "PENDING" ? relative(x.next_attempt_at) : "—"}
                    </td>
                    <td data-label="Last response">
                      {x.last_status_code ? <span className="mono">{x.last_status_code} </span> : null}
                      {x.last_error ?? (x.status === "DELIVERED" ? "" : "—")}
                    </td>
                    <td className="r">
                      {x.status === "FAILED" && (
                        <button className="btn sm" onClick={() => void retry(x.id)}>
                          Retry delivery
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {d.hasNextPage && (
              <div className="tablefoot">
                <button className="btn sm" onClick={() => void d.fetchNextPage()}>
                  Load more
                </button>
              </div>
            )}
          </>
        )}
      </div>
    </>
  );
}

function Receiver() {
  const r = useReceiver(INBOX);
  const items = r.data ?? [];
  return (
    <div className="card">
      <header>
        <span className="cap">Test receiver</span>
        <span className="aside mono">partner-sim :8004 · inbox {tenantSlug()}-{INBOX}</span>
      </header>
      {r.isLoading ? (
        <Loading label="Loading received webhooks" />
      ) : r.isError ? (
        <ErrorState error={r.error} retry={() => void r.refetch()} />
      ) : items.length === 0 ? (
        <Empty title="Nothing received yet">Choose “Use the test receiver” on the Endpoints tab, then place an order.</Empty>
      ) : (
        <div style={{ padding: "6px 18px 12px" }}>
          {items.slice(0, 30).map((h, i) => {
            const body = (h.body ?? {}) as { event_type?: string };
            return (
              <div className="evrow" key={h.id ?? i}>
                <time className="mono">{clock(h.received_at)}</time>
                <details>
                  <summary>
                    <b>{body.event_type ?? h.headers["swiftflow-event"] ?? "Webhook"}</b>
                    <span className={`stamp sm${h.signature_valid === false ? " fail" : h.signature_valid === null ? " run" : ""}`}>
                      {h.signature_valid === true ? "SIGNATURE VALID" : h.signature_valid === false ? "SIGNATURE INVALID" : "NOT CHECKED"}
                    </span>
                  </summary>
                  <pre>{json({ headers: h.headers, body: h.body })}</pre>
                </details>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

export function Webhooks() {
  const [tab, setTab] = useState<"endpoints" | "deliveries" | "receiver">("endpoints");
  const admin = isAdmin();
  return (
    <>
      <Head title="Webhooks" sub="Tell your own systems the moment something happens to an order or to stock." />
      <div className="tabs" role="tablist">
        <button role="tab" aria-selected={tab === "endpoints"} onClick={() => setTab("endpoints")}>
          Endpoints
        </button>
        <button role="tab" aria-selected={tab === "deliveries"} onClick={() => setTab("deliveries")}>
          Deliveries
        </button>
        {admin && (
          <button role="tab" aria-selected={tab === "receiver"} onClick={() => setTab("receiver")}>
            Test receiver
          </button>
        )}
      </div>
      {tab === "endpoints" ? <Endpoints /> : tab === "deliveries" ? <Deliveries /> : <Receiver />}
    </>
  );
}
