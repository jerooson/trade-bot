import type {
  DayTradePnl,
  DayTradePosition,
  ManualDayPlan,
  OpenPosition,
  PnlSummary,
  ProposedOrder,
  Signal,
  Stats,
  TradeAction,
  TradePlan,
  VirtualBook,
} from "./types";

export async function fetchSignals(params: {
  limit?: number;
  kind?: string;
  ticker?: string;
} = {}): Promise<{ count: number; signals: Signal[] }> {
  const qs = new URLSearchParams();
  if (params.limit) qs.set("limit", String(params.limit));
  if (params.kind) qs.set("kind", params.kind);
  if (params.ticker) qs.set("ticker", params.ticker);
  const r = await fetch(`/api/signals?${qs.toString()}`);
  if (!r.ok) throw new Error(`signals: ${r.status}`);
  return r.json();
}

export async function fetchStats(): Promise<Stats> {
  const r = await fetch("/api/stats");
  if (!r.ok) throw new Error(`stats: ${r.status}`);
  return r.json();
}

export interface StreamHandlers {
  onHello?: (data: { connected_at: string }) => void;
  onSignal?: (signal: Signal) => void;
  onHeartbeat?: (data: { ts: string }) => void;
  onError?: (err: Event) => void;
  onOpen?: () => void;
}

/** Open the SSE stream. Returns a cleanup function. */
export function openSignalStream(handlers: StreamHandlers): () => void {
  const es = new EventSource("/api/stream");

  es.addEventListener("open", () => handlers.onOpen?.());

  es.addEventListener("hello", (e) => {
    handlers.onHello?.(JSON.parse((e as MessageEvent).data));
  });

  es.addEventListener("signal", (e) => {
    handlers.onSignal?.(JSON.parse((e as MessageEvent).data));
  });

  es.addEventListener("heartbeat", (e) => {
    handlers.onHeartbeat?.(JSON.parse((e as MessageEvent).data));
  });

  es.addEventListener("error", (e) => handlers.onError?.(e));

  return () => es.close();
}

// -- Plans --------------------------------------------------------------------

export async function fetchPlans(params: {
  limit?: number;
  ticker?: string;
} = {}): Promise<{ count: number; plans: TradePlan[] }> {
  const qs = new URLSearchParams();
  if (params.limit) qs.set("limit", String(params.limit));
  if (params.ticker) qs.set("ticker", params.ticker);
  const r = await fetch(`/api/plans?${qs.toString()}`);
  if (!r.ok) throw new Error(`plans: ${r.status}`);
  return r.json();
}

export interface PlanStreamHandlers {
  onHello?: (data: { connected_at: string }) => void;
  onPlan?: (plan: TradePlan) => void;
  onHeartbeat?: (data: { ts: string }) => void;
  onError?: (err: Event) => void;
  onOpen?: () => void;
}

/** Open the plans SSE stream. Returns a cleanup function. */
export function openPlanStream(handlers: PlanStreamHandlers): () => void {
  const es = new EventSource("/api/plans/stream");
  es.addEventListener("open", () => handlers.onOpen?.());
  es.addEventListener("hello", (e) => {
    handlers.onHello?.(JSON.parse((e as MessageEvent).data));
  });
  es.addEventListener("plan", (e) => {
    handlers.onPlan?.(JSON.parse((e as MessageEvent).data));
  });
  es.addEventListener("heartbeat", (e) => {
    handlers.onHeartbeat?.(JSON.parse((e as MessageEvent).data));
  });
  es.addEventListener("error", (e) => handlers.onError?.(e));
  return () => es.close();
}

// -- Swings -------------------------------------------------------------------

export async function fetchSwings(params: {
  limit?: number;
  kind?: string;
  ticker?: string;
  actionable_only?: boolean;
} = {}): Promise<{ count: number; actions: TradeAction[]; open_positions: OpenPosition[] }> {
  const qs = new URLSearchParams();
  if (params.limit) qs.set("limit", String(params.limit));
  if (params.kind) qs.set("kind", params.kind);
  if (params.ticker) qs.set("ticker", params.ticker);
  if (params.actionable_only) qs.set("actionable_only", "true");
  const r = await fetch(`/api/swings?${qs.toString()}`);
  if (!r.ok) throw new Error(`swings: ${r.status}`);
  return r.json();
}

export interface SwingStreamHandlers {
  onHello?: (data: { connected_at: string }) => void;
  onSwing?: (action: TradeAction) => void;
  onHeartbeat?: (data: { ts: string }) => void;
  onError?: (err: Event) => void;
  onOpen?: () => void;
}

