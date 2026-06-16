import { useMemo, useState } from "react";
import clsx from "clsx";
import type {
  OpenPosition,
  PnlRecord,
  PnlSummary,
  ProposedOrder,
  TradeAction,
  VirtualBook,
  VirtualPosition,
} from "../lib/types";
import { ActionRow } from "./ActionRow";
import { PositionCard } from "./PositionCard";
import { fmtPrice, relativeTime } from "../lib/format";

type SubTab = "positions" | "pnl" | "book";

interface Props {
  // Swing (Discord signals)
  actions: TradeAction[];
  openPositions: OpenPosition[];
  swingLoading: boolean;
  swingError: string | null;
  // P&L (actual Robinhood fills)
  pnlSummary: PnlSummary | null;
  pnlLoading: boolean;
  pnlError: string | null;
  onPnlRefresh: () => void;
  // Executor (virtual book)
  book: VirtualBook | null;
  orders: ProposedOrder[];
  executorLoading: boolean;
  executorError: string | null;
}

export function SwingTradeView({
  actions, openPositions, swingLoading, swingError,
  pnlSummary, pnlLoading, pnlError, onPnlRefresh,
  book, orders, executorLoading, executorError,
}: Props) {
  const [sub, setSub] = useState<SubTab>("positions");

  const totalPnl = pnlSummary?.total_realized_pnl ?? 0;
  const deployedUsd = book?.summary?.total_deployed_usd ?? 0;
  const openCount = openPositions.length;

  return (
    <main className="relative z-10 mx-auto max-w-[1400px] px-6 pb-16 pt-8">
      {/* Header */}
      <div className="mb-6">
        <div className="flex items-end justify-between border-b border-ink-500/40 pb-4">
          <div>
            <div className="text-[10px] uppercase tracking-[0.32em] text-bone-500">
              swing trading
            </div>
            <h2 className="mt-1 font-editorial text-5xl italic leading-none text-bone-50">
              positions&nbsp;&amp;&nbsp;
              <span className={clsx(totalPnl >= 0 ? "text-crt-long" : "text-crt-short")}>
                {totalPnl >= 0 ? "gains" : "losses"}
              </span>
              <span className="text-bone-300">.</span>
            </h2>
          </div>
        </div>
      </div>

      {/* Quick stats */}
      <div className="mb-6 grid grid-cols-2 gap-3 md:grid-cols-4">
        <StatTile label="Open Positions" value={openCount.toString()} caption="from signals" />
        <StatTile
          label="Deployed (virtual)"
          value={`$${deployedUsd.toFixed(2)}`}
          caption="executor book"
        />
        <StatTile
          label="Realized P&L"
          value={`${totalPnl >= 0 ? "+" : ""}$${totalPnl.toFixed(2)}`}
          valueClass={totalPnl >= 0 ? "text-crt-long" : "text-crt-short"}
          caption={`${pnlSummary?.wins ?? 0}W · ${pnlSummary?.losses ?? 0}L`}
        />
        <StatTile
          label="Trades Filed"
          value={(pnlSummary?.count ?? 0).toString()}
          caption="actual broker fills"
        />
      </div>

      {/* Sub-tab nav */}
      <div className="mb-4 flex items-center gap-2">
        {(["positions", "pnl", "book"] as SubTab[]).map((t) => (
          <button
            key={t}
            onClick={() => setSub(t)}
            className={clsx(
              "border px-3 py-1.5 text-[10px] uppercase tracking-[0.28em] transition-colors",
              sub === t
                ? "border-crt-amber/60 bg-crt-amber/10 text-crt-amber"
                : "border-ink-500/60 text-bone-400 hover:border-bone-300 hover:text-bone-100",
            )}
          >
            {t === "pnl" ? "P&L" : t === "book" ? "Virtual Book" : "Positions"}
          </button>
        ))}
      </div>

      {/* Sub-tab content */}
      {sub === "positions" && (
        <PositionsTab
          actions={actions}
          openPositions={openPositions}
          loading={swingLoading}
          error={swingError}
        />
      )}
      {sub === "pnl" && (
        <PnlTab
          summary={pnlSummary}
          loading={pnlLoading}
          error={pnlError}
          onRefresh={onPnlRefresh}
        />
      )}
      {sub === "book" && (
        <BookTab
          book={book}
          orders={orders}
          loading={executorLoading}
          error={executorError}
        />
      )}
    </main>
  );
}

