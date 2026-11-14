// Types for the gateway API (docs/api.md) and the event envelope (docs/events.md).

export type OrderStatus = "PENDING" | "RESERVED" | "CONFIRMED" | "SHIPPED" | "REJECTED" | "CANCELLED";
export const ORDER_STATUSES: OrderStatus[] = ["PENDING", "RESERVED", "CONFIRMED", "SHIPPED", "REJECTED", "CANCELLED"];
export const IN_FLIGHT: OrderStatus[] = ["PENDING", "RESERVED"];
export const FINAL: OrderStatus[] = ["CONFIRMED", "SHIPPED", "REJECTED", "CANCELLED"];

export interface Problem {
  type: string;
  title: string;
  status: number;
  detail?: string;
  instance?: string;
  request_id?: string;
  errors?: Array<{ loc?: Array<string | number>; msg?: string; message?: string; field?: string; type?: string }>;
  [k: string]: unknown;
}

export interface Page<T> {
  items: T[];
  next_cursor: string | null;
}

export interface TokenResponse {
  access_token: string;
  token_type: string;
  expires_in: number;
  scope: string;
  tenant_id: string;
  tenant_name: string;
}

export interface DemoClient {
  client_id: string;
  client_secret: string;
  role: string;
  tenant: string;
}

export interface SystemInfo {
  environment: string;
  version: string;
  demo_clients: DemoClient[];
  webhook_test_receiver_url: string;
}

export interface HealthNode {
  id: string;
  kind: string;
  port: number;
  status: "up" | "down" | string;
  latency_ms?: number | null;
  error?: string | null;
  checks?: Record<string, string>;
}

export interface SystemHealth {
  status: string;
  checked_at: string;
  nodes: HealthNode[];
}

export interface OrderItemInput {
  sku: string;
  qty: number;
}

export interface OrderItem {
  sku: string;
  qty: number;
  warehouse_code?: string | null;
  unit_price_cents?: number | null;
}

export interface HistoryEntry {
  from_status?: OrderStatus | null;
  to_status: OrderStatus;
  at: string;
  reason: string | null;
  event_type: string | null;
}

export interface OrderSummary {
  id: string;
  status: OrderStatus;
  customer_ref?: string | null;
  total_cents: number | null;
  item_count?: number;
  failure_reason?: string | null;
  created_at: string;
  updated_at?: string;
}

export interface Order extends OrderSummary {
  items: OrderItem[];
  failure_reason?: string | null;
  payment_status?: string | null;
  payment_id?: string | null;
  history?: HistoryEntry[];
}

export interface OrderStats {
  counts: Partial<Record<OrderStatus, number>>;
  per_minute: Array<{ minute: string; counts: Partial<Record<OrderStatus, number>>; total: number }>;
  saga_p50_ms: number | null;
  saga_p95_ms: number | null;
}

export interface Warehouse {
  id?: string;
  code: string;
  name: string;
  location?: string | null;
  created_at?: string;
}

export interface Product {
  id?: string;
  sku: string;
  name: string;
  price_cents: number;
  created_at?: string;
}

export interface StockCell {
  warehouse_code: string;
  on_hand: number;
  reserved: number;
  available: number;
  version: number;
}

export interface StockRow {
  sku: string;
  name?: string | null;
  cells: StockCell[];
  total_available: number;
  total_on_hand: number;
  total_reserved: number;
}

export interface StockGrid {
  warehouses: Warehouse[];
  rows: StockRow[];
  totals: { on_hand: number; reserved: number; available: number };
  oversold_rows: number;
}

export interface Availability {
  sku: string;
  total_available: number;
  cached?: boolean;
  warehouses: Array<{ code: string; on_hand: number; reserved: number; available: number; version: number }>;
}

export interface AdjustmentInput {
  sku: string;
  warehouse_code: string;
  mode: "set" | "delta";
  quantity: number;
  reason: string;
}

export interface EventEnvelope<P = Record<string, unknown>> {
  event_id: string;
  event_type: string;
  schema_version: number;
  occurred_at: string;
  tenant_id: string;
  aggregate_id: string;
  producer: string;
  payload: P;
}

export interface WebhookSubscription {
  id: string;
  url: string;
  event_types: string[];
  active: boolean;
  created_at: string;
  signing_secret?: string;
}

export interface WebhookDelivery {
  id: string;
  subscription_id: string;
  event_id: string;
  event_type: string;
  status: "PENDING" | "DELIVERED" | "FAILED";
  attempts: number;
  next_attempt_at: string | null;
  last_status_code: number | null;
  last_error: string | null;
  created_at: string;
  delivered_at: string | null;
}

export interface SimConfig {
  failure_rate: number;
  latency_ms: number;
  outage: boolean;
}

export interface SimPayments {
  double_charges: number;
  charges?: unknown[];
  refunds?: unknown[];
  [k: string]: unknown;
}

export interface ReceivedHook {
  id?: string;
  received_at: string;
  headers: Record<string, string>;
  body: unknown;
  signature_valid: boolean | null;
}

export const EVENT_TYPES = [
  "OrderPlaced",
  "StockReserved",
  "StockReservationFailed",
  "OrderConfirmed",
  "OrderCancelled",
  "OrderShipped",
  "StockReleased",
  "StockCommitted",
  "StockLevelChanged",
] as const;

export const TOPIC_OF: Record<string, string> = {
  OrderPlaced: "orders.events",
  OrderConfirmed: "orders.events",
  OrderCancelled: "orders.events",
  OrderShipped: "orders.events",
  StockReserved: "inventory.events",
  StockReservationFailed: "inventory.events",
  StockReleased: "inventory.events",
  StockCommitted: "inventory.events",
  StockLevelChanged: "inventory.stock-changes",
};
