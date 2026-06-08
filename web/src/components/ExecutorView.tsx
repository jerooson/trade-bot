import { useMemo, useState } from "react";
import clsx from "clsx";
import type { DecisionAction, ProposedOrder, VirtualBook, VirtualPosition } from "../lib/types";
import { fmtPrice, relativeTime } from "../lib/format";

type ActionFilter = "all" | "BUY" | "SELL" | "REJECT";

interface Props {
  book: VirtualBook | null;
  orders: ProposedOrder[];
  loading: boolean;
  error: string | null;
}

/**
 * Executor page — Robinhood-style layout.
 *
 * Top: big "deployed" hero number with mode badge.
 * Stats row: open count, available budget, decisions today.
 * Holdings card: a row per open position (Robinhood "Investing" feel).
 * Decisions feed: BUY/SELL/REJECT rows with filter pills.
 *
 * Stays read-only — the executor is the source of truth; this page just
 * surfaces what it's doing.
 */
export function ExecutorView({ book, orders, loading, error }: Props) {
  const [actionFilter, setActionFilter] = useState<ActionFilter>("all");

  const positions = useMemo<VirtualPosition[]>(() => {
    if (!book?.positions) return [];
    return Object.values(book.positions).sort(
      (a, b) => b.deployed_usd - a.deployed_usd,
    );
  }, [book]);

  const filteredOrders = useMemo(() => {
    if (actionFilter === "all") return orders;
    return orders.filter((o) => o.action === actionFilter);
  }, [orders, actionFilter]);

  // Today's decision count (UTC date match — matches the executor's stamps).
  const todayCount = useMemo(() => {
    const today = new Date().toISOString().slice(0, 10);
    return orders.filter((o) => (o.decided_at ?? "").slice(0, 10) === today).length;
  }, [orders]);

  const buyCount = orders.filter((o) => o.action === "BUY").length;
  const sellCount = orders.filter((o) => o.action === "SELL").length;
  const rejectCount = orders.filter((o) => o.action === "REJECT").length;

  const mode = book?.mode ?? "DRY_RUN";
  const summary = book?.summary;

  return (
    <main className="relative z-10 mx-auto max-w-[1400px] px-6 pb-16 pt-8">
      {/* Page header */}
      <div className="mb-6">
        <div className="flex items-end justify-between border-b border-ink-500/40 pb-4">
          <div>
            <div className="text-[10px] uppercase tracking-[0.32em] text-bone-500">
              section · 03 · executor
            </div>
            <h2 className="mt-1 font-editorial text-5xl italic leading-none text-bone-50">
              auto-trader,&nbsp;
              <span className="text-crt-amber">paper</span>
              <span className="text-bone-300">.</span>
            </h2>
          </div>
          <div className="hidden text-right md:block">
            <div className="text-[10px] uppercase tracking-[0.32em] text-bone-500">
              decisions on record
            </div>
            <div className="mt-1 tabular text-sm text-bone-300">
              {orders.length.toLocaleString()} total · {todayCount} today
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
          loading executor state…
        </div>
      )}

      {!loading && book && !book.present && (
        <ExecutorOffline reason={book.reason} />
      )}

      {!loading && book?.present && (
        <>
          {/* Hero: Robinhood-style big account number */}
          <Hero
            deployedUsd={summary?.total_deployed_usd ?? 0}
            accountBudgetUsd={summary?.account_budget_usd ?? 0}
            mode={mode}
            startedAt={book.started_at}
          />

          {/* Stats strip */}
          <div className="mb-6 grid grid-cols-2 gap-3 md:grid-cols-4">
            <StatTile
              label="Holdings"
              value={`${summary?.open_tickers ?? 0} / ${summary?.max_tickers ?? 0}`}
              caption="open tickers"
            />
            <StatTile
              label="Available"
              value={`$${(summary?.available_usd ?? 0).toFixed(2)}`}
              caption="of budget"
            />
            <StatTile
              label="Buys"
              value={buyCount.toString()}
              caption={`sells: ${sellCount} · rejected: ${rejectCount}`}
            />
            <StatTile
              label="Today"
              value={todayCount.toString()}
              caption="decisions"
            />
          </div>

          {/* Holdings */}
          <Section title="Holdings" subtitle="positions the executor would currently hold">
            {positions.length === 0 ? (
              <EmptyRow text="No open positions. Waiting for the next ENTRY signal." />
            ) : (
              <div className="flex flex-col">
                <HoldingsHeader />
                {positions.map((p) => (
                  <HoldingRow key={p.ticker} pos={p} />
                ))}
              </div>
            )}
          </Section>

          {/* Decisions feed */}
          <Section
            title="Decisions"
            subtitle="every action the executor took (or refused to take)"
            trailing={
              <div className="flex items-center gap-2">
                {(["all", "BUY", "SELL", "REJECT"] as ActionFilter[]).map((f) => (
                  <FilterPill
                    key={f}
                    active={actionFilter === f}
                    onClick={() => setActionFilter(f)}
                  >
                    {f.toLowerCase()}
                  </FilterPill>
                ))}
              </div>
            }
          >
            {filteredOrders.length === 0 ? (
              <EmptyRow
                text={
                  orders.length === 0
                    ? "No decisions yet. They'll appear here when Will posts a new swing action."
                    : `No decisions match "${actionFilter}".`
                }
              />
            ) : (
              <div className="flex flex-col">
                <DecisionsHeader />
                {filteredOrders.slice(0, 200).map((o) => (
                  <DecisionRow key={o.id} order={o} />
                ))}
                {filteredOrders.length > 200 && (
                  <div className="px-4 py-3 text-center text-[10px] uppercase tracking-[0.32em] text-bone-500">
                    showing newest 200 of {filteredOrders.length.toLocaleString()}
                  </div>
                )}
              </div>
            )}
          </Section>
        </>
      )}
    </main>
  );
}

