import { useEffect, useMemo, useRef, useState } from "react";
import { fetchSwings, openSwingStream } from "../lib/api";
import type { OpenPosition, TradeAction } from "../lib/types";
import type { ConnectionState } from "./useDashboardData";

interface SwingData {
  actions: TradeAction[];
  openPositions: OpenPosition[];
  conn: ConnectionState;
  lastEventAt: string | null;
  loading: boolean;
  error: string | null;
}

/**
 * Live data for the swing-trade execution channel.
 *
 * Initial fetch returns both the action history AND a derived "current open
 * positions" snapshot (computed server-side by folding actions chronologically).
 * The SSE stream prepends new actions; we don't re-derive positions on the
 * client because that requires the FULL history -- instead, when an actionable
 * event arrives we trigger a lightweight refetch of open positions only.
 */
export function useSwingData(): SwingData {
  const [actions, setActions] = useState<TradeAction[]>([]);
  const [openPositions, setOpenPositions] = useState<OpenPosition[]>([]);
  const [conn, setConn] = useState<ConnectionState>("connecting");
  const [lastEventAt, setLastEventAt] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const seenIdsRef = useRef<Set<number>>(new Set());

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const data = await fetchSwings({ limit: 5000 });
        if (cancelled) return;
        setActions(data.actions);
        setOpenPositions(data.open_positions);
        data.actions.forEach((r) => {
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
    let refetchTimer: number | null = null;
    const scheduleRefetch = () => {
      if (refetchTimer) return;
      // Coalesce bursts of events into a single refetch ~1s later.
      refetchTimer = window.setTimeout(async () => {
        refetchTimer = null;
        try {
          const data = await fetchSwings({ limit: 5000 });
          setOpenPositions(data.open_positions);
        } catch {
          // Soft-fail: stream will keep working; positions just stale.
        }
      }, 1000);
    };

    const close = openSwingStream({
      onOpen: () => setConn("connecting"),
      onHello: (d) => {
        setConn("connected");
        setLastEventAt(d.connected_at);
      },
      onHeartbeat: (d) => setLastEventAt(d.ts),
      onSwing: (a) => {
        setLastEventAt(new Date().toISOString());
        const mid = a.discord?.message_id;
        if (mid && seenIdsRef.current.has(mid)) return;
        if (mid) seenIdsRef.current.add(mid);
        setActions((prev) => [a, ...prev].slice(0, 5000));
        scheduleRefetch();
      },
      onError: () => setConn("disconnected"),
    });
    return () => {
      close();
      if (refetchTimer) window.clearTimeout(refetchTimer);
      setConn("disconnected");
    };
  }, []);

  return useMemo(
    () => ({ actions, openPositions, conn, lastEventAt, loading, error }),
    [actions, openPositions, conn, lastEventAt, loading, error],
  );
}
