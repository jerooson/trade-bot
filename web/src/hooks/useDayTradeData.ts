import { useCallback, useEffect, useRef, useState } from "react";
import { fetchDayTradeState } from "../lib/api";
import type { DayTradePnl, DayTradePosition, HeatIdea, HeatSettings, ManualDayPlan } from "../lib/types";

interface DayTradeDataState {
  positions: DayTradePosition[];
  manualPlans: ManualDayPlan[];
  heatIdeas: HeatIdea[];
  heatSettings: HeatSettings;
  pnl: DayTradePnl | null;
  serviceRunning: boolean;
  loading: boolean;
  error: string | null;
  refetch: () => Promise<void>;
}

const POLL_INTERVAL_MS = 15_000;

export function useDayTradeData(): DayTradeDataState {
  const [positions, setPositions] = useState<DayTradePosition[]>([]);
  const [manualPlans, setManualPlans] = useState<ManualDayPlan[]>([]);
  const [heatIdeas, setHeatIdeas] = useState<HeatIdea[]>([]);
  const [heatSettings, setHeatSettings] = useState<HeatSettings>({ auto_trading_enabled: false, updated_at: null });
  const [pnl, setPnl] = useState<DayTradePnl | null>(null);
  const [serviceRunning, setServiceRunning] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const load = useCallback(async () => {
    try {
      const data = await fetchDayTradeState();
      setPositions(data.positions);
      setManualPlans(data.manual_plans ?? []);
      setHeatIdeas(data.heat_ideas ?? []);
      setHeatSettings(data.heat_settings ?? { auto_trading_enabled: false, updated_at: null });
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

  return { positions, manualPlans, heatIdeas, heatSettings, pnl, serviceRunning, loading, error, refetch: load };
}