// -- Sub-components ----------------------------------------------------------

function Hero({
  deployedUsd,
  accountBudgetUsd,
  mode,
  startedAt,
}: {
  deployedUsd: number;
  accountBudgetUsd: number;
  mode: string;
  startedAt: string | undefined;
}) {
  const pct = accountBudgetUsd > 0 ? (deployedUsd / accountBudgetUsd) * 100 : 0;

  return (
    <div className="mb-6 border border-ink-500/40 bg-ink-900/40 px-6 py-6">
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <div className="flex items-center gap-2 text-[10px] uppercase tracking-[0.32em] text-bone-500">
            <span>virtual deployed</span>
            <ModeBadge mode={mode} />
          </div>
          <div className="mt-2 flex items-baseline gap-3">
            <span className="font-mono text-5xl font-light tracking-tight text-bone-50">
              ${deployedUsd.toFixed(2)}
            </span>
            <span className="tabular text-sm text-bone-400">
              of ${accountBudgetUsd.toFixed(2)} cap
            </span>
          </div>
          <div className="mt-1 text-[11px] text-bone-500">
            {pct.toFixed(1)}% of budget allocated · started {relativeTime(startedAt)}
          </div>
        </div>

        {/* Allocation bar */}
        <div className="hidden w-56 md:block">
          <div className="text-right text-[9px] uppercase tracking-[0.32em] text-bone-500">
            allocation
          </div>
          <div className="mt-2 h-2 w-full overflow-hidden border border-ink-500/40 bg-ink-950">
            <div
              className="h-full bg-crt-amber"
              style={{ width: `${Math.min(100, Math.max(0, pct))}%` }}
            />
          </div>
          <div className="mt-1 flex justify-between text-[9px] uppercase tracking-[0.32em] text-bone-500">
            <span>0%</span>
            <span>100%</span>
          </div>
        </div>
      </div>
    </div>
  );
}

function ModeBadge({ mode }: { mode: string }) {
  const isLive = mode.toUpperCase() === "LIVE";
  return (
    <span
      className={clsx(
        "inline-flex items-center gap-1 border px-1.5 py-0.5 text-[9px] uppercase tracking-[0.18em]",
        isLive
          ? "border-crt-short/60 bg-crt-short/10 text-crt-short"
          : "border-crt-amber/60 bg-crt-amber/10 text-crt-amber",
      )}
    >
      <span
        className={clsx(
          "h-1.5 w-1.5 rounded-full",
          isLive ? "animate-pulseDot bg-crt-short" : "bg-crt-amber",
        )}
      />
      {mode}
    </span>
  );
}