/** Open the swings SSE stream. Returns a cleanup function. */
export function openSwingStream(handlers: SwingStreamHandlers): () => void {
  const es = new EventSource("/api/swings/stream");
  es.addEventListener("open", () => handlers.onOpen?.());
  es.addEventListener("hello", (e) => {
    handlers.onHello?.(JSON.parse((e as MessageEvent).data));
  });
  es.addEventListener("swing", (e) => {
    handlers.onSwing?.(JSON.parse((e as MessageEvent).data));
  });
  es.addEventListener("heartbeat", (e) => {
    handlers.onHeartbeat?.(JSON.parse((e as MessageEvent).data));
  });
  es.addEventListener("error", (e) => handlers.onError?.(e));
  return () => es.close();
}

// -- Executor -----------------------------------------------------------------

export async function fetchExecutorBook(): Promise<VirtualBook> {
  const r = await fetch("/api/executor/book");
  if (!r.ok) throw new Error(`executor/book: ${r.status}`);
  return r.json();
}

export async function fetchExecutorOrders(params: {
  limit?: number;
  action?: string;
  ticker?: string;
} = {}): Promise<{ count: number; orders: ProposedOrder[] }> {
  const qs = new URLSearchParams();
  if (params.limit) qs.set("limit", String(params.limit));
  if (params.action) qs.set("action", params.action);
  if (params.ticker) qs.set("ticker", params.ticker);
  const r = await fetch(`/api/executor/orders?${qs.toString()}`);
  if (!r.ok) throw new Error(`executor/orders: ${r.status}`);
  return r.json();
}

export interface OrderStreamHandlers {
  onHello?: (data: { connected_at: string }) => void;
  onOrder?: (order: ProposedOrder) => void;
  onHeartbeat?: (data: { ts: string }) => void;
  onError?: (err: Event) => void;
  onOpen?: () => void;
}

export async function fetchPnl(): Promise<PnlSummary> {
  const r = await fetch("/api/pnl");
  if (!r.ok) throw new Error(`pnl: ${r.status}`);
  return r.json();
}

// -- Day trader ---------------------------------------------------------------

export interface DayTradeState {
  positions: DayTradePosition[];
  pnl: DayTradePnl | null;
  service_running: boolean;
  manual_plans: ManualDayPlan[];
}

export async function fetchDayTradeState(): Promise<DayTradeState> {
  const res = await fetch("/api/daytrader");
  if (!res.ok) throw new Error(`daytrader: ${res.status}`);
  return res.json();
}

export async function createManualDayPlan(input: {
  ticker: string;
  trigger_price: number;
  target_price: number | null;
  setup: string | null;
}): Promise<ManualDayPlan> {
  const res = await fetch("/api/daytrader/manual-plans", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.detail ?? `manual watch: ${res.status}`);
  }
  return res.json();
}

export async function cancelManualDayPlan(planId: string): Promise<void> {
  const res = await fetch(`/api/daytrader/manual-plans/${planId}`, {
    method: "DELETE",
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.detail ?? `cancel manual watch: ${res.status}`);
  }
}

/** Open the executor orders SSE stream. Returns a cleanup function. */
export function openOrderStream(handlers: OrderStreamHandlers): () => void {
  const es = new EventSource("/api/executor/orders/stream");
  es.addEventListener("open", () => handlers.onOpen?.());
  es.addEventListener("hello", (e) => {
    handlers.onHello?.(JSON.parse((e as MessageEvent).data));
  });
  es.addEventListener("order", (e) => {
    handlers.onOrder?.(JSON.parse((e as MessageEvent).data));
  });
  es.addEventListener("heartbeat", (e) => {
    handlers.onHeartbeat?.(JSON.parse((e as MessageEvent).data));
  });
  es.addEventListener("error", (e) => handlers.onError?.(e));
  return () => es.close();
}

// ---------------------------------------------------------------------------
// Chat agent
// ---------------------------------------------------------------------------

export async function fetchChatHistory(
  sessionId: string
): Promise<{ role: string; content: string }[]> {
  const r = await fetch(`/api/chat/history/${sessionId}`);
  if (!r.ok) return [];
  const d = await r.json();
  return d.messages ?? [];
}

export async function clearChatHistory(sessionId: string): Promise<void> {
  await fetch(`/api/chat/history/${sessionId}`, { method: "DELETE" });
}
