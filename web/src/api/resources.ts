// Query keys, fetchers and hooks per resource. Response shapes that docs/api.md leaves open are
// normalised here, in one place (see the assumptions listed in the README section of the report).

import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { api } from "./client";
import { getSession } from "./session";
import type {
  EventEnvelope,
  Order,
  OrderStats,
  OrderStatus,
  OrderSummary,
  Page,
  Product,
  ReceivedHook,
  SimConfig,
  SimPayments,
  StockCell,
  StockGrid,
  StockRow,
  SystemHealth,
  SystemInfo,
  Warehouse,
  WebhookDelivery,
  WebhookSubscription,
} from "./types";
import { ORDER_STATUSES } from "./types";

export const qk = {
  info: ["system", "info"] as const,
  health: ["system", "health"] as const,
  ordersAll: ["orders"] as const,
  orders: (f: OrderFilters) => ["orders", "list", f] as const,
  inflight: ["orders", "inflight"] as const,
  stats: ["orders", "stats"] as const,
  order: (id: string) => ["order", id] as const,
  orderEvents: (id: string) => ["events", "aggregate", id] as const,
  recentEvents: ["events", "recent"] as const,
  warehouses: ["warehouses"] as const,
  products: ["products"] as const,
  stock: ["stock"] as const,
  subs: ["webhooks", "subscriptions"] as const,
  deliveries: (status: string) => ["webhooks", "deliveries", status] as const,
  simConfig: ["sim", "config"] as const,
  simPayments: ["sim", "payments"] as const,
  receiver: (inbox: string) => ["sim", "hooks", inbox] as const,
};

type Obj = Record<string, unknown>;
const isObj = (v: unknown): v is Obj => !!v && typeof v === "object" && !Array.isArray(v);
const num = (v: unknown, d = 0): number => (typeof v === "number" && Number.isFinite(v) ? v : d);
const numOrNull = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);

/** Cursor pages are `{ items, next_cursor }`; a bare array is accepted as a single page. */
export function toPage<T>(data: unknown): Page<T> {
  if (Array.isArray(data)) return { items: data as T[], next_cursor: null };
  if (isObj(data)) {
    const items = (Array.isArray(data.items) ? data.items : Array.isArray(data.data) ? data.data : []) as T[];
    const next = typeof data.next_cursor === "string" && data.next_cursor ? data.next_cursor : null;
    return { items, next_cursor: next };
  }
  return { items: [], next_cursor: null };
}

// ---------------------------------------------------------------- system
export const fetchInfo = () => api.get<SystemInfo>("/v1/system/info");
export function useSystemInfo() {
  return useQuery({ queryKey: qk.info, queryFn: fetchInfo, staleTime: 60_000, retry: 1 });
}

export function useHealth() {
  return useQuery({
    queryKey: qk.health,
    queryFn: async () => {
      const t0 = performance.now();
      const h = await api.get<SystemHealth>("/v1/system/health");
      return { ...h, nodes: Array.isArray(h.nodes) ? h.nodes : [], rtt: Math.round(performance.now() - t0) };
    },
    refetchInterval: 5_000,
    refetchIntervalInBackground: false,
    retry: 0,
  });
}

// ---------------------------------------------------------------- orders
export interface OrderFilters {
  q: string;
  status: OrderStatus[];
}

export function useOrders(f: OrderFilters) {
  return useInfiniteQuery({
    queryKey: qk.orders(f),
    initialPageParam: null as string | null,
    queryFn: async ({ pageParam, signal }) =>
      toPage<OrderSummary>(
        await api.get("/v1/orders", { limit: 50, cursor: pageParam ?? undefined, q: f.q.trim() || undefined, status: f.status }, signal),
      ),
    getNextPageParam: (last) => last.next_cursor,
    placeholderData: (prev) => prev,
  });
}

export async function fetchOrderPage(query: Record<string, string | number | string[] | undefined>, signal?: AbortSignal) {
  return toPage<OrderSummary>(await api.get("/v1/orders", query, signal));
}

export function useOrder(id: string | null) {
  return useQuery({
    queryKey: qk.order(id ?? ""),
    queryFn: ({ signal }) => api.get<Order>(`/v1/orders/${encodeURIComponent(id ?? "")}`, undefined, signal),
    enabled: !!id,
  });
}

export function useOrderEvents(id: string | null) {
  return useQuery({
    queryKey: qk.orderEvents(id ?? ""),
    queryFn: async ({ signal }) => {
      const p = toPage<EventEnvelope>(await api.get("/v1/events", { aggregate_id: id ?? "", limit: 100 }, signal));
      // Feed is newest first; the drawer reads oldest first.
      return [...p.items].sort((a, b) => a.occurred_at.localeCompare(b.occurred_at));
    },
    enabled: !!id,
  });
}

