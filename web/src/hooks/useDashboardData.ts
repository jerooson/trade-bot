import { useEffect, useRef, useState } from "react";
import { fetchSignals, openSignalStream } from "../lib/api";
import type { Signal } from "../lib/types";

export type ConnectionState = "connecting" | "connected" | "disconnected";

interface DashboardData {
  signals: Signal[];
  conn: ConnectionState;
  lastEventAt: string | null;
  loading: boolean;
  error: string | null;
}

/**
 * One hook to rule them all: initial REST fetch + live SSE merge.
 *
 * - Fetches /api/signals on mount.
 * - Opens the SSE stream and prepends new signals as they arrive.
 * - Maintains a small connection state machine for the status indicator.
 * - Dedupes by Discord message_id when merging live signals into the list.
 *
 * Stats are derived client-side (see lib/derive.ts) so the date-range filter
 * in App can apply instantly to all aggregations.
 */
export function useDashboardData(): DashboardData {
  const [signals, setSignals] = useState<Signal[]>([]);
  const [conn, setConn] = useState<ConnectionState>("connecting");
  const [lastEventAt, setLastEventAt] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Keep a ref to message_ids seen so live merge dedupes against history.
  const seenIdsRef = useRef<Set<number>>(new Set());

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const { signals: rows } = await fetchSignals({ limit: 2000 });
        if (cancelled) return;
        setSignals(rows);
        rows.forEach((r) => {
          if (r.discord?.message_id) seenIdsRef.current.add(r.discord.message_id);
        });
        setLoading(false);
      } catch (e) {
        if (cancelled) return;
        setError((e as Error).message);
        setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    const close = openSignalStream({
      onOpen: () => setConn("connecting"),
      onHello: (d) => {
        setConn("connected");
        setLastEventAt(d.connected_at);
      },
      onHeartbeat: (d) => setLastEventAt(d.ts),
      onSignal: (sig) => {
        setLastEventAt(new Date().toISOString());
        const mid = sig.discord?.message_id;
        if (mid && seenIdsRef.current.has(mid)) return;
        if (mid) seenIdsRef.current.add(mid);
        setSignals((prev) => [sig, ...prev].slice(0, 5000));
      },
      onError: () => setConn("disconnected"),
    });
    return () => {
      close();
      setConn("disconnected");
    };
  }, []);

  return { signals, conn, lastEventAt, loading, error };
}
