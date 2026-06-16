import { useEffect, useMemo, useState } from "react";
import { fetchPnl } from "../lib/api";
import type { PnlSummary } from "../lib/types";

interface PnlData {
  summary: PnlSummary | null;
  loading: boolean;
  error: string | null;
  refetch: () => void;
}

export function usePnlData(): PnlData {
  const [summary, setSummary] = useState<PnlSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);

  const refetch = () => setTick((n) => n + 1);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    fetchPnl()
      .then((data) => {
        if (!cancelled) {
          setSummary(data);
          setLoading(false);
        }
      })
      .catch((e) => {
        if (!cancelled) {
          setError((e as Error).message);
          setLoading(false);
        }
      });
    return () => { cancelled = true; };
  }, [tick]);

  return useMemo(() => ({ summary, loading, error, refetch }), [summary, loading, error]);
}
