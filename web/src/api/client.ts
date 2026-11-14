// The one API client. Relative URLs only (/v1/...), bearer token, Idempotency-Key on writes,
// RFC 9457 problem parsing, 401 → refresh or back to sign-in, 429 → wait for Retry-After.

import { newKey } from "../lib/idempotency";
import { sleep } from "../lib/pool";
import { getSession, refresh, setTokenFetcher, signOut } from "./session";
import type { Problem, TokenResponse } from "./types";

export class ApiError extends Error {
  readonly status: number;
  readonly problem: Problem;
  readonly requestId: string | null;
  readonly retryAfter: number | null;

  constructor(problem: Problem, requestId: string | null, retryAfter: number | null = null) {
    super(problem.title);
    this.name = "ApiError";
    this.status = problem.status;
    this.problem = problem;
    this.requestId = requestId;
    this.retryAfter = retryAfter;
  }

  /** Short problem type, e.g. "version-conflict". */
  get kind(): string {
    const t = this.problem.type || "";
    return t.split("/").filter(Boolean).pop() ?? "";
  }
}

export function isApiError(e: unknown): e is ApiError {
  return e instanceof ApiError;
}

export interface RequestOptions {
  method?: "GET" | "POST" | "PUT" | "DELETE";
  query?: Record<string, string | number | boolean | null | undefined | Array<string | number>>;
  body?: unknown;
  idempotencyKey?: string;
  ifMatch?: string | number;
  signal?: AbortSignal;
  auth?: boolean;
  /** How many times to wait out a 429 before giving up (default 2). */
  retry429?: number;
  /** Longest Retry-After we are willing to wait automatically, in seconds (default 10). */
  maxRetryWait?: number;
}

export interface ApiResult<T> {
  data: T;
  status: number;
  headers: Headers;
  replayed: boolean;
}

function buildUrl(path: string, query?: RequestOptions["query"]): string {
  if (!path.startsWith("/")) throw new Error("API paths must be relative to the origin");
  if (!query) return path;
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(query)) {
    if (v === undefined || v === null || v === "") continue;
    if (Array.isArray(v)) v.forEach((x) => qs.append(k, String(x)));
    else qs.append(k, String(v));
  }
  const s = qs.toString();
  return s ? `${path}?${s}` : path;
}

function parseRetryAfter(h: string | null): number | null {
  if (!h) return null;
  const n = Number(h);
  if (Number.isFinite(n)) return Math.max(0, n);
  const t = Date.parse(h);
  return Number.isNaN(t) ? null : Math.max(0, Math.round((t - Date.now()) / 1000));
}

const FALLBACK_TITLES: Record<number, string> = {
  400: "The request was not accepted",
  401: "Your session has ended",
  403: "Your client is not allowed to do this",
  404: "Not found",
  409: "That conflicts with the current state",
  413: "The request is too large",
  422: "Some fields need attention",
  429: "Too many requests",
  500: "The service hit an error",
  502: "The gateway could not reach a service",
  503: "The service is unavailable",
  504: "The service took too long to answer",
};

async function toProblem(res: Response): Promise<Problem> {
  const ct = res.headers.get("content-type") ?? "";
  let body: unknown = null;
  try {
    body = ct.includes("json") ? await res.json() : await res.text();
  } catch {
    body = null;
  }
  if (body && typeof body === "object" && "title" in body) {
    const p = body as Problem;
    return { ...p, status: typeof p.status === "number" ? p.status : res.status };
  }
  return {
    type: "about:blank",
    title: FALLBACK_TITLES[res.status] ?? `Request failed (${res.status})`,
    status: res.status,
    detail: typeof body === "string" && body.length < 300 ? body : undefined,
  };
}

