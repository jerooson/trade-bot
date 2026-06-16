import { useMemo } from "react";
import clsx from "clsx";
import type { PnlRecord, PnlSummary } from "../lib/types";
import { relativeTime } from "../lib/format";

interface Props {
  summary: PnlSummary | null;
  loading: boolean;
  error: string | null;
  onRefresh: () => void;
}

export function PnlView({ summary, loading, error, onRefresh }: Props) {
  const byTicker = useMemo(() => {
    if (!summary) return [];
    const map = new Map<string, { buys: PnlRecord[]; sells: PnlRecord[] }>();
    for (const r of summary.records) {
      if (!map.has(r.ticker)) map.set(r.ticker, { buys: [], sells: [] });
      const entry = map.get(r.ticker)!;
      if (r.action === "BUY") entry.buys.push(r);
      else entry.sells.push(r);
    }
    return Array.from(map.entries()).map(([ticker, { buys, sells }]) => {
      const realized = sells.reduce((s, r) => s + (r.realized_pnl ?? 0), 0);
      const lastBuy = buys[0];
      return { ticker, buys, sells, realized, lastBuy };
    });
  }, [summary]);

  const totalPnl = summary?.total_realized_pnl ?? 0;
  const wins = summary?.wins ?? 0;
  const losses = summary?.losses ?? 0;

  return (
    <main className="relative z-10 mx-auto max-w-[1400px] px-6 pb-16 pt-8">
      {/* Header */}
      <div className="mb-6">
        <div className="flex items-end justify-between border-b border-ink-500/40 pb-4">
          <div>
            <div className="text-[10px] uppercase tracking-[0.32em] text-bone-500">
              section · 05 · p&amp;l
            </div>
            <h2 className="mt-1 font-editorial text-5xl italic leading-none text-bone-50">
              trades &amp;&nbsp;
              <span className={clsx(totalPnl >= 0 ? "text-crt-long" : "text-crt-short")}>
                {totalPnl >= 0 ? "profit" : "loss"}
              </span>
              <span className="text-bone-300">.</span>
            </h2>
          </div>
          <button
            onClick={onRefresh}
            className="border border-ink-500/60 px-3 py-1.5 text-[10px] uppercase tracking-[0.32em] text-bone-400 hover:border-bone-300 hover:text-bone-100 transition-colors"
          >
            refresh
          </button>
        </div>
      </div>

      {error && (
        <div className="mb-6 border border-crt-short/60 bg-crt-short/10 px-4 py-3 text-sm text-crt-short">
          ERR — {error}
        </div>
      )}
      {loading && (
        <div className="mb-6 border border-ink-500/40 px-4 py-3 text-[11px] uppercase tracking-[0.32em] text-bone-400">
          loading…
        </div>
      )}

      {!loading && summary && (
        <>
          {/* Summary strip */}
          <div className="mb-6 grid grid-cols-2 gap-3 md:grid-cols-4">
            <StatTile
              label="Realized P&L"
              value={`${totalPnl >= 0 ? "+" : ""}$${totalPnl.toFixed(2)}`}
              valueClass={totalPnl >= 0 ? "text-crt-long" : "text-crt-short"}
              caption="all closed trades"
            />
            <StatTile
              label="Trades"
              value={summary.count.toString()}
              caption={`${wins} wins · ${losses} losses`}
            />
            <StatTile
              label="Win Rate"
              value={wins + losses > 0 ? `${Math.round((wins / (wins + losses)) * 100)}%` : "—"}
              caption="closed trades only"
            />
            <StatTile
              label="Open"
              value={byTicker.filter((t) => t.buys.length > 0 && t.sells.filter(s => s.kind === "CLOSE").length === 0).length.toString()}
              caption="tickers with open position"
            />
          </div>

          {/* Per-ticker breakdown */}
          <section className="mb-6 border border-ink-500/40 bg-ink-900/20">
            <header className="border-b border-ink-500/30 px-4 py-3">
              <h3 className="font-editorial text-xl italic text-bone-100">Positions</h3>
              <div className="mt-0.5 text-[10px] uppercase tracking-[0.32em] text-bone-500">
                actual fill prices · avg cost · realized p&l
              </div>
            </header>
            {byTicker.length === 0 ? (
              <EmptyRow text="No trades recorded yet." />
            ) : (
              <div className="flex flex-col">
                <PositionHeader />
                {byTicker.map(({ ticker, buys, sells, realized }) => (
                  <PositionRow key={ticker} ticker={ticker} buys={buys} sells={sells} realized={realized} />
                ))}
              </div>
            )}
          </section>

          {/* Full trade log */}
          <section className="mb-6 border border-ink-500/40 bg-ink-900/20">
            <header className="border-b border-ink-500/30 px-4 py-3">
              <h3 className="font-editorial text-xl italic text-bone-100">Trade Log</h3>
              <div className="mt-0.5 text-[10px] uppercase tracking-[0.32em] text-bone-500">
                every order — newest first
              </div>
            </header>
            <div className="flex flex-col">
              <TradeLogHeader />
              {summary.records.map((r) => (
                <TradeLogRow key={r.order_id} record={r} />
              ))}
            </div>
          </section>
        </>
      )}
    </main>
  );
}

