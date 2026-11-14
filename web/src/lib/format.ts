// Formatting helpers: money, time, ids. All server data is rendered as text.

const usd = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" });
const int = new Intl.NumberFormat("en-US");

export function money(cents: number | null | undefined): string {
  if (cents === null || cents === undefined || Number.isNaN(cents)) return "—";
  return usd.format(cents / 100);
}

export function count(n: number | null | undefined): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return int.format(n);
}

export function shortId(id: string | null | undefined, len = 8): string {
  if (!id) return "—";
  return id.replace(/-/g, "").slice(0, len);
}

function pad(n: number, w = 2): string {
  return String(n).padStart(w, "0");
}

/** 10:15:00.12 — local clock time with centiseconds, as on the rail. */
export function clock(iso: string | null | undefined, withCs = true): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  const base = `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  return withCs ? `${base}.${pad(Math.floor(d.getMilliseconds() / 10))}` : base;
}

export function dateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

/** Compact age: 3s, 2m, 1h, 4d. */
export function age(iso: string | null | undefined, now = Date.now()): string {
  if (!iso) return "—";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "—";
  const s = Math.max(0, Math.round((now - t) / 1000));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  if (h < 48) return `${h}h`;
  return `${Math.floor(h / 24)}d`;
}

/** Relative time for a future or past instant: "in 25 s", "2 min ago". */
export function relative(iso: string | null | undefined, now = Date.now()): string {
  if (!iso) return "—";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "—";
  const diff = Math.round((t - now) / 1000);
  const abs = Math.abs(diff);
  let txt: string;
  if (abs < 60) txt = `${abs} s`;
  else if (abs < 3600) txt = `${Math.round(abs / 60)} min`;
  else if (abs < 172800) txt = `${Math.round(abs / 3600)} h`;
  else txt = `${Math.round(abs / 86400)} d`;
  if (abs < 2) return "now";
  return diff > 0 ? `in ${txt}` : `${txt} ago`;
}

export function seconds(ms: number | null | undefined, digits = 2): string {
  if (ms === null || ms === undefined || Number.isNaN(ms)) return "—";
  return (ms / 1000).toFixed(digits);
}

export function percent(x: number | null | undefined, digits = 1): string {
  if (x === null || x === undefined || Number.isNaN(x)) return "—";
  return (x * 100).toFixed(digits);
}

/** Pretty-print any JSON value as text (never as HTML). */
export function json(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2) ?? "null";
  } catch {
    return String(value);
  }
}

export function sentence(s: string): string {
  return s.replace(/_/g, " ");
}