// ---------------------------------------------------------------------------
// Positions tab
// ---------------------------------------------------------------------------

function PositionsTab({ actions, openPositions, loading, error }: {
  actions: TradeAction[];
  openPositions: OpenPosition[];
  loading: boolean;
  error: string | null;
}) {
  const actionable = useMemo(
    () => actions.filter((a) => ["ENTRY","ADD","REDUCE","CLOSE","STOP_TRIGGER"].includes(a.kind)),
    [actions],
  );

  return (
    <>
      {error && <ErrBanner msg={error} />}
      {loading && <LoadingBanner />}

      <Section title="Open Positions" subtitle="derived from Discord swing signals">
        {openPositions.length === 0 ? (
          <EmptyRow text="No open positions." />
        ) : (
          <div className="grid gap-3 p-4 sm:grid-cols-2 lg:grid-cols-3">
            {openPositions.map((p) => (
              <PositionCard key={p.ticker} pos={p} />
            ))}
          </div>
        )}
      </Section>

      <Section title="Action Tape" subtitle="entries, adds, reduces, closes, stops">
        {actionable.length === 0 ? (
          <EmptyRow text="No actions yet." />
        ) : (
          <div className="flex flex-col">
            {actionable.slice(0, 100).map((a, i) => (
              <ActionRow key={i} action={a} />
            ))}
          </div>
        )}
      </Section>
    </>
  );
}

// ---------------------------------------------------------------------------
// P&L tab
// ---------------------------------------------------------------------------