// -- Sub-components ----------------------------------------------------------

function StatTile({ label, value, valueClass, caption }: { label: string; value: string; valueClass?: string; caption?: string }) {
  return (
    <div className="border border-ink-500/40 bg-ink-900/40 px-4 py-3">
      <div className="text-[9px] uppercase tracking-[0.32em] text-bone-500">{label}</div>
      <div className={clsx("mt-1 font-mono text-2xl", valueClass ?? "text-bone-50")}>{value}</div>
      {caption && <div className="mt-0.5 text-[10px] uppercase tracking-[0.18em] text-bone-500">{caption}</div>}
    </div>
  );
}

function PositionHeader() {
  return (
    <div className="grid grid-cols-12 gap-3 border-b border-ink-500/30 bg-ink-900/40 px-4 py-2 text-[9px] uppercase tracking-[0.32em] text-bone-500">
      <div className="col-span-2">ticker</div>
      <div className="col-span-2 text-right">avg cost</div>
      <div className="col-span-2 text-right">fills</div>
      <div className="col-span-2 text-right">deployed</div>
      <div className="col-span-2 text-right">realized p&l</div>
      <div className="col-span-2 text-right">status</div>
    </div>
  );
}

function PositionRow({ ticker, buys, sells, realized }: { ticker: string; buys: PnlRecord[]; sells: PnlRecord[]; realized: number }) {
  const lastBuy = buys[buys.length - 1];
  const avgCost = lastBuy?.avg_cost_after;
  const isClosed = sells.some((s) => s.kind === "CLOSE");
  const totalDeployed = buys.reduce((s, r) => s + (r.fill_usd ?? 0), 0);
  const hasPnl = sells.length > 0;

  return (
    <div className="grid grid-cols-12 items-center gap-3 border-b border-ink-500/20 px-4 py-3 hover:bg-ink-800/30">
      <div className="col-span-2">
        <span className="font-editorial text-xl italic text-bone-50">{ticker}</span>
      </div>
      <div className="col-span-2 text-right tabular text-sm text-bone-200">
        {avgCost != null ? `$${avgCost.toFixed(4)}` : "—"}
      </div>
      <div className="col-span-2 text-right tabular text-[11px] text-bone-400">
        {buys.length}B / {sells.length}S
      </div>
      <div className="col-span-2 text-right tabular text-sm text-bone-50">
        ${totalDeployed.toFixed(2)}
      </div>
      <div className="col-span-2 text-right">
        {hasPnl ? (
          <span className={clsx("tabular text-sm font-medium", realized >= 0 ? "text-crt-long" : "text-crt-short")}>
            {realized >= 0 ? "+" : ""}${realized.toFixed(2)}
          </span>
        ) : (
          <span className="tabular text-[11px] text-bone-500">open</span>
        )}
      </div>
      <div className="col-span-2 text-right">
        <span className={clsx(
          "inline-flex items-center border px-1.5 py-0.5 text-[9px] uppercase tracking-[0.18em]",
          isClosed ? "border-ink-500/60 text-bone-500" : "border-crt-long/60 bg-crt-long/10 text-crt-long"
        )}>
          {isClosed ? "closed" : "open"}
        </span>
      </div>
    </div>
  );
}

