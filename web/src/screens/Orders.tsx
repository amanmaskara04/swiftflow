// Orders: filterable list with compact rails, an order drawer with the full rail,
// and a Place order dialog that shows its idempotency key.
import { useQueryClient } from "@tanstack/react-query";
import { Loader2, Minus, Plus, RefreshCw, Search, Trash2, X } from "lucide-react";
import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { api } from "../api/client";
import { qk, useOrder, useOrderEvents, useOrders, useStock } from "../api/resources";
import { isAdmin } from "../api/session";
import { ORDER_STATUSES, type Order, type OrderStatus } from "../api/types";
import { SagaRail } from "../components/SagaRail";
import { Copy, Dialog, Drawer, Empty, ErrorState, Head, Loading, ProblemAlert, StatusBadge, useToast } from "../components/ui";
import { age, clock, json, money, shortId } from "../lib/format";
import { newKey } from "../lib/idempotency";

function OrderDrawer({ id, onClose }: { id: string; onClose: () => void }) {
  const order = useOrder(id);
  const events = useOrderEvents(id);
  const qc = useQueryClient();
  const toast = useToast();
  const [busy, setBusy] = useState<"cancel" | "ship" | null>(null);
  const [confirming, setConfirming] = useState(false);
  const o = order.data;

  async function act(kind: "cancel" | "ship") {
    setBusy(kind);
    try {
      await api.post(`/v1/orders/${id}/${kind}`);
      toast.ok(kind === "cancel" ? "Order cancelled" : "Order marked shipped");
      setConfirming(false);
      void qc.invalidateQueries({ queryKey: qk.order(id) });
      void qc.invalidateQueries({ queryKey: qk.ordersAll });
    } catch (e) {
      toast.fail(e);
    } finally {
      setBusy(null);
    }
  }

  return (
    <Drawer label="Order detail" onClose={onClose}>
      <header>
        <h2>
          Order <span className="mono" style={{ fontSize: "1rem", textTransform: "none" }}>{shortId(id)}</span>
          <Copy text={id} label="Copy order ID" />
        </h2>
        {o && <StatusBadge status={o.status} />}
        <button className="x" aria-label="Close" onClick={onClose}>
          <X size={18} />
        </button>
      </header>
      {order.isLoading ? (
        <Loading label="Loading order" />
      ) : order.isError || !o ? (
        <ErrorState error={order.error} retry={() => void order.refetch()} />
      ) : (
        <>
          <section>
            <div className="cap" style={{ marginBottom: 14 }}>
              Journey
            </div>
            <SagaRail order={o} size="full" />
            <p className="muted" style={{ margin: "12px 0 0", fontSize: ".8125rem" }}>
              {o.customer_ref ? `Customer ref ${o.customer_ref} · ` : ""}
              {money(o.total_cents)}
              {o.payment_status && o.payment_status !== "NONE" ? ` · payment ${o.payment_status.toLowerCase().replace("_", " ")}` : ""}
            </p>
          </section>
          <section>
            <div className="cap" style={{ marginBottom: 8 }}>
              Items
            </div>
            <table>
              <tbody>
                {o.items.map((i) => (
                  <tr key={i.sku}>
                    <td className="mono">{i.sku}</td>
                    <td>× {i.qty}</td>
                    <td className="mono">{i.warehouse_code ?? "not reserved"}</td>
                    <td className="mono r">{i.unit_price_cents != null ? money(i.unit_price_cents * i.qty) : "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>
          <section>
            <div className="cap" style={{ marginBottom: 8 }}>
              Events
            </div>
            {events.isLoading ? (
              <div className="skel" style={{ width: "60%" }} />
            ) : (events.data ?? []).length === 0 ? (
              <span className="muted">No events recorded for this order yet.</span>
            ) : (
              (events.data ?? []).map((e) => (
                <div className="evrow" key={e.event_id}>
                  <time className="mono">{clock(e.occurred_at)}</time>
                  <details>
                    <summary>
                      <b>{e.event_type}</b>
                      {typeof (e.payload as Record<string, unknown>).reason === "string" && (
                        <span className="mono">{String((e.payload as Record<string, unknown>).reason)}</span>
                      )}
                    </summary>
                    <pre>{json(e)}</pre>
                  </details>
                </div>
              ))
            )}
          </section>
          <section style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
            {confirming ? (
              <>
                <span>Cancel this order and release its stock?</span>
                <button className="btn danger solid" disabled={busy !== null} onClick={() => void act("cancel")} data-autofocus>
                  {busy === "cancel" && <Loader2 className="spin" />} Cancel order
                </button>
                <button className="btn ghost" onClick={() => setConfirming(false)}>
                  Keep order
                </button>
              </>
            ) : (
              <>
                <button className="btn danger" disabled={!["PENDING", "RESERVED", "CONFIRMED"].includes(o.status)} onClick={() => setConfirming(true)}>
                  Cancel order
                </button>
                {isAdmin() && (
                  <button className="btn" disabled={o.status !== "CONFIRMED" || busy !== null} onClick={() => void act("ship")}>
                    {busy === "ship" && <Loader2 className="spin" />} Mark shipped
                  </button>
                )}
              </>
            )}
          </section>
        </>
      )}
    </Drawer>
  );
}

interface Line {
  sku: string;
  qty: number;
}

function PlaceOrder({ onClose, onPlaced }: { onClose: () => void; onPlaced: (o: Order) => void }) {
  const stock = useStock();
  const toast = useToast();
  const [lines, setLines] = useState<Line[]>([{ sku: "", qty: 1 }]);
  const [ref, setRef] = useState("");
  const [key, setKey] = useState(() => newKey("order"));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const rows = stock.data?.rows ?? [];
  const available = (sku: string) => rows.find((r) => r.sku === sku)?.total_available;
  const valid = lines.every((l) => l.sku && l.qty >= 1) && new Set(lines.map((l) => l.sku)).size === lines.length;

  async function submit() {
    setBusy(true);
    setError(null);
    try {
      const r = await api.post<Order>("/v1/orders", { customer_ref: ref.trim() || null, items: lines }, { idempotencyKey: key });
      toast.ok(r.replayed ? "Same key, same order" : "Order placed", r.replayed ? "The stored response was replayed; no new order was created." : undefined);
      onPlaced(r.data);
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog
      title="Place order"
      onClose={onClose}
      wide
      footer={
        <>
          <button className="btn ghost" onClick={onClose}>
            Close
          </button>
          <button className="btn primary" disabled={!valid || busy} onClick={() => void submit()}>
            {busy && <Loader2 className="spin" />} Place order
          </button>
        </>
      }
    >
      {lines.map((l, i) => (
        <div key={i} style={{ display: "grid", gridTemplateColumns: "minmax(0,1fr) auto auto", gap: 8, alignItems: "end" }}>
          <label className="field">
            <span>{i === 0 ? "Product" : <span className="sr-only">Product</span>}</span>
            <select className="select" value={l.sku} onChange={(e) => setLines((cur) => cur.map((x, j) => (j === i ? { ...x, sku: e.target.value } : x)))} data-autofocus={i === 0 ? "" : undefined}>
              <option value="">Choose a product</option>
              {rows.map((r) => (
                <option key={r.sku} value={r.sku} disabled={lines.some((x, j) => j !== i && x.sku === r.sku)}>
                  {r.sku} — {r.name} ({r.total_available} available)
                </option>
              ))}
            </select>
          </label>
          <div className="stepper" role="group" aria-label="Quantity">
            <button type="button" aria-label="Decrease quantity" disabled={l.qty <= 1} onClick={() => setLines((cur) => cur.map((x, j) => (j === i ? { ...x, qty: x.qty - 1 } : x)))}>
              <Minus />
            </button>
            <input
              aria-label="Quantity"
              inputMode="numeric"
              value={l.qty}
              onChange={(e) => {
                const n = Math.max(1, Math.min(1000, Number(e.target.value.replace(/\D/g, "")) || 1));
                setLines((cur) => cur.map((x, j) => (j === i ? { ...x, qty: n } : x)));
              }}
            />
            <button type="button" aria-label="Increase quantity" onClick={() => setLines((cur) => cur.map((x, j) => (j === i ? { ...x, qty: Math.min(1000, x.qty + 1) } : x)))}>
              <Plus />
            </button>
          </div>
          <button type="button" className="btn ghost" aria-label="Remove line" disabled={lines.length === 1} onClick={() => setLines((cur) => cur.filter((_, j) => j !== i))} style={{ height: 38 }}>
            <Trash2 />
          </button>
          {l.sku && available(l.sku) !== undefined && l.qty > (available(l.sku) ?? 0) && (
            <small className="muted" style={{ gridColumn: "1 / -1" }}>
              Only {available(l.sku)} available, so this order will be rejected. You can place it anyway to see that happen.
            </small>
          )}
        </div>
      ))}
      <div>
        <button type="button" className="btn sm ghost" disabled={lines.length >= 10} onClick={() => setLines((cur) => [...cur, { sku: "", qty: 1 }])}>
          <Plus /> Add line
        </button>
      </div>
      <label className="field">
        <span>Customer ref (optional)</span>
        <input className="input" value={ref} maxLength={200} onChange={(e) => setRef(e.target.value)} placeholder="shopify-#10412" />
      </label>
      <div className="field">
        <span>Idempotency key</span>
        <div className="keybox">
          <span>{key}</span>
          <button type="button" className="btn sm ghost" onClick={() => setKey(newKey("order"))}>
            <RefreshCw /> New key
          </button>
        </div>
        <small>Placing again with the same key and the same items returns the first order instead of creating another.</small>
      </div>
      <ProblemAlert error={error} />
    </Dialog>
  );
}

export function Orders() {
  const { id } = useParams();
  const nav = useNavigate();
  const [q, setQ] = useState("");
  const [debounced, setDebounced] = useState("");
  const [status, setStatus] = useState<OrderStatus[]>([]);
  const [placing, setPlacing] = useState(false);
  useEffect(() => {
    const t = setTimeout(() => setDebounced(q), 250);
    return () => clearTimeout(t);
  }, [q]);
  const orders = useOrders({ q: debounced, status });
  const items = orders.data?.pages.flatMap((p) => p.items) ?? [];
  const now = Date.now();

  return (
    <>
      <Head title="Orders" sub="Open an order to see every step it took.">
        <button className="btn primary" onClick={() => setPlacing(true)}>
          <Plus /> Place order
        </button>
      </Head>
      <div className="filters">
        <div className="search">
          <Search />
          <input className="input" placeholder="Search order ID or customer ref" aria-label="Search orders" value={q} onChange={(e) => setQ(e.target.value)} maxLength={100} />
        </div>
        <button className="chip" aria-pressed={status.length === 0} onClick={() => setStatus([])}>
          All
        </button>
        {ORDER_STATUSES.map((s) => (
          <button key={s} className="chip" aria-pressed={status.includes(s)} onClick={() => setStatus((cur) => (cur.includes(s) ? cur.filter((x) => x !== s) : [...cur, s]))}>
            {s.charAt(0) + s.slice(1).toLowerCase()}
          </button>
        ))}
      </div>
      <div className="card">
        {orders.isLoading ? (
          <Loading label="Loading orders" />
        ) : orders.isError ? (
          <ErrorState error={orders.error} retry={() => void orders.refetch()} />
        ) : items.length === 0 ? (
          <Empty title={debounced || status.length ? "No orders match these filters" : "No orders yet"}>
            {debounced || status.length ? "Clear the search or pick a different status." : "Place the first one to watch it move along the line."}
          </Empty>
        ) : (
          <>
            <table className="rows stack">
              <thead>
                <tr>
                  <th>Order</th>
                  <th>Customer ref</th>
                  <th>Journey</th>
                  <th>Status</th>
                  <th className="r">Items</th>
                  <th className="r">Total</th>
                  <th className="r">Age</th>
                </tr>
              </thead>
              <tbody>
                {items.map((o) => (
                  <tr key={o.id} className={o.id === id ? "sel" : ""} onClick={() => nav(`/orders/${o.id}`)}>
                    <td data-label="Order">
                      <button className="rowbtn" onClick={(e) => { e.stopPropagation(); nav(`/orders/${o.id}`); }} aria-label={`Open order ${shortId(o.id)}`}>
                        {shortId(o.id)}
                      </button>
                    </td>
                    <td data-label="Customer ref">{o.customer_ref ?? <span className="muted">—</span>}</td>
                    <td data-label="Journey" className="rail">
                      <SagaRail order={o} />
                    </td>
                    <td data-label="Status">
                      <StatusBadge status={o.status} />
                    </td>
                    <td data-label="Items" className="mono r">{o.item_count ?? "—"}</td>
                    <td data-label="Total" className="mono r">{money(o.total_cents)}</td>
                    <td data-label="Age" className="mono r">{age(o.created_at, now)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {orders.hasNextPage && (
              <div className="tablefoot">
                <button className="btn sm" disabled={orders.isFetchingNextPage} onClick={() => void orders.fetchNextPage()}>
                  {orders.isFetchingNextPage && <Loader2 className="spin" />} Load more
                </button>
              </div>
            )}
          </>
        )}
      </div>
      {id && <OrderDrawer id={id} onClose={() => nav("/orders")} />}
      {placing && (
        <PlaceOrder
          onClose={() => setPlacing(false)}
          onPlaced={(o) => {
            setPlacing(false);
            nav(`/orders/${o.id}`);
          }}
        />
      )}
    </>
  );
}