export async function request<T>(path: string, opts: RequestOptions = {}): Promise<ApiResult<T>> {
  const method = opts.method ?? "GET";
  const auth = opts.auth ?? true;
  const url = buildUrl(path, opts.query);
  // A write keeps one key across every retry of this logical request.
  const key = method !== "GET" && auth ? (opts.idempotencyKey ?? newKey()) : undefined;
  const max429 = opts.retry429 ?? 2;
  const maxWait = opts.maxRetryWait ?? 10;
  let refreshed = false;
  let tries429 = 0;

  for (;;) {
    const headers: Record<string, string> = { Accept: "application/json, application/problem+json" };
    if (opts.body !== undefined) headers["Content-Type"] = "application/json";
    if (key) headers["Idempotency-Key"] = key;
    if (opts.ifMatch !== undefined) headers["If-Match"] = String(opts.ifMatch);
    if (auth) {
      const s = getSession();
      if (s) headers.Authorization = `Bearer ${s.token}`;
    }

    let res: Response;
    try {
      res = await fetch(url, {
        method,
        headers,
        body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
        signal: opts.signal,
        credentials: "same-origin",
        cache: "no-store",
      });
    } catch (e) {
      if ((e as Error)?.name === "AbortError") throw e;
      throw new ApiError(
        {
          type: "/problems/network",
          title: "Can't reach the gateway",
          status: 0,
          detail: "Check that the stack is running (.\\swiftflow.ps1 up), then try again.",
        },
        null,
      );
    }

    const requestId = res.headers.get("x-request-id");

    if (res.ok) {
      let data: unknown = null;
      if (res.status !== 204) {
        const text = await res.text();
        data = text ? JSON.parse(text) : null;
      }
      return {
        data: data as T,
        status: res.status,
        headers: res.headers,
        replayed: res.headers.get("idempotent-replayed") === "true",
      };
    }

    if (res.status === 401 && auth) {
      if (!refreshed && (await refresh())) {
        refreshed = true;
        continue;
      }
      const p = await toProblem(res);
      signOut("expired");
      throw new ApiError(p, p.request_id ?? requestId);
    }

    if (res.status === 429) {
      const wait = parseRetryAfter(res.headers.get("retry-after")) ?? 1;
      if (tries429 < max429 && wait <= maxWait) {
        tries429++;
        await sleep(wait * 1000 + Math.random() * 250, opts.signal);
        if (opts.signal?.aborted) throw new DOMException("Aborted", "AbortError");
        continue;
      }
      const p = await toProblem(res);
      throw new ApiError(p, p.request_id ?? requestId, wait);
    }

    const p = await toProblem(res);
    throw new ApiError(p, p.request_id ?? requestId);
  }
}

export const api = {
  get: <T>(path: string, query?: RequestOptions["query"], signal?: AbortSignal) =>
    request<T>(path, { query, signal }).then((r) => r.data),
  post: <T>(path: string, body?: unknown, opts: Omit<RequestOptions, "method" | "body"> = {}) =>
    request<T>(path, { ...opts, method: "POST", body }),
  put: <T>(path: string, body?: unknown, opts: Omit<RequestOptions, "method" | "body"> = {}) =>
    request<T>(path, { ...opts, method: "PUT", body }),
  del: <T>(path: string, opts: Omit<RequestOptions, "method"> = {}) => request<T>(path, { ...opts, method: "DELETE" }),
};

// The token endpoint is the one unauthenticated write; it carries no Idempotency-Key.
setTokenFetcher(async (client_id, client_secret) => {
  const r = await request<TokenResponse>("/v1/auth/token", {
    method: "POST",
    auth: false,
    body: { grant_type: "client_credentials", client_id, client_secret },
  });
  return r.data;
});

/** Human text for any error, for toasts. */
export function errorText(e: unknown): { title: string; detail?: string; requestId?: string | null } {
  if (isApiError(e)) {
    const detail =
      e.status === 429 && e.retryAfter !== null
        ? `${e.problem.detail ? e.problem.detail + " " : ""}Try again in ${e.retryAfter} s.`
        : e.problem.detail;
    return { title: e.problem.title, detail, requestId: e.requestId };
  }
  return { title: "Something went wrong in the console", detail: e instanceof Error ? e.message : String(e) };
}