function StatTile({
  label,
  value,
  caption,
}: {
  label: string;
  value: string;
  caption?: string;
}) {
  return (
    <div className="border border-ink-500/40 bg-ink-900/40 px-4 py-3">
      <div className="text-[9px] uppercase tracking-[0.32em] text-bone-500">
        {label}
      </div>
      <div className="mt-1 font-mono text-2xl text-bone-50">{value}</div>
      {caption && (
        <div className="mt-0.5 text-[10px] uppercase tracking-[0.18em] text-bone-500">
          {caption}
        </div>
      )}
    </div>
  );
}

function Section({
  title,
  subtitle,
  trailing,
  children,
}: {
  title: string;
  subtitle?: string;
  trailing?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <section className="mb-6 border border-ink-500/40 bg-ink-900/20">
      <header className="flex flex-wrap items-baseline justify-between gap-3 border-b border-ink-500/30 px-4 py-3">
        <div>
          <h3 className="font-editorial text-xl italic text-bone-100">{title}</h3>
          {subtitle && (
            <div className="mt-0.5 text-[10px] uppercase tracking-[0.32em] text-bone-500">
              {subtitle}
            </div>
          )}
        </div>
        {trailing}
      </header>
      {children}
    </section>
  );
}

function HoldingsHeader() {
  return (
    <div className="grid grid-cols-12 gap-3 border-b border-ink-500/30 bg-ink-900/40 px-4 py-2 text-[9px] uppercase tracking-[0.32em] text-bone-500">
      <div className="col-span-3">ticker</div>
      <div className="col-span-2 text-right">avg cost</div>
      <div className="col-span-2 text-right">shares</div>
      <div className="col-span-2 text-right">deployed</div>
      <div className="col-span-2 text-right">budget</div>
      <div className="col-span-1 text-right">held</div>
    </div>
  );
}

function HoldingRow({ pos }: { pos: VirtualPosition }) {
  const fillPct = pos.budget_usd > 0 ? (pos.deployed_usd / pos.budget_usd) * 100 : 0;
  return (
    <div className="grid grid-cols-12 items-center gap-3 border-b border-ink-500/20 px-4 py-3 hover:bg-ink-800/30">
      <div className="col-span-3 flex items-baseline gap-2 min-w-0">
        <span className="font-editorial text-xl italic text-bone-50">{pos.ticker}</span>
        <span className="tabular text-[9px] uppercase tracking-[0.18em] text-crt-long">
          {pos.side}
        </span>
        {pos.last_signal_size && (
          <span className="tabular text-[9px] uppercase tracking-[0.18em] text-bone-500">
            sz {pos.last_signal_size}
          </span>
        )}
      </div>
      <div className="col-span-2 text-right tabular text-sm text-bone-200">
        {pos.avg_price != null ? `$${fmtPrice(pos.avg_price)}` : "—"}
      </div>
      <div className="col-span-2 text-right tabular text-sm text-bone-300">
        {pos.shares.toFixed(4)}
      </div>
      <div className="col-span-2 text-right tabular text-sm text-bone-50">
        ${pos.deployed_usd.toFixed(2)}
      </div>
      <div className="col-span-2 text-right">
        <div className="ml-auto h-1.5 w-full max-w-[120px] overflow-hidden border border-ink-500/40 bg-ink-950">
          <div
            className="h-full bg-crt-amber"
            style={{ width: `${Math.min(100, Math.max(0, fillPct))}%` }}
          />
        </div>
        <div className="mt-0.5 text-right tabular text-[10px] text-bone-500">
          {fillPct.toFixed(0)}%
        </div>
      </div>
      <div className="col-span-1 text-right tabular text-[10px] uppercase tracking-[0.18em] text-bone-500">
        {relativeTime(pos.first_entry_at)}
      </div>
    </div>
  );
}

