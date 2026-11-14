import { Loader2 } from "lucide-react";
import { useState, type FormEvent } from "react";
import { useSystemInfo } from "../api/resources";
import { signIn } from "../api/session";
import { ProblemAlert } from "../components/ui";

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
    <div className="signin">
      <div className="brandside">
        <b className="word">
          SWIFT<i>FLOW</i>
        </b>
        <p>Order fulfillment that never oversells.</p>
        <svg className="art" viewBox="0 0 420 120" role="img" aria-label="An order's journey: placed, reserved, paid, confirmed, shipped" style={{ maxWidth: 420, marginTop: 24 }}>
          <line x1="20" y1="30" x2="400" y2="30" stroke="#3A4654" strokeWidth="6" strokeLinecap="round" />
          <line x1="20" y1="30" x2="210" y2="30" stroke="#fff" strokeWidth="6" strokeLinecap="round" />
          <path d="M115 30v40a10 10 0 0 0 10 10h120" fill="none" stroke="var(--stop)" strokeWidth="6" strokeLinecap="round" />
          <rect x="245" y="71" width="18" height="18" rx="3" fill="var(--stop)" />
          {[20, 115, 210, 305, 400].map((x, i) => (
            <circle key={x} cx={x} cy="30" r={i === 2 ? 12 : 9} fill={i === 2 ? "var(--signal)" : i < 2 ? "#fff" : "#141B24"} stroke={i <= 2 ? "#fff" : "#3A4654"} strokeWidth="4" />
          ))}
        </svg>
        <div className="foot">six services · one event bus · zero oversold units</div>
      </div>
      <div className="formside">
        <div>
          <h1>Sign in</h1>
          <p className="muted" style={{ margin: "6px 0 0" }}>
            Use an API client's ID and secret. You get a 15-minute token for that client's tenant.
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
              <span className="aside mono">local environment only</span>
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
                  <tr key={d.client_id}>
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
    </div>
  );
}
