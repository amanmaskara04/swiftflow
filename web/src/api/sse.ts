// Live event stream: GET /v1/events/stream read with fetch + ReadableStream and an
// Authorization header (never a token in the URL), with reconnect and exponential backoff.

import { getSession, refresh, signOut } from "./session";
import type { EventEnvelope } from "./types";

export type StreamStatus = "connecting" | "live" | "reconnecting" | "offline";

export interface StreamHandlers {
  onEvent: (e: EventEnvelope) => void;
  onStatus: (s: StreamStatus) => void;
}

const IDLE_TIMEOUT_MS = 45_000; // server sends a keep-alive every 15 s
const MAX_BACKOFF_MS = 30_000;

export function openStream({ onEvent, onStatus }: StreamHandlers): () => void {
  let stopped = false;
  let attempt = 0;
  let ctrl: AbortController | null = null;
  let lastId: string | null = null;
  let wake: (() => void) | null = null;

  const wait = (ms: number) =>
    new Promise<void>((resolve) => {
      const t = setTimeout(resolve, ms);
      wake = () => {
        clearTimeout(t);
        resolve();
      };
    });

  // Reconnect at once when the tab comes back online.
  const onOnline = () => wake?.();
  window.addEventListener("online", onOnline);

  const dispatch = (type: string, data: string) => {
    if (!data) return;
    try {
      const env = JSON.parse(data) as EventEnvelope;
      if (!env.event_type && type) env.event_type = type;
      onEvent(env);
    } catch {
      /* a malformed message is skipped; the stream continues */
    }
  };

  async function readBody(body: ReadableStream<Uint8Array>, signal: AbortSignal) {
    const reader = body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    let evType = "";
    let data: string[] = [];
    let idle: ReturnType<typeof setTimeout> | undefined;
    const kick = () => {
      clearTimeout(idle);
      idle = setTimeout(() => ctrl?.abort(), IDLE_TIMEOUT_MS);
    };
    kick();
    try {
      while (!signal.aborted) {
        const { value, done } = await reader.read();
        if (done) break;
        kick();
        buf += decoder.decode(value, { stream: true });
        let nl: number;
        while ((nl = buf.search(/\r\n|\r|\n/)) >= 0) {
          const line = buf.slice(0, nl);
          buf = buf.slice(nl + (buf[nl] === "\r" && buf[nl + 1] === "\n" ? 2 : 1));
          if (line === "") {
            dispatch(evType, data.join("\n"));
            evType = "";
            data = [];
            continue;
          }
          if (line.startsWith(":")) continue; // keep-alive comment
          const i = line.indexOf(":");
          const field = i < 0 ? line : line.slice(0, i);
          let val = i < 0 ? "" : line.slice(i + 1);
          if (val.startsWith(" ")) val = val.slice(1);
          if (field === "event") evType = val;
          else if (field === "data") data.push(val);
          else if (field === "id") lastId = val;
        }
      }
    } finally {
      clearTimeout(idle);
      reader.cancel().catch(() => undefined);
    }
  }

  async function loop() {
    while (!stopped) {
      const s = getSession();
      if (!s) {
        onStatus("offline");
        return;
      }
      ctrl = new AbortController();
      onStatus(attempt === 0 ? "connecting" : "reconnecting");
      try {
        const headers: Record<string, string> = { Authorization: `Bearer ${s.token}`, Accept: "text/event-stream" };
        if (lastId) headers["Last-Event-ID"] = lastId;
        const res = await fetch("/v1/events/stream", { headers, signal: ctrl.signal, cache: "no-store" });
        if (res.status === 401) {
          if (await refresh()) continue;
          signOut("expired");
          return;
        }
        if (!res.ok || !res.body) throw new Error(`stream ${res.status}`);
        attempt = 0;
        onStatus("live");
        await readBody(res.body, ctrl.signal);
      } catch {
        /* fall through to backoff */
      }
      if (stopped) return;
      attempt++;
      const delay = Math.min(MAX_BACKOFF_MS, 1000 * 2 ** Math.min(attempt - 1, 6));
      onStatus("reconnecting");
      await wait(delay * (0.5 + Math.random() * 0.5));
    }
  }

  void loop();

  return () => {
    stopped = true;
    ctrl?.abort();
    wake?.();
    window.removeEventListener("online", onOnline);
  };
}