function PnlTab({ summary, loading, error, onRefresh }: {
  summary: PnlSummary | null;
  loading: boolean;
  error: string | null;
  onRefresh: () => void;
}) {
  const byTicker = useMemo(() => {
    if (!summary) return [];
    const map = new Map<string, { buys: PnlRecord[]; sells: PnlRecord[] }>();
    for (const r of [...summary.records].reverse()) {
      if (!map.has(r.ticker)) map.set(r.ticker, { buys: [], sells: [] });
      const e = map.get(r.ticker)!;
      if (r.action === "BUY") e.buys.push(r);
      else e.sells.push(r);
    }
    return Array.from(map.entries()).map(([ticker, { buys, sells }]) => ({
      ticker,
      buys,
      sells,
      realized: sells.reduce((s, r) => s + (r.realized_pnl ?? 0), 0),
      isClosed: sells.some((s) => s.kind === "CLOSE"),
      avgCost: buys.at(-1)?.avg_cost_after ?? null,
      deployed: buys.reduce((s, r) => s + (r.fill_usd ?? 0), 0),
    }));
  }, [summary]);

  return (
    <>
      {error && <ErrBanner msg={error} />}
      {loading && <LoadingBanner />}

      <Section
        title="Ticker Summary"
        subtitle="actual fill prices · avg cost · realized P&L"
        trailing={
          <button
            onClick={onRefresh}
            className="border border-ink-500/60 px-2.5 py-1 text-[10px] uppercase tracking-[0.28em] text-bone-400 hover:text-bone-100 transition-colors"
          >
            refresh
          </button>
        }
      >
        {byTicker.length === 0 ? (
          <EmptyRow text="No trades recorded yet." />
        ) : (
          <div className="flex flex-col">
            <div className="grid grid-cols-12 gap-3 border-b border-ink-500/30 bg-ink-900/40 px-4 py-2 text-[9px] uppercase tracking-[0.32em] text-bone-500">
              <div className="col-span-2">ticker</div>
              <div className="col-span-2 text-right">avg cost</div>
              <div className="col-span-2 text-right">fills</div>
              <div className="col-span-2 text-right">deployed</div>
              <div className="col-span-2 text-right">realized p&l</div>
              <div className="col-span-2 text-right">status</div>
            </div>
            {byTicker.map(({ ticker, buys, sells, realized, isClosed, avgCost, deployed }) => (
              <div key={ticker} className="grid grid-cols-12 items-center gap-3 border-b border-ink-500/20 px-4 py-3 hover:bg-ink-800/30">
                <div className="col-span-2 font-editorial text-xl italic text-bone-50">{ticker}</div>
                <div className="col-span-2 text-right tabular text-sm text-bone-200">{avgCost != null ? `$${avgCost.toFixed(4)}` : "—"}</div>
                <div className="col-span-2 text-right tabular text-[11px] text-bone-400">{buys.length}B / {sells.length}S</div>
                <div className="col-span-2 text-right tabular text-sm text-bone-50">${deployed.toFixed(2)}</div>
                <div className="col-span-2 text-right">
                  {sells.length > 0 ? (
                    <span className={clsx("tabular text-sm font-medium", realized >= 0 ? "text-crt-long" : "text-crt-short")}>
                      {realized >= 0 ? "+" : ""}${realized.toFixed(2)}
                    </span>
                  ) : (
                    <span className="tabular text-[11px] text-bone-500">open</span>
                  )}
                </div>
                <div className="col-span-2 text-right">
                  <span className={clsx("inline-flex items-center border px-1.5 py-0.5 text-[9px] uppercase tracking-[0.18em]",
                    isClosed ? "border-ink-500/60 text-bone-500" : "border-crt-long/60 bg-crt-long/10 text-crt-long"
                  )}>
                    {isClosed ? "closed" : "open"}
                  </span>
                </div>
              </div>
            ))}
          </div>
        )}
      </Section>

      <Section title="Trade Log" subtitle="every broker fill — newest first">
        {!summary || summary.records.length === 0 ? (
          <EmptyRow text="No trades yet." />
        ) : (
          <div className="flex flex-col">
            <div className="grid grid-cols-12 gap-3 border-b border-ink-500/30 bg-ink-900/40 px-4 py-2 text-[9px] uppercase tracking-[0.32em] text-bone-500">
              <div className="col-span-2">when</div>
              <div className="col-span-1">side</div>
              <div className="col-span-1">ticker</div>
              <div className="col-span-1">kind</div>
              <div className="col-span-2 text-right">fill price</div>
              <div className="col-span-1 text-right">qty</div>
              <div className="col-span-2 text-right">avg cost after</div>
              <div className="col-span-2 text-right">realized p&l</div>
            </div>
            {summary.records.map((r) => (
              <div key={r.order_id} className="grid grid-cols-12 items-baseline gap-3 border-b border-ink-500/20 px-4 py-2.5 hover:bg-ink-800/30">
                <div className="col-span-2 tabular text-[11px] text-bone-400">{relativeTime(r.timestamp)}</div>
                <div className="col-span-1">
                  <span className={clsx("inline-flex items-center justify-center border px-1.5 py-0.5 text-[9px] uppercase tracking-[0.18em]",
                    r.action === "BUY" ? "border-crt-long/60 bg-crt-long/10 text-crt-long" : "border-crt-short/60 bg-crt-short/10 text-crt-short"
                  )}>
                    {r.action}
                  </span>
                </div>
                <div className="col-span-1 font-editorial text-base italic text-bone-100">{r.ticker}</div>
                <div className="col-span-1 tabular text-[10px] uppercase tracking-[0.18em] text-bone-500">{r.kind}</div>
                <div className="col-span-2 text-right">
                  <div className="tabular text-sm text-bone-50">{r.fill_price != null ? `$${r.fill_price.toFixed(4)}` : "—"}</div>
                  {r.signal_price != null && <div className="tabular text-[10px] text-bone-500">signal ${r.signal_price.toFixed(2)}</div>}
                </div>
                <div className="col-span-1 text-right tabular text-[11px] text-bone-400">{r.fill_qty != null ? r.fill_qty.toFixed(4) : "—"}</div>
                <div className="col-span-2 text-right tabular text-[11px] text-bone-400">{r.avg_cost_after != null ? `$${r.avg_cost_after.toFixed(4)}` : "—"}</div>
                <div className="col-span-2 text-right">
                  {r.realized_pnl != null ? (
                    <div>
                      <div className={clsx("tabular text-sm font-medium", r.realized_pnl >= 0 ? "text-crt-long" : "text-crt-short")}>
                        {r.realized_pnl >= 0 ? "+" : ""}${r.realized_pnl.toFixed(2)}
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
            ))}
          </div>
        )}
      </Section>
    </>
  );
}

// ---------------------------------------------------------------------------
// Virtual Book tab
// ---------------------------------------------------------------------------

function BookTab({ book, orders, loading, error }: {
  book: VirtualBook | null;
  orders: ProposedOrder[];
  loading: boolean;
  error: string | null;
}) {
  const positions = useMemo<VirtualPosition[]>(() => {
    if (!book?.positions) return [];
    return Object.values(book.positions).sort((a, b) => b.deployed_usd - a.deployed_usd);
  }, [book]);

  return (
    <>
      {error && <ErrBanner msg={error} />}
      {loading && <LoadingBanner />}

      {book?.present && (
        <Section title="Virtual Holdings" subtitle="paper-trade book — for signal sizing reference">
          {positions.length === 0 ? (
            <EmptyRow text="No virtual positions." />
          ) : (
            <div className="flex flex-col">
              <div className="grid grid-cols-12 gap-3 border-b border-ink-500/30 bg-ink-900/40 px-4 py-2 text-[9px] uppercase tracking-[0.32em] text-bone-500">
                <div className="col-span-3">ticker</div>
                <div className="col-span-2 text-right">avg cost</div>
                <div className="col-span-2 text-right">shares</div>
                <div className="col-span-2 text-right">deployed</div>
                <div className="col-span-2 text-right">budget %</div>
                <div className="col-span-1 text-right">held</div>
              </div>
              {positions.map((p) => {
                const pct = p.budget_usd > 0 ? (p.deployed_usd / p.budget_usd) * 100 : 0;
                return (
                  <div key={p.ticker} className="grid grid-cols-12 items-center gap-3 border-b border-ink-500/20 px-4 py-3 hover:bg-ink-800/30">
                    <div className="col-span-3 flex items-baseline gap-2">
                      <span className="font-editorial text-xl italic text-bone-50">{p.ticker}</span>
                      {p.last_signal_size && <span className="tabular text-[9px] uppercase text-bone-500">sz {p.last_signal_size}</span>}
                    </div>
                    <div className="col-span-2 text-right tabular text-sm text-bone-200">{p.avg_price != null ? `$${fmtPrice(p.avg_price)}` : "—"}</div>
                    <div className="col-span-2 text-right tabular text-sm text-bone-300">{p.shares.toFixed(4)}</div>
                    <div className="col-span-2 text-right tabular text-sm text-bone-50">${p.deployed_usd.toFixed(2)}</div>
                    <div className="col-span-2 text-right">
                      <div className="ml-auto h-1.5 w-full max-w-[100px] overflow-hidden border border-ink-500/40 bg-ink-950">
                        <div className="h-full bg-crt-amber" style={{ width: `${Math.min(100, pct)}%` }} />
                      </div>
                      <div className="mt-0.5 text-right tabular text-[10px] text-bone-500">{pct.toFixed(0)}%</div>
                    </div>
                    <div className="col-span-1 text-right tabular text-[10px] uppercase text-bone-500">{relativeTime(p.first_entry_at)}</div>
                  </div>
                );
              })}
            </div>
          )}
        </Section>
      )}

      <Section title="Decisions" subtitle="every executor action (last 100)">
        {orders.length === 0 ? (
          <EmptyRow text="No decisions yet." />
        ) : (
          <div className="flex flex-col">
            {orders.slice(0, 100).map((o) => (
              <div key={o.id} className="grid grid-cols-12 items-baseline gap-3 border-b border-ink-500/20 px-4 py-2.5 hover:bg-ink-800/30">
                <div className="col-span-2 tabular text-[11px] text-bone-400">{relativeTime(o.decided_at)}</div>
                <div className="col-span-1">
                  <span className={clsx("inline-flex items-center justify-center border px-1.5 py-0.5 text-[9px] uppercase tracking-[0.18em]",
                    o.action === "BUY" ? "border-crt-long/60 bg-crt-long/10 text-crt-long"
                    : o.action === "SELL" ? "border-crt-short/60 bg-crt-short/10 text-crt-short"
                    : "border-ink-500/60 text-bone-500"
                  )}>
                    {o.action}
                  </span>
                </div>
                <div className="col-span-1 font-editorial text-base italic text-bone-100">{o.ticker}</div>
                <div className="col-span-1 tabular text-[10px] uppercase text-bone-500">{o.signal_kind}</div>
                <div className="col-span-2 text-right tabular text-sm text-bone-50">{o.usd_amount != null ? `$${o.usd_amount.toFixed(2)}` : "—"}</div>
                <div className="col-span-5 truncate text-[11px] text-bone-300" title={o.rationale}>{o.rationale}</div>
              </div>
            ))}
          </div>
        )}
      </Section>
    </>
  );
}

// ---------------------------------------------------------------------------
// Shared primitives
// ---------------------------------------------------------------------------

function Section({ title, subtitle, trailing, children }: {
  title: string; subtitle?: string; trailing?: React.ReactNode; children: React.ReactNode;
}) {
  return (
    <section className="mb-6 border border-ink-500/40 bg-ink-900/20">
      <header className="flex flex-wrap items-baseline justify-between gap-3 border-b border-ink-500/30 px-4 py-3">
        <div>
          <h3 className="font-editorial text-xl italic text-bone-100">{title}</h3>
          {subtitle && <div className="mt-0.5 text-[10px] uppercase tracking-[0.32em] text-bone-500">{subtitle}</div>}
        </div>
        {trailing}
      </header>
      {children}
    </section>
  );
}

function StatTile({ label, value, valueClass, caption }: { label: string; value: string; valueClass?: string; caption?: string }) {
  return (
    <div className="border border-ink-500/40 bg-ink-900/40 px-4 py-3">
      <div className="text-[9px] uppercase tracking-[0.32em] text-bone-500">{label}</div>
      <div className={clsx("mt-1 font-mono text-2xl", valueClass ?? "text-bone-50")}>{value}</div>
      {caption && <div className="mt-0.5 text-[10px] uppercase tracking-[0.18em] text-bone-500">{caption}</div>}
    </div>
  );
}

function ErrBanner({ msg }: { msg: string }) {
  return <div className="mb-4 border border-crt-short/60 bg-crt-short/10 px-4 py-3 text-sm text-crt-short">ERR — {msg}</div>;
}

function LoadingBanner() {
  return <div className="mb-4 border border-ink-500/40 px-4 py-3 text-[11px] uppercase tracking-[0.32em] text-bone-400">loading…</div>;
}

function EmptyRow({ text }: { text: string }) {
  return <div className="px-6 py-10 text-center font-editorial text-lg italic text-bone-400">{text}</div>;
}
