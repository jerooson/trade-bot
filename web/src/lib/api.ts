import type { OpenPosition, Signal, Stats, TradeAction, TradePlan } from "./types";

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
