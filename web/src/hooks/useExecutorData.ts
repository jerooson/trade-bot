import { useEffect, useMemo, useRef, useState } from "react";
import { fetchExecutorBook, fetchExecutorOrders, openOrderStream } from "../lib/api";
import type { ProposedOrder, VirtualBook } from "../lib/types";
import type { ConnectionState } from "./useDashboardData";

interface ExecutorData {
  book: VirtualBook | null;
  orders: ProposedOrder[];
  conn: ConnectionState;
  lastEventAt: string | null;
  loading: boolean;
  error: string | null;
}

/**
 * Live executor state: the virtual book + the proposed-orders feed.
 *
 * - Initial fetch loads the latest book snapshot + recent orders.
 * - SSE stream prepends new orders as they're decided; we refetch the book
 *   (debounced ~1s) so the holdings reflect the latest decision.
 * - Heartbeats keep `lastEventAt` fresh so the status bar shows "alive".
 */
export function useExecutorData(): ExecutorData {
  const [book, setBook] = useState<VirtualBook | null>(null);
  const [orders, setOrders] = useState<ProposedOrder[]>([]);
  const [conn, setConn] = useState<ConnectionState>("connecting");
  const [lastEventAt, setLastEventAt] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const seenIdsRef = useRef<Set<string>>(new Set());

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [b, o] = await Promise.all([
          fetchExecutorBook(),
          fetchExecutorOrders({ limit: 2000 }),
        ]);
        if (cancelled) return;
        setBook(b);
        setOrders(o.orders);
        o.orders.forEach((r) => seenIdsRef.current.add(r.id));
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
    const scheduleBookRefetch = () => {
      if (refetchTimer) return;
      // Debounce bursts of decisions into a single book refresh.
      refetchTimer = window.setTimeout(async () => {
        refetchTimer = null;
        try {
          const b = await fetchExecutorBook();
          setBook(b);
        } catch {
          // Soft-fail; stream still works.
        }
      }, 800);
    };

    const close = openOrderStream({
      onOpen: () => setConn("connecting"),
      onHello: (d) => {
        setConn("connected");
        setLastEventAt(d.connected_at);
      },
      onHeartbeat: (d) => setLastEventAt(d.ts),
      onOrder: (order) => {
        setLastEventAt(new Date().toISOString());
        if (seenIdsRef.current.has(order.id)) return;
        seenIdsRef.current.add(order.id);
        setOrders((prev) => [order, ...prev].slice(0, 2000));
        scheduleBookRefetch();
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
    () => ({ book, orders, conn, lastEventAt, loading, error }),
    [book, orders, conn, lastEventAt, loading, error],
  );
}
