// Stock: SKU × warehouse grid of bin cards; adjust a count; add products and warehouses.
import { useQueryClient } from "@tanstack/react-query";
import { Loader2, Plus } from "lucide-react";
import { useState } from "react";
import { api, isApiError } from "../api/client";
import { useLive } from "../api/live";
import { qk, useStock } from "../api/resources";
import { isAdmin } from "../api/session";
import type { StockCell, StockRow } from "../api/types";
import { Dialog, Empty, ErrorState, Head, Loading, ProblemAlert, useToast } from "../components/ui";
import { count } from "../lib/format";

function BinCard({ cell, flash, onClick }: { cell: StockCell; flash: boolean; onClick?: () => void }) {
  const low = cell.on_hand > 0 && cell.available / cell.on_hand < 0.2;
  const pct = (n: number) => (cell.on_hand ? `${(n / cell.on_hand) * 100}%` : "0%");
  const body = (
    <>
      <div className="bar" aria-hidden="true">
        <i style={{ width: pct(cell.available) }} />
        <u style={{ width: pct(cell.reserved) }} />
      </div>
      <span className="mono">
        {cell.available} / {cell.on_hand}
      </span>{" "}
      <small>· {cell.reserved} reserved</small>
      {low && <span className="lowtag">LOW</span>}
    </>
  );
  const label = `${cell.available} available of ${cell.on_hand} on hand, ${cell.reserved} reserved${low ? ", low" : ""}`;
  const cls = `bin${low ? " low" : ""}${flash ? " flash" : ""}`;
  return onClick ? (
    <button className={cls} onClick={onClick} aria-label={`${label}. Correct this count`}>
      {body}
    </button>
  ) : (
    <div className={cls} aria-label={label}>
      {body}
    </div>
  );
}