function TradeLogHeader() {
  return (
    <div className="grid grid-cols-12 gap-3 border-b border-ink-500/30 bg-ink-900/40 px-4 py-2 text-[9px] uppercase tracking-[0.32em] text-bone-500">
      <div className="col-span-2">when</div>
      <div className="col-span-1">action</div>
      <div className="col-span-1">ticker</div>
      <div className="col-span-1">kind</div>
      <div className="col-span-2 text-right">fill price</div>
      <div className="col-span-1 text-right">qty</div>
      <div className="col-span-2 text-right">avg cost</div>
      <div className="col-span-2 text-right">realized p&l</div>
    </div>
  );
}

function TradeLogRow({ record: r }: { record: PnlRecord }) {
  const hasPnl = r.realized_pnl != null;
  return (
    <div className="grid grid-cols-12 items-baseline gap-3 border-b border-ink-500/20 px-4 py-2.5 hover:bg-ink-800/30">
      <div className="col-span-2 tabular text-[11px] text-bone-400">{relativeTime(r.timestamp)}</div>
      <div className="col-span-1">
        <span className={clsx(
          "inline-flex items-center justify-center border px-1.5 py-0.5 text-[9px] uppercase tracking-[0.18em]",
          r.action === "BUY"
            ? "border-crt-long/60 bg-crt-long/10 text-crt-long"
            : "border-crt-short/60 bg-crt-short/10 text-crt-short"
        )}>
          {r.action}
        </span>
      </div>
      <div className="col-span-1 font-editorial text-base italic text-bone-100">{r.ticker}</div>
      <div className="col-span-1 tabular text-[10px] uppercase tracking-[0.18em] text-bone-500">{r.kind}</div>
      <div className="col-span-2 text-right">
        <div className="tabular text-sm text-bone-50">
          {r.fill_price != null ? `$${r.fill_price.toFixed(4)}` : "—"}
        </div>
        {r.signal_price != null && r.fill_price != null && (
          <div className="tabular text-[10px] text-bone-500">
            signal ${r.signal_price.toFixed(2)}
          </div>
        )}
      </div>
      <div className="col-span-1 text-right tabular text-[11px] text-bone-400">
        {r.fill_qty != null ? r.fill_qty.toFixed(4) : "—"}
      </div>
      <div className="col-span-2 text-right tabular text-[11px] text-bone-400">
        {r.avg_cost_after != null ? `$${r.avg_cost_after.toFixed(4)}` : "—"}
      </div>
      <div className="col-span-2 text-right">
        {hasPnl ? (
          <div>
            <div className={clsx("tabular text-sm font-medium", (r.realized_pnl ?? 0) >= 0 ? "text-crt-long" : "text-crt-short")}>
              {(r.realized_pnl ?? 0) >= 0 ? "+" : ""}${r.realized_pnl!.toFixed(2)}
            </div>
            <div className={clsx("tabular text-[10px]", (r.realized_pnl_pct ?? 0) >= 0 ? "text-crt-long/70" : "text-crt-short/70")}>
              {(r.realized_pnl_pct ?? 0) >= 0 ? "+" : ""}{r.realized_pnl_pct!.toFixed(2)}%
            </div>
          </div>
        ) : (
          <span className="tabular text-[11px] text-bone-600">—</span>
        )}
      </div>
    </div>
  );
}

function EmptyRow({ text }: { text: string }) {
  return (
    <div className="px-6 py-12 text-center">
      <div className="font-editorial text-lg italic text-bone-400">{text}</div>
    </div>
  );
}
