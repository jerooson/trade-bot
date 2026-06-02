import { useEffect, useRef, useState } from "react";
import { fetchPlans, openPlanStream } from "../lib/api";
import type { TradePlan } from "../lib/types";
import type { ConnectionState } from "./useDashboardData";

interface PlanData {
  plans: TradePlan[];
  conn: ConnectionState;
  lastEventAt: string | null;
  loading: boolean;
  error: string | null;
}

/**
 * Mirror of useDashboardData but for the trade-plan channel.
 * Initial REST fetch + live SSE merge, deduped by Discord message_id.
 */
export function usePlanData(): PlanData {
  const [plans, setPlans] = useState<TradePlan[]>([]);
  const [conn, setConn] = useState<ConnectionState>("connecting");
  const [lastEventAt, setLastEventAt] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const seenIdsRef = useRef<Set<number>>(new Set());

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const { plans: rows } = await fetchPlans({ limit: 2000 });
        if (cancelled) return;
        setPlans(rows);
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
    const close = openPlanStream({
      onOpen: () => setConn("connecting"),
      onHello: (d) => {
        setConn("connected");
        setLastEventAt(d.connected_at);
      },
      onHeartbeat: (d) => setLastEventAt(d.ts),
      onPlan: (p) => {
        setLastEventAt(new Date().toISOString());
        const mid = p.discord?.message_id;
        if (mid && seenIdsRef.current.has(mid)) return;
        if (mid) seenIdsRef.current.add(mid);
        setPlans((prev) => [p, ...prev].slice(0, 5000));
      },
      onError: () => setConn("disconnected"),
    });
    return () => {
      close();
      setConn("disconnected");
    };
  }, []);

  return { plans, conn, lastEventAt, loading, error };
}
