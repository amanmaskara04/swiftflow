// Small shared building blocks: badge, copy, problem alert, states, dialog, drawer, toasts.
import { Check, Copy as CopyIcon, Loader2, X } from "lucide-react";
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { errorText, isApiError } from "../api/client";

export function StatusBadge({ status }: { status: string }) {
  return <span className={`badge ${status.toUpperCase()}`}>{status.replace(/_/g, " ")}</span>;
}

export function Copy({ text, label = "Copy" }: { text: string; label?: string }) {
  const [done, setDone] = useState(false);
  return (
    <span className="copy">
      <button
        type="button"
        aria-label={done ? "Copied" : label}
        title={label}
        onClick={() => {
          void navigator.clipboard?.writeText(text).then(() => {
            setDone(true);
            setTimeout(() => setDone(false), 1500);
          });
        }}
      >
        {done ? <Check /> : <CopyIcon />}
      </button>
    </span>
  );
}

/** Renders an RFC 9457 problem: title, detail, field errors and the request ID for support. */
export function ProblemAlert({ error }: { error: unknown }) {
  if (!error) return null;
  const t = errorText(error);
  const errs = isApiError(error) ? (error.problem.errors ?? []) : [];
  return (
    <div className="problem" role="alert">
      <b>{t.title}</b>
      {t.detail && <span>{t.detail}</span>}
      {errs.length > 0 && (
        <ul>
          {errs.slice(0, 5).map((e, i) => (
            <li key={i}>
              {(e.loc ?? []).filter((p) => p !== "body").join(".")} {e.msg ?? e.message}
            </li>
          ))}
        </ul>
      )}
      {t.requestId && <span className="rid">request {t.requestId}</span>}
    </div>
  );
}

export function Loading({ label = "Loading" }: { label?: string }) {
  return (
    <div className="state" role="status">
      <Loader2 className="spin" size={20} />
      <span>{label}…</span>
    </div>
  );
}

export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="state">
      <b>{title}</b>
      {children && <span>{children}</span>}
    </div>
  );
}

export function ErrorState({ error, retry }: { error: unknown; retry?: () => void }) {
  return (
    <div className="state err">
      <ProblemAlert error={error} />
      {retry && (
        <button className="btn sm" onClick={retry}>
          Try again
        </button>
      )}
    </div>
  );
}

// ---------------------------------------------------------------- focus-trapped overlays
function useOverlay(onClose: () => void) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    const el = ref.current;
    const focusables = () =>
      Array.from(el?.querySelectorAll<HTMLElement>('button:not(:disabled), [href], input:not(:disabled), select, textarea, [tabindex]:not([tabindex="-1"])') ?? []);
    (focusables().find((f) => f.hasAttribute("data-autofocus")) ?? focusables()[0] ?? el)?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        onClose();
      } else if (e.key === "Tab") {
        const f = focusables();
        if (!f.length) return;
        const first = f[0]!;
        const last = f[f.length - 1]!;
        if (e.shiftKey && document.activeElement === first) {
          e.preventDefault();
          last.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault();
          first.focus();
        }
      }
    };
    document.addEventListener("keydown", onKey, true);
    return () => {
      document.removeEventListener("keydown", onKey, true);
      previous?.focus?.();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  return ref;
}

export function Dialog({ title, onClose, children, footer, wide }: { title: string; onClose: () => void; children: ReactNode; footer?: ReactNode; wide?: boolean }) {
  const ref = useOverlay(onClose);
  return (
    <div className="dialog-wrap">
      <div className="scrim" onClick={onClose} />
      <div className={`dialog${wide ? " wide" : ""}`} role="dialog" aria-modal="true" aria-label={title} ref={ref} tabIndex={-1}>
        <header>
          <h2>{title}</h2>
          <button className="x" aria-label="Close" onClick={onClose}>
            <X size={18} />
          </button>
        </header>
        <div className="body">{children}</div>
        {footer && <footer>{footer}</footer>}
      </div>
    </div>
  );
}

export function Drawer({ label, onClose, children }: { label: string; onClose: () => void; children: ReactNode }) {
  const ref = useOverlay(onClose);
  return (
    <>
      <div className="scrim" onClick={onClose} />
      <aside className="drawer" role="dialog" aria-modal="true" aria-label={label} ref={ref} tabIndex={-1}>
        {children}
      </aside>
    </>
  );
}

// ---------------------------------------------------------------- toasts
interface Toast {
  id: number;
  kind: "ok" | "err";
  title: string;
  detail?: string;
  rid?: string | null;
}
const ToastCtx = createContext<{ ok: (title: string, detail?: string) => void; fail: (e: unknown) => void } | null>(null);

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<Toast[]>([]);
  const next = useRef(1);
  const push = useCallback((t: Omit<Toast, "id">) => {
    const id = next.current++;
    setItems((cur) => [...cur.slice(-3), { ...t, id }]);
    setTimeout(() => setItems((cur) => cur.filter((x) => x.id !== id)), t.kind === "err" ? 8000 : 3500);
  }, []);
  const value = useMemo(
    () => ({
      ok: (title: string, detail?: string) => push({ kind: "ok", title, detail }),
      fail: (e: unknown) => {
        const t = errorText(e);
        push({ kind: "err", title: t.title, detail: t.detail, rid: t.requestId });
      },
    }),
    [push],
  );
  return (
    <ToastCtx.Provider value={value}>
      {children}
      <div className="toasts" role="status" aria-live="polite">
        {items.map((t) => (
          <div key={t.id} className={`toast ${t.kind}`}>
            <i />
            <div className="t">
              <b>{t.title}</b>
              {t.detail && <div>{t.detail}</div>}
              {t.rid && <small>request {t.rid}</small>}
            </div>
            <button aria-label="Dismiss" onClick={() => setItems((cur) => cur.filter((x) => x.id !== t.id))}>
              <X size={14} />
            </button>
          </div>
        ))}
      </div>
    </ToastCtx.Provider>
  );
}

export function useToast() {
  const c = useContext(ToastCtx);
  if (!c) throw new Error("useToast outside ToastProvider");
  return c;
}

export function Head({ title, sub, children }: { title: string; sub: string; children?: ReactNode }) {
  return (
    <div className="head">
      <div>
        <h1>{title}</h1>
        <p>{sub}</p>
      </div>
      {children && <div className="act">{children}</div>}
    </div>
  );
}
