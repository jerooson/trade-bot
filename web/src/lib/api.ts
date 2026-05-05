import type { Signal, Stats } from "./types";

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