function Adjust({ row, code, cell, onClose }: { row: StockRow; code: string; cell?: StockCell; onClose: () => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [mode, setMode] = useState<"set" | "delta">("set");
  const [qty, setQty] = useState(String(cell?.on_hand ?? 0));
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const n = Number(qty);
  const valid = qty.trim() !== "" && Number.isInteger(n) && (mode === "delta" ? n !== 0 : n >= 0);
  const result = mode === "set" ? n : (cell?.on_hand ?? 0) + n;

  async function save() {
    setBusy(true);
    setError(null);
    try {
      await api.post("/v1/inventory/adjustments", { sku: row.sku, warehouse_code: code, mode, quantity: n, reason: reason.trim() || null }, { ifMatch: cell?.version });
      toast.ok("Count saved");
      void qc.invalidateQueries({ queryKey: qk.stock });
      onClose();
    } catch (e) {
      setError(e);
      if (isApiError(e) && e.kind === "version-conflict") void qc.invalidateQueries({ queryKey: qk.stock });
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog
      title="Correct stock count"
      onClose={onClose}
      footer={
        <>
          <button className="btn ghost" onClick={onClose}>
            Close
          </button>
          <button className="btn primary" disabled={!valid || busy} onClick={() => void save()}>
            {busy && <Loader2 className="spin" />} Save count
          </button>
        </>
      }
    >
      <div>
        <span className="mono">{row.sku}</span> at <span className="mono">{code}</span>
        <div className="muted" style={{ fontSize: ".8125rem" }}>
          {cell ? `${cell.on_hand} on hand, ${cell.reserved} reserved for open orders (version ${cell.version})` : "Not stocked here yet"}
        </div>
      </div>
      <div className="seg" role="group" aria-label="How to change the count">
        <button aria-pressed={mode === "set"} onClick={() => { setMode("set"); setQty(String(cell?.on_hand ?? 0)); }}>
          Set count
        </button>
        <button aria-pressed={mode === "delta"} onClick={() => { setMode("delta"); setQty(""); }}>
          Add or remove
        </button>
      </div>
      <label className="field">
        <span>{mode === "set" ? "Units on hand" : "Units to add (negative to remove)"}</span>
        <input className="input mono" inputMode="numeric" value={qty} onChange={(e) => setQty(e.target.value)} aria-invalid={qty !== "" && !valid} data-autofocus />
        {valid && <small>On hand becomes {result}.</small>}
      </label>
      <label className="field">
        <span>Reason (optional)</span>
        <input className="input" value={reason} maxLength={200} onChange={(e) => setReason(e.target.value)} placeholder="Cycle count" />
      </label>
      <ProblemAlert error={error} />
    </Dialog>
  );
}

function AddDialog({ kind, onClose }: { kind: "product" | "warehouse"; onClose: () => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [a, setA] = useState("");
  const [name, setName] = useState("");
  const [c, setC] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const product = kind === "product";
  const price = Math.round(Number(c) * 100);
  const valid = /^[A-Za-z0-9._-]{1,32}$/.test(a) && name.trim() !== "" && (!product || (c !== "" && Number.isFinite(price) && price >= 0));

  async function save() {
    setBusy(true);
    setError(null);
    try {
      if (product) await api.post("/v1/products", { sku: a, name: name.trim(), price_cents: price });
      else await api.post("/v1/warehouses", { code: a, name: name.trim(), location: c.trim() || null });
      toast.ok(product ? "Product added" : "Warehouse added");
      void qc.invalidateQueries({ queryKey: qk.stock });
      onClose();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog
      title={product ? "Add product" : "Add warehouse"}
      onClose={onClose}
      footer={
        <>
          <button className="btn ghost" onClick={onClose}>
            Close
          </button>
          <button className="btn primary" disabled={!valid || busy} onClick={() => void save()}>
            {busy && <Loader2 className="spin" />} {product ? "Add product" : "Add warehouse"}
          </button>
        </>
      }
    >
      <label className="field">
        <span>{product ? "SKU" : "Code"}</span>
        <input className="input mono" value={a} onChange={(e) => setA(e.target.value.toUpperCase())} maxLength={32} placeholder={product ? "TEE-CLASSIC-XL" : "PNQ-3"} data-autofocus />
        <small>Letters, digits, dots and dashes.</small>
      </label>
      <label className="field">
        <span>Name</span>
        <input className="input" value={name} onChange={(e) => setName(e.target.value)} maxLength={200} />
      </label>
      <label className="field">
        <span>{product ? "Price (USD)" : "Location (optional)"}</span>
        <input className="input" value={c} onChange={(e) => setC(e.target.value)} inputMode={product ? "decimal" : "text"} maxLength={200} placeholder={product ? "18.00" : "Pune, IN"} />
      </label>
      <ProblemAlert error={error} />
    </Dialog>
  );
}

export function Stock() {
  const stock = useStock();
  const { stockFlashes } = useLive();
  const admin = isAdmin();
  const [editing, setEditing] = useState<{ row: StockRow; code: string } | null>(null);
  const [adding, setAdding] = useState<"product" | "warehouse" | null>(null);
  const g = stock.data;
  const now = Date.now();

  return (
    <>
      <Head title="Stock" sub={admin ? "Available is on hand minus reserved. Select a cell to correct a count." : "Available is on hand minus reserved."}>
        {admin && (
          <>
            <button className="btn" onClick={() => setAdding("warehouse")}>
              <Plus /> Add warehouse
            </button>
            <button className="btn primary" onClick={() => setAdding("product")}>
              <Plus /> Add product
            </button>
          </>
        )}
      </Head>
      <div className="card">
        {stock.isLoading ? (
          <Loading label="Loading stock" />
        ) : stock.isError || !g ? (
          <ErrorState error={stock.error} retry={() => void stock.refetch()} />
        ) : g.rows.length === 0 ? (
          <Empty title="No products yet">{admin ? "Add a warehouse and a product, then set a count." : "An admin needs to add products first."}</Empty>
        ) : (
          <table className="stack">
            <thead>
              <tr>
                <th>SKU</th>
                {g.warehouses.map((w) => (
                  <th key={w.code}>
                    {w.code} · {w.name}
                  </th>
                ))}
                <th className="r">Available</th>
              </tr>
            </thead>
            <tbody>
              {g.rows.map((row) => (
                <tr key={row.sku}>
                  <td data-label="SKU">
                    <span className="mono">{row.sku}</span>
                    <br />
                    <small className="muted">{row.name}</small>
                  </td>
                  {g.warehouses.map((w) => {
                    const cell = row.cells.find((c) => c.warehouse_code === w.code);
                    const flash = now - (stockFlashes[`${row.sku}|${w.code}`] ?? 0) < 2000;
                    const open = admin ? () => setEditing({ row, code: w.code }) : undefined;
                    return (
                      <td key={w.code} data-label={w.code}>
                        {cell ? (
                          <BinCard cell={cell} flash={flash} onClick={open} />
                        ) : admin ? (
                          <button className="bin empty" onClick={open}>
                            Not stocked here
                          </button>
                        ) : (
                          <span className="muted">Not stocked here</span>
                        )}
                      </td>
                    );
                  })}
                  <td data-label="Available" className="r fig" style={{ fontSize: "1.25rem" }}>
                    {count(row.total_available)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
      {editing && <Adjust row={editing.row} code={editing.code} cell={editing.row.cells.find((c) => c.warehouse_code === editing.code)} onClose={() => setEditing(null)} />}
      {adding && <AddDialog kind={adding} onClose={() => setAdding(null)} />}
    </>
  );
}
