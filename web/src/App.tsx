import { useMemo, useState } from "react";
import { useDashboardData } from "./hooks/useDashboardData";
import { usePlanData } from "./hooks/usePlanData";
import { useSwingData } from "./hooks/useSwingData";
import { useExecutorData } from "./hooks/useExecutorData";
import { usePinnedPlans } from "./hooks/usePinnedPlans";
import { usePnlData } from "./hooks/usePnlData";
import { useDayTradeData } from "./hooks/useDayTradeData";
import { StatusBar } from "./components/StatusBar";
import { Sidebar, type ViewId } from "./components/Sidebar";
import { DayTradeView } from "./components/DayTradeView";
import { WatchlistView } from "./components/WatchlistView";
import { SwingTradeView } from "./components/SwingTradeView";
import { ChatPanel } from "./components/ChatPanel";
import { Footer } from "./components/Footer";

export default function App() {
  const [view, setView] = useState<ViewId>("daytrade");

  const dash = useDashboardData();
  const planData = usePlanData();
  const swingData = useSwingData();
  const executorData = useExecutorData();
  const pin = usePinnedPlans();
  const pnlData = usePnlData();
  const dayTradeData = useDayTradeData();

  const status = useMemo(() => {
    if (view === "daytrade") {
      return {
        conn: dash.conn,
        lastEventAt: dash.lastEventAt,
        total: dayTradeData.positions.filter(p => p.status === "open").length,
        totalLabel: "day trades open",
      };
    }
    if (view === "watchlist") {
      return {
        conn: planData.conn,
        lastEventAt: planData.lastEventAt,
        total: planData.plans.length,
        totalLabel: "plans on file",
      };
    }
    return {
      conn: swingData.conn,
      lastEventAt: swingData.lastEventAt,
      total: swingData.openPositions.length,
      totalLabel: "open swing positions",
    };
  }, [view, dash, planData, swingData, dayTradeData]);

  const activeDayTradesCount =
    dayTradeData.positions.filter(
      p => p.status === "open" || p.status === "watching" || p.status === "pending_entry" || p.status === "pending_exit"
    ).length;

  return (
    <div className="relative min-h-screen">
      <StatusBar
        conn={status.conn}
        lastEventAt={status.lastEventAt}
        total={status.total}
        totalLabel={status.totalLabel}
      />

      <div className="relative z-10 mx-auto flex max-w-[1680px]">
        <Sidebar
          current={view}
          onChange={setView}
          signalCount={dash.signals.length}
          planCount={planData.plans.length}
          openPositionsCount={swingData.openPositions.length}
          pnlTradeCount={pnlData.summary?.count ?? 0}
          activeDayTradesCount={activeDayTradesCount}
        />

        <div className="min-w-0 flex-1 pb-20 md:pb-0">
          {view === "daytrade" && (
            <DayTradeView
              dash={dash}
              positions={dayTradeData.positions}
              manualPlans={dayTradeData.manualPlans}
              heatIdeas={dayTradeData.heatIdeas}
              heatSettings={dayTradeData.heatSettings}
              pnl={dayTradeData.pnl}
              serviceRunning={dayTradeData.serviceRunning}
              onManualPlansChanged={dayTradeData.refetch}
            />
          )}
          {view === "watchlist" && (
            <WatchlistView
              plans={planData.plans}
              loading={planData.loading}
              error={planData.error}
              isPinned={pin.isPinned}
              onTogglePin={pin.toggle}
              pinnedCount={pin.count}
            />
          )}
          {view === "swing" && (
            <SwingTradeView
              actions={swingData.actions}
              openPositions={swingData.openPositions}
              swingLoading={swingData.loading}
              swingError={swingData.error}
              pnlSummary={pnlData.summary}
              pnlLoading={pnlData.loading}
              pnlError={pnlData.error}
              onPnlRefresh={pnlData.refetch}
              book={executorData.book}
              orders={executorData.orders}
              executorLoading={executorData.loading}
              executorError={executorData.error}
            />
          )}
        </div>
      </div>

      <Footer />
      <ChatPanel />
    </div>
  );
}
