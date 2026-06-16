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
import { SwingTradeView } from "./components/SwingTradeView";
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
    return {
      conn: swingData.conn,
      lastEventAt: swingData.lastEventAt,
      total: swingData.openPositions.length,
      totalLabel: "open swing positions",
    };
  }, [view, dash, planData, swingData, pnlData]);

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
    </div>
  );
}