function toCounts(v: unknown): Partial<Record<OrderStatus, number>> {
  const out: Partial<Record<OrderStatus, number>> = {};
  if (isObj(v)) for (const s of ORDER_STATUSES) if (typeof v[s] === "number") out[s] = v[s] as number;
  return out;
}

/** GET /v1/orders/stats — shape not fixed by docs/api.md; see the normaliser notes. */
export function normaliseStats(raw: unknown): OrderStats {
  const r = isObj(raw) ? raw : {};
  const counts = toCounts(r.counts ?? r.by_status ?? r.status_counts);
  const pmRaw = (r.per_minute ?? r.orders_per_minute ?? r.minutes) as unknown;
  const per_minute = (Array.isArray(pmRaw) ? pmRaw : []).map((e: unknown) => {
    const o = isObj(e) ? e : {};
    const c = isObj(o.counts) ? toCounts(o.counts) : isObj(o.by_status) ? toCounts(o.by_status) : toCounts(o);
    const sum = Object.values(c).reduce((a, b) => a + (b ?? 0), 0);
    const total = num(o.total ?? o.count, sum);
    return { minute: String(o.minute ?? o.at ?? o.ts ?? ""), counts: c, total };
  });
  const sd = isObj(r.saga_duration_ms) ? r.saga_duration_ms : isObj(r.saga_ms) ? r.saga_ms : null;
  const p50 =
    numOrNull(r.saga_p50_ms) ?? (sd ? numOrNull(sd.p50) : null) ?? (numOrNull(r.saga_p50_seconds) !== null ? num(r.saga_p50_seconds) * 1000 : null);
  const p95 =
    numOrNull(r.saga_p95_ms) ?? (sd ? numOrNull(sd.p95) : null) ?? (numOrNull(r.saga_p95_seconds) !== null ? num(r.saga_p95_seconds) * 1000 : null);
  return { counts, per_minute, saga_p50_ms: p50, saga_p95_ms: p95 };
}

export function useStats() {
  return useQuery({
    queryKey: qk.stats,
    queryFn: async ({ signal }) => normaliseStats(await api.get("/v1/orders/stats", undefined, signal)),
    refetchInterval: 30_000,
  });
}

export function useRecentEvents() {
  return useQuery({
    queryKey: qk.recentEvents,
    queryFn: async ({ signal }) => toPage<EventEnvelope>(await api.get("/v1/events", { limit: 30 }, signal)).items,
    staleTime: Infinity,
  });
}

// ---------------------------------------------------------------- inventory
async function allPages<T>(path: string, signal?: AbortSignal, maxPages = 10): Promise<T[]> {
  const out: T[] = [];
  let cursor: string | null = null;
  for (let i = 0; i < maxPages; i++) {
    const p: Page<T> = toPage<T>(await api.get(path, { limit: 100, cursor: cursor ?? undefined }, signal));
    out.push(...p.items);
    if (!p.next_cursor) break;
    cursor = p.next_cursor;
  }
  return out;
}

export function useWarehouses() {
  return useQuery({ queryKey: qk.warehouses, queryFn: ({ signal }) => allPages<Warehouse>("/v1/warehouses", signal) });
}

export function useProducts() {
  return useQuery({ queryKey: qk.products, queryFn: ({ signal }) => allPages<Product>("/v1/products", signal) });
}

function toCell(o: Obj, fallbackCode = ""): StockCell {
  const on_hand = num(o.on_hand);
  const reserved = num(o.reserved);
  return {
    warehouse_code: String(o.warehouse_code ?? o.code ?? fallbackCode),
    on_hand,
    reserved,
    available: num(o.available, on_hand - reserved),
    version: num(o.version),
  };
}

