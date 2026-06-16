import { useCallback, useEffect, useRef, useState } from "react";
import { fetchDayTradeState } from "../lib/api";
import type { DayTradePnl, DayTradePosition } from "../lib/types";

interface DayTradeDataState {
  positions: DayTradePosition[];
  pnl: DayTradePnl | null;
  serviceRunning: boolean;
  loading: boolean;
  error: string | null;
  refetch: () => void;
}

const POLL_INTERVAL_MS = 15_000;

export function useDayTradeData(): DayTradeDataState {
  const [positions, setPositions] = useState<DayTradePosition[]>([]);
  const [pnl, setPnl] = useState<DayTradePnl | null>(null);
  const [serviceRunning, setServiceRunning] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const load = useCallback(async () => {
    try {
      const data = await fetchDayTradeState();
      setPositions(data.positions);
      setPnl(data.pnl);
      setServiceRunning(data.service_running);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
    timerRef.current = setInterval(load, POLL_INTERVAL_MS);
    return () => {
      if (timerRef.current != null) clearInterval(timerRef.current);
    };
  }, [load]);

  return { positions, pnl, serviceRunning, loading, error, refetch: load };
}
