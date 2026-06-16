import { useMemo, useState } from "react";
import { useDashboardData } from "./hooks/useDashboardData";
import { usePlanData } from "./hooks/usePlanData";
import { useSwingData } from "./hooks/useSwingData";
import { useExecutorData } from "./hooks/useExecutorData";
import { usePinnedPlans } from "./hooks/usePinnedPlans";
import { usePnlData } from "./hooks/usePnlData";
import { StatusBar } from "./components/StatusBar";
import { Sidebar, type ViewId } from "./components/Sidebar";
import { SignalsView } from "./components/SignalsView";
import { WatchlistView } from "./components/WatchlistView";
import { SwingView } from "./components/SwingView";
import { ExecutorView } from "./components/ExecutorView";
import { PnlView } from "./components/PnlView";
import { Footer } from "./components/Footer";

export default function App() {
  const [view, setView] = useState<ViewId>("signals");

  const dash = useDashboardData();
  const planData = usePlanData();
  const swingData = useSwingData();
  const executorData = useExecutorData();
  const pin = usePinnedPlans();
  const pnlData = usePnlData();

  // The status bar reflects whichever view is active.
  const status = useMemo(() => {
    if (view === "signals") {
      return {
        conn: dash.conn,
        lastEventAt: dash.lastEventAt,
        total: dash.signals.length,
        totalLabel: "signals on file",
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
    if (view === "swings") {
      return {
        conn: swingData.conn,
        lastEventAt: swingData.lastEventAt,
        total: swingData.actions.length,
        totalLabel: "actions on file",
      };
    }
    return {
      conn: executorData.conn,
      lastEventAt: executorData.lastEventAt,
      total: executorData.orders.length,
      totalLabel: "decisions on file",
    };
    if (view === "pnl") {
      return {
        conn: "connected" as const,
        lastEventAt: null,
        total: pnlData.summary?.count ?? 0,
        totalLabel: "trades recorded",
      };
    }
  }, [view, dash, planData, swingData, executorData, pnlData]);

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
          swingCount={swingData.actions.length}
          openPositionsCount={swingData.openPositions.length}
          pinnedCount={pin.count}
          executorOpenCount={
            executorData.book?.summary?.open_tickers ?? 0
          }
          executorDecisionsCount={executorData.orders.length}
          pnlTradeCount={pnlData.summary?.count ?? 0}
        />

        <div className="min-w-0 flex-1 pb-20 md:pb-0">
          {view === "signals" && <SignalsView dash={dash} />}
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
          {view === "swings" && (
            <SwingView
              actions={swingData.actions}
              openPositions={swingData.openPositions}
              loading={swingData.loading}
              error={swingData.error}
            />
          )}
          {view === "executor" && (
            <ExecutorView
              book={executorData.book}
              orders={executorData.orders}
              loading={executorData.loading}
              error={executorData.error}
            />
          )}
          {view === "pnl" && (
            <PnlView
              summary={pnlData.summary}
              loading={pnlData.loading}
              error={pnlData.error}
              onRefresh={pnlData.refetch}
            />
          )}
        </div>
      </div>

      <Footer />
    </div>
  );
}