/** GET /v1/inventory/stock — accepts rows with nested cells, or a flat SKU × warehouse list. */
export function normaliseStock(raw: unknown): StockGrid {
  const r = isObj(raw) ? raw : {};
  const warehouses: Warehouse[] = (Array.isArray(r.warehouses) ? r.warehouses : [])
    .filter(isObj)
    .map((w) => ({ code: String(w.code ?? w.warehouse_code ?? ""), name: String(w.name ?? w.warehouse_name ?? w.code ?? ""), location: (w.location as string) ?? null }));
  const bySku = new Map<string, StockRow>();
  const src = (Array.isArray(r.rows) ? r.rows : Array.isArray(r.items) ? r.items : Array.isArray(r.stock) ? r.stock : []) as unknown[];
  for (const e of src) {
    if (!isObj(e)) continue;
    const sku = String(e.sku ?? "");
    if (!sku) continue;
    let row = bySku.get(sku);
    if (!row) {
      row = { sku, name: (e.name ?? e.product_name ?? null) as string | null, cells: [], total_available: 0, total_on_hand: 0, total_reserved: 0 };
      bySku.set(sku, row);
    }
    const nested = Array.isArray(e.cells) ? e.cells : Array.isArray(e.warehouses) ? e.warehouses : Array.isArray(e.levels) ? e.levels : null;
    if (nested) for (const c of nested) if (isObj(c)) row.cells.push(toCell(c));
    if (!nested && ("on_hand" in e || "warehouse_code" in e)) row.cells.push(toCell(e));
  }
  const rows = [...bySku.values()].map((row) => {
    const t = row.cells.reduce((a, c) => ({ on_hand: a.on_hand + c.on_hand, reserved: a.reserved + c.reserved, available: a.available + c.available }), { on_hand: 0, reserved: 0, available: 0 });
    return { ...row, total_available: t.available, total_on_hand: t.on_hand, total_reserved: t.reserved };
  });
  const totals = rows.reduce(
    (a, x) => ({ on_hand: a.on_hand + x.total_on_hand, reserved: a.reserved + x.total_reserved, available: a.available + x.total_available }),
    { on_hand: 0, reserved: 0, available: 0 },
  );
  const oversold = numOrNull(r.oversold_rows) ?? rows.reduce((a, x) => a + x.cells.filter((c) => c.reserved > c.on_hand).length, 0);
  return { warehouses, rows, totals, oversold_rows: oversold };
}

export const fetchStock = async (signal?: AbortSignal) => normaliseStock(await api.get("/v1/inventory/stock", undefined, signal));
export function useStock() {
  return useQuery({ queryKey: qk.stock, queryFn: ({ signal }) => fetchStock(signal) });
}

// ---------------------------------------------------------------- webhooks
export function useSubscriptions() {
  return useQuery({
    queryKey: qk.subs,
    queryFn: async ({ signal }) => toPage<WebhookSubscription>(await api.get("/v1/webhooks/subscriptions", { limit: 100 }, signal)).items,
  });
}

export function useDeliveries(status: string) {
  return useInfiniteQuery({
    queryKey: qk.deliveries(status),
    initialPageParam: null as string | null,
    queryFn: async ({ pageParam, signal }) =>
      toPage<WebhookDelivery>(await api.get("/v1/webhooks/deliveries", { limit: 50, cursor: pageParam ?? undefined, status: status || undefined }, signal)),
    getNextPageParam: (last) => last.next_cursor,
    placeholderData: (prev) => prev,
  });
}

// ---------------------------------------------------------------- partner simulator (admin)
export function useSimConfig(enabled = true) {
  return useQuery({
    queryKey: qk.simConfig,
    queryFn: ({ signal }) => api.get<SimConfig>("/v1/sim/config", undefined, signal),
    enabled: enabled && !!getSession()?.scopes.includes("admin"),
    refetchInterval: 15_000,
    retry: 0,
  });
}

export function useSimPayments(enabled = true) {
  return useQuery({
    queryKey: qk.simPayments,
    queryFn: ({ signal }) => api.get<SimPayments>("/v1/sim/payments", undefined, signal),
    enabled: enabled && !!getSession()?.scopes.includes("admin"),
    retry: 0,
  });
}

/** GET /v1/sim/hooks/{inbox} — accepts `{ items }`, `{ deliveries }`, `{ received }` or a bare array. */
export function normaliseReceived(raw: unknown): ReceivedHook[] {
  const list = Array.isArray(raw)
    ? raw
    : isObj(raw)
      ? ((raw.items ?? raw.deliveries ?? raw.received ?? raw.requests ?? []) as unknown[])
      : [];
  return (Array.isArray(list) ? list : []).filter(isObj).map((o) => ({
    id: typeof o.id === "string" ? o.id : undefined,
    received_at: String(o.received_at ?? o.at ?? ""),
    headers: isObj(o.headers) ? (Object.fromEntries(Object.entries(o.headers).map(([k, v]) => [k.toLowerCase(), String(v)])) as Record<string, string>) : {},
    body: o.body ?? o.payload ?? null,
    signature_valid: typeof o.signature_valid === "boolean" ? o.signature_valid : null,
  }));
}

export function useReceiver(inbox: string, enabled = true) {
  return useQuery({
    queryKey: qk.receiver(inbox),
    queryFn: async ({ signal }) => normaliseReceived(await api.get(`/v1/sim/hooks/${encodeURIComponent(inbox)}`, undefined, signal)),
    enabled: enabled && !!getSession()?.scopes.includes("admin"),
    refetchInterval: 5_000,
    retry: 0,
  });
}
