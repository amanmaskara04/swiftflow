// Session: the access token lives in memory and sessionStorage; the client secret lives in
// memory only, so after a reload an expired token sends the user back to sign-in.

import type { TokenResponse } from "./types";

export interface Session {
  token: string;
  expiresAt: number; // epoch ms
  clientId: string;
  tenantId: string;
  tenantName: string;
  role: string;
  scopes: string[];
}

const KEY = "swiftflow.session";
let current: Session | null = null;
let secret: string | null = null; // memory only — never persisted
let refreshTimer: ReturnType<typeof setTimeout> | undefined;
type Listener = (s: Session | null, reason?: SignOutReason) => void;
export type SignOutReason = "signed-out" | "expired" | "switched";
const listeners = new Set<Listener>();

function decodeClaims(jwt: string): Record<string, unknown> {
  try {
    const part = jwt.split(".")[1];
    if (!part) return {};
    const b64 = part.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(part.length / 4) * 4, "=");
    const text = new TextDecoder().decode(Uint8Array.from(atob(b64), (c) => c.charCodeAt(0)));
    const v: unknown = JSON.parse(text);
    return v && typeof v === "object" ? (v as Record<string, unknown>) : {};
  } catch {
    return {};
  }
}

function load(): Session | null {
  try {
    const raw = sessionStorage.getItem(KEY);
    if (!raw) return null;
    const s = JSON.parse(raw) as Session;
    if (!s.token || !s.expiresAt) return null;
    if (s.expiresAt <= Date.now()) {
      sessionStorage.removeItem(KEY);
      return null;
    }
    return s;
  } catch {
    return null;
  }
}

current = load();

function persist(s: Session | null) {
  try {
    if (s) sessionStorage.setItem(KEY, JSON.stringify(s));
    else sessionStorage.removeItem(KEY);
  } catch {
    /* storage unavailable: memory only */
  }
}

function emit(reason?: SignOutReason) {
  for (const l of listeners) l(current, reason);
}

export function getSession(): Session | null {
  if (current && current.expiresAt <= Date.now() && !secret) {
    // Expired and nothing to refresh with.
    current = null;
    persist(null);
  }
  return current;
}

export function hasSecret(): boolean {
  return secret !== null;
}

export function subscribe(l: Listener): () => void {
  listeners.add(l);
  return () => listeners.delete(l);
}

export function hasScope(scope: string): boolean {
  return !!current && current.scopes.includes(scope);
}

export function isAdmin(): boolean {
  return hasScope("admin");
}

/** Exchanges client credentials for a token. Throws whatever the transport throws. */
type TokenFetcher = (clientId: string, clientSecret: string) => Promise<TokenResponse>;
let fetchToken: TokenFetcher | null = null;
export function setTokenFetcher(f: TokenFetcher) {
  fetchToken = f;
}

function fromToken(clientId: string, t: TokenResponse): Session {
  const claims = decodeClaims(t.access_token);
  const scopes = (t.scope || (typeof claims.scope === "string" ? claims.scope : "")).split(/\s+/).filter(Boolean);
  const role = typeof claims.role === "string" ? claims.role : scopes.includes("admin") ? "admin" : "partner";
  const exp = typeof claims.exp === "number" ? claims.exp * 1000 : Date.now() + (t.expires_in || 900) * 1000;
  return {
    token: t.access_token,
    expiresAt: Math.min(exp, Date.now() + (t.expires_in || 900) * 1000),
    clientId,
    tenantId: t.tenant_id ?? (typeof claims.tenant_id === "string" ? claims.tenant_id : ""),
    tenantName: t.tenant_name ?? "",
    role,
    scopes,
  };
}

function scheduleRefresh() {
  clearTimeout(refreshTimer);
  if (!current || !secret) return;
  const wait = Math.max(5_000, current.expiresAt - Date.now() - 60_000);
  refreshTimer = setTimeout(() => {
    void refresh().catch(() => {
      /* the next 401 signs out */
    });
  }, wait);
}

export async function signIn(clientId: string, clientSecret: string, reason?: SignOutReason): Promise<Session> {
  if (!fetchToken) throw new Error("token fetcher not configured");
  const t = await fetchToken(clientId, clientSecret);
  current = fromToken(clientId, t);
  secret = clientSecret;
  persist(current);
  scheduleRefresh();
  emit(reason);
  return current;
}

let refreshing: Promise<boolean> | null = null;
/** Re-issues the token with the in-memory secret. Resolves false if there is no secret. */
export function refresh(): Promise<boolean> {
  if (!current || !secret || !fetchToken) return Promise.resolve(false);
  if (!refreshing) {
    const clientId = current.clientId;
    const s = secret;
    refreshing = fetchToken(clientId, s)
      .then((t) => {
        current = fromToken(clientId, t);
        persist(current);
        scheduleRefresh();
        emit();
        return true;
      })
      .catch(() => false)
      .finally(() => {
        refreshing = null;
      });
  }
  return refreshing;
}

export function signOut(reason: SignOutReason = "signed-out") {
  clearTimeout(refreshTimer);
  current = null;
  secret = null;
  persist(null);
  emit(reason);
}

export function minutesLeft(): number {
  if (!current) return 0;
  return Math.max(0, Math.round((current.expiresAt - Date.now()) / 60000));
}
