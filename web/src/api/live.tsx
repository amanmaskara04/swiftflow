// LiveProvider: owns the event stream, keeps the event tape, fans events out to subscribers
// (line board, system map, oversell test) and invalidates the matching TanStack Query caches.

import { useQueryClient, type InfiniteData, type QueryKey } from "@tanstack/react-query";
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { qk, useRecentEvents } from "./resources";
import { openStream, type StreamStatus } from "./sse";
import type { EventEnvelope, OrderSummary, Page } from "./types";
import { TOPIC_OF } from "./types";

const ORDER_EVENTS = new Set([
  "OrderPlaced",
  "StockReserved",
  "StockReservationFailed",
  "OrderConfirmed",
  "OrderCancelled",
  "OrderShipped",
  "StockReleased",
  "StockCommitted",
]);

export interface TapeItem {
  env: EventEnvelope;
  seenAt: number;
  live: boolean;
}

interface LiveCtx {
  status: StreamStatus;
  tape: TapeItem[];
  lastByTopic: Record<string, EventEnvelope | undefined>;
  subscribe: (fn: (e: EventEnvelope) => void) => () => void;
  /** Recent StockLevelChanged arrivals: key `${sku}|${warehouse_code}` → time seen. */
  stockFlashes: Record<string, number>;
}

const Ctx = createContext<LiveCtx | null>(null);
const TAPE_MAX = 200;

export function LiveProvider({ children }: { children: ReactNode }) {
  const qc = useQueryClient();
  const [status, setStatus] = useState<StreamStatus>("connecting");
  const [tape, setTape] = useState<TapeItem[]>([]);
  const [lastByTopic, setLastByTopic] = useState<Record<string, EventEnvelope | undefined>>({});
  const [stockFlashes, setStockFlashes] = useState<Record<string, number>>({});
  const subs = useRef(new Set<(e: EventEnvelope) => void>());
  const seen = useRef(new Set<string>());
  const pending = useRef<EventEnvelope[]>([]);
  const dirty = useRef(new Map<string, QueryKey>());
  const recent = useRecentEvents();

  // Seed the tape from the feed so it is not empty on first paint.
  useEffect(() => {
    if (!recent.data) return;
    setTape((cur) => {
      const have = new Set(cur.map((t) => t.env.event_id));
      const seed = recent.data.filter((e) => !have.has(e.event_id)).map((env) => ({ env, seenAt: 0, live: false }));
      seed.forEach((s) => seen.current.add(s.env.event_id));
      return [...cur, ...seed].slice(0, TAPE_MAX);
    });
  }, [recent.data]);

  const markDirty = useCallback((key: QueryKey) => {
    dirty.current.set(JSON.stringify(key), key);
  }, []);

  const isVisibleOrder = useCallback(
    (id: string) => {
      const lists = qc.getQueriesData<InfiniteData<Page<OrderSummary>>>({ queryKey: ["orders", "list"] });
      return lists.some(([, d]) => d?.pages.some((p) => p.items.some((o) => o.id === id)));
    },
    [qc],
  );

  const handle = useCallback(
    (env: EventEnvelope) => {
      if (env.event_id) {
        if (seen.current.has(env.event_id)) return; // at-least-once delivery: drop duplicates
        seen.current.add(env.event_id);
        if (seen.current.size > 5000) seen.current = new Set([...seen.current].slice(-2500));
      }
      pending.current.push(env);
      for (const fn of subs.current) fn(env);

      const t = env.event_type;
      const p = (env.payload ?? {}) as Record<string, unknown>;
      if (ORDER_EVENTS.has(t)) {
        const orderId = (typeof p.order_id === "string" && p.order_id) || env.aggregate_id;
        markDirty(qk.stats);
        markDirty(qk.inflight);
        if (orderId) {
          markDirty(qk.order(orderId));
          markDirty(qk.orderEvents(orderId));
          if (t === "OrderPlaced" || isVisibleOrder(orderId)) markDirty(["orders", "list"]);
        }
        if (t === "StockReserved" || t === "StockReleased" || t === "StockCommitted") markDirty(qk.stock);
      }
      if (t === "StockLevelChanged") {
        markDirty(qk.stock);
        const key = `${String(p.sku ?? "")}|${String(p.warehouse_code ?? "")}`;
        setStockFlashes((cur) => ({ ...cur, [key]: Date.now() }));
      }
      markDirty(["webhooks", "deliveries"]);
    },
    [isVisibleOrder, markDirty],
  );

  // Flush tape updates and cache invalidations in batches, so a burst of
  // 500 orders does not re-render the app per event.
  useEffect(() => {
    const t = setInterval(() => {
      if (pending.current.length) {
        const batch = pending.current;
        pending.current = [];
        const now = Date.now();
        setTape((cur) => [...batch.reverse().map((env) => ({ env, seenAt: now, live: true })), ...cur].slice(0, TAPE_MAX));
        setLastByTopic((cur) => {
          const next = { ...cur };
          for (const e of batch) {
            const topic = TOPIC_OF[e.event_type];
            if (topic && (!next[topic] || next[topic]!.occurred_at <= e.occurred_at)) next[topic] = e;
          }
          return next;
        });
      }
      if (dirty.current.size) {
        const keys = [...dirty.current.values()];
        dirty.current.clear();
        for (const k of keys) void qc.invalidateQueries({ queryKey: k });
      }
    }, 400);
    return () => clearInterval(t);
  }, [qc]);

  useEffect(() => openStream({ onEvent: handle, onStatus: setStatus }), [handle]);

  const subscribe = useCallback((fn: (e: EventEnvelope) => void) => {
    subs.current.add(fn);
    return () => {
      subs.current.delete(fn);
    };
  }, []);

  const value = useMemo(() => ({ status, tape, lastByTopic, subscribe, stockFlashes }), [status, tape, lastByTopic, subscribe, stockFlashes]);
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useLive(): LiveCtx {
  const c = useContext(Ctx);
  if (!c) throw new Error("useLive outside LiveProvider");
  return c;
}

export function useLiveEvents(fn: (e: EventEnvelope) => void) {
  const { subscribe } = useLive();
  const ref = useRef(fn);
  ref.current = fn;
  useEffect(() => subscribe((e) => ref.current(e)), [subscribe]);
}
