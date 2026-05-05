import { useMemo, useState } from "react";
import { useDashboardData } from "./hooks/useDashboardData";
import { StatusBar } from "./components/StatusBar";
import { MetricsStrip } from "./components/MetricsStrip";
import { SignalFeed } from "./components/SignalFeed";
import { TopTickers } from "./components/TopTickers";
import { HourlyChart } from "./components/HourlyChart";
import { DailyChart } from "./components/DailyChart";
import { Footer } from "./components/Footer";
import { DateRangeBar } from "./components/DateRangeBar";
import { filterByRange, isSingleDay, type DateRange } from "./lib/filters";
import { deriveStats } from "./lib/derive";

export default function App() {
  const { signals, conn, lastEventAt, loading, error } = useDashboardData();
  const [range, setRange] = useState<DateRange>("TODAY");

  const filteredSignals = useMemo(() => filterByRange(signals, range), [signals, range]);

  // All client-side derived: instant, no round-trip.
  const stats = useMemo(() => deriveStats(filteredSignals), [filteredSignals]);
  const allTimeStats = useMemo(() => deriveStats(signals), [signals]);

  const showDaily = !isSingleDay(range) && filteredSignals.length > 0;

  return (
    <div className="relative min-h-screen">
      <StatusBar
        conn={conn}
        lastEventAt={lastEventAt}
        totalSignals={signals.length}
      />

      <main className="relative z-10 mx-auto max-w-[1600px] px-6 pb-16 pt-8">
        {/* Hero strip with editorial title bar */}
        <div className="mb-6">
          <div className="flex items-end justify-between border-b border-ink-500/40 pb-4">
            <div>
              <div className="text-[10px] uppercase tracking-[0.32em] text-bone-500">
                section · 00 · overview
              </div>
              <h2 className="mt-1 font-editorial text-5xl italic leading-none text-bone-50">
                live signals,&nbsp;
                <span className="text-crt-amber">parsed</span>
                <span className="text-bone-300">.</span>
              </h2>
            </div>

            <div className="hidden text-right md:block">
              <div className="text-[10px] uppercase tracking-[0.32em] text-bone-500">
                full capture range
              </div>
              <div className="mt-1 tabular text-sm text-bone-300">
                {allTimeStats.earliest?.slice(0, 10) ?? "—"} →{" "}
                {allTimeStats.latest?.slice(0, 10) ?? "—"}
              </div>
            </div>
          </div>
        </div>

        {error && (
          <div className="mb-6 border border-crt-short/60 bg-crt-short/10 px-4 py-3 text-sm text-crt-short">
            ERR — {error}
          </div>
        )}
        {loading && (
          <div className="mb-6 border border-ink-500/40 px-4 py-3 text-[11px] uppercase tracking-[0.32em] text-bone-400">
            loading capture file…
          </div>
        )}

        {/* Date range filter -- single source of truth for the rest of the page */}
        <div className="mb-6">
          <DateRangeBar
            value={range}
            onChange={setRange}
            totalInRange={filteredSignals.length}
            totalAll={signals.length}
          />
        </div>

        {/* Metrics */}
        <div className="mb-12">
          <MetricsStrip stats={stats} range={range} />
        </div>

        {/* Two-column layout */}
        <div className="grid grid-cols-1 gap-12 lg:grid-cols-[minmax(0,1.8fr)_minmax(0,1fr)]">
          <div className="space-y-12">
            <SignalFeed signals={filteredSignals} totalAll={signals.length} />
            {showDaily && <DailyChart stats={stats} />}
          </div>
          <aside className="space-y-12">
            <TopTickers stats={stats} />
            <HourlyChart stats={stats} singleDay={isSingleDay(range)} />
          </aside>
        </div>
      </main>

      <Footer />
    </div>
  );
}