function DecisionsHeader() {
  return (
    <div className="grid grid-cols-12 gap-3 border-b border-ink-500/30 bg-ink-900/40 px-4 py-2 text-[9px] uppercase tracking-[0.32em] text-bone-500">
      <div className="col-span-2">when</div>
      <div className="col-span-1">action</div>
      <div className="col-span-1">ticker</div>
      <div className="col-span-1">signal</div>
      <div className="col-span-2 text-right">amount</div>
      <div className="col-span-5">rationale</div>
    </div>
  );
}

function DecisionRow({ order }: { order: ProposedOrder }) {
  return (
    <div className="grid grid-cols-12 items-baseline gap-3 border-b border-ink-500/20 px-4 py-2.5 hover:bg-ink-800/30">
      <div className="col-span-2 tabular text-[11px] text-bone-400">
        {relativeTime(order.decided_at)}
      </div>
      <div className="col-span-1">
        <ActionBadge action={order.action} />
      </div>
      <div className="col-span-1">
        <span className="font-editorial text-base italic text-bone-100">
          {order.ticker}
        </span>
      </div>
      <div className="col-span-1 tabular text-[10px] uppercase tracking-[0.18em] text-bone-500">
        {order.signal_kind}
      </div>
      <div className="col-span-2 text-right">
        {order.action === "REJECT" ? (
          <span className="tabular text-sm text-bone-500">—</span>
        ) : (
          <>
            <div className="tabular text-sm text-bone-50">
              ${(order.usd_amount ?? 0).toFixed(2)}
            </div>
            {order.shares_estimate != null && (
              <div className="tabular text-[10px] text-bone-500">
                ≈ {order.shares_estimate.toFixed(4)} sh
                {order.signal_price != null
                  ? ` @ $${fmtPrice(order.signal_price)}`
                  : ""}
              </div>
            )}
          </>
        )}
      </div>
      <div className="col-span-5 truncate text-[11px] text-bone-300" title={order.rationale}>
        {order.rationale}
      </div>
    </div>
  );
}

function ActionBadge({ action }: { action: DecisionAction }) {
  const style =
    action === "BUY"
      ? "border-crt-long/60 bg-crt-long/10 text-crt-long"
      : action === "SELL"
        ? "border-crt-short/60 bg-crt-short/10 text-crt-short"
        : "border-ink-500/60 bg-ink-800/40 text-bone-500";
  return (
    <span
      className={clsx(
        "inline-flex items-center justify-center border px-1.5 py-0.5 text-[9px] uppercase tracking-[0.18em]",
        style,
      )}
    >
      {action}
    </span>
  );
}

function FilterPill({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      onClick={onClick}
      className={clsx(
        "inline-flex items-center border px-2.5 py-1 text-[10px] uppercase tracking-[0.18em] transition-colors",
        active
          ? "border-crt-amber bg-crt-amber/10 text-crt-amber"
          : "border-ink-500/60 text-bone-400 hover:border-bone-300 hover:text-bone-100",
      )}
    >
      {children}
    </button>
  );
}

function EmptyRow({ text }: { text: string }) {
  return (
    <div className="px-6 py-12 text-center">
      <div className="font-editorial text-lg italic text-bone-400">{text}</div>
    </div>
  );
}

function ExecutorOffline({ reason }: { reason?: string }) {
  return (
    <div className="mb-6 border border-dashed border-crt-amber/40 bg-crt-amber/[0.04] px-6 py-12 text-center">
      <div className="font-editorial text-3xl italic text-bone-100">
        executor offline
      </div>
      <div className="mt-2 text-[11px] uppercase tracking-[0.32em] text-bone-500">
        {reason ?? "no virtual_book.json found"}
      </div>
      <div className="mx-auto mt-6 max-w-md text-left">
        <pre className="border border-ink-500/40 bg-ink-950 px-3 py-2 text-[11px] text-bone-300">
{`# start the executor in a terminal
python -m bot.executor

# or restart the dashboard so it launches automatically
python -m bot.dashboard`}
        </pre>
      </div>
    </div>
  );
}
