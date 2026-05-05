import type { Stats } from "../lib/types";
import { SectionHeader } from "./SectionHeader";

interface Props {
  stats: Stats;
}

export function TopTickers({ stats }: Props) {
  const rows = stats.top_tickers.slice(0, 12);
  const max = rows.reduce((m, r) => Math.max(m, r.total), 1);

  return (
    <section>
      <SectionHeader
        index="02"
        label="ticker activity"
        hint="in current range · top movers"
        right={<span className="tabular">{rows.length} / {stats.top_tickers.length}</span>}
      />

      <div className="border-b border-ink-500/40">
        {rows.length === 0 ? (
          <div className="px-4 py-12 text-center text-[10px] uppercase tracking-[0.32em] text-bone-500">
            no tickers in range
          </div>
        ) : (
          rows.map((r, i) => (
            <Row key={r.ticker} rank={i + 1} ticker={r.ticker} total={r.total} max={max} trigger={r.trigger} plan={r.plan} profit={r.profit} />
          ))
        )}
      </div>
    </section>
  );
}

function Row({
  rank,
  ticker,
  total,
  max,
  trigger,
  plan,
  profit,
}: {
  rank: number;
  ticker: string;
  total: number;
  max: number;
  trigger: number;
  plan: number;
  profit: number;
}) {
  const triggerPct = (trigger / total) * 100;
  const planPct = (plan / total) * 100;
  const widthPct = (total / max) * 100;

  return (
    <div className="group relative grid grid-cols-[40px_72px_1fr_auto] items-center gap-4 px-4 py-2.5 transition-colors hover:bg-ink-800/40">
      <div className="text-[10px] tabular text-bone-500">#{String(rank).padStart(2, "0")}</div>
      <div className="text-sm font-bold tracking-tight text-bone-50">{ticker}</div>

      {/* Stacked bar */}
      <div className="relative h-5 overflow-hidden bg-ink-800/60">
        <div
          className="absolute inset-y-0 left-0 flex"
          style={{ width: `${widthPct}%` }}
        >
          {/* trigger | plan | profit  -- segments proportional to within ticker */}
          <div className="bg-crt-amber/85" style={{ width: `${triggerPct}%` }} title={`Trigger: ${trigger}`} />
          <div className="bg-crt-info/65" style={{ width: `${planPct}%` }} title={`Plan: ${plan}`} />
          <div className="bg-crt-long/40 grow" title={`Profit: ${profit}`} />
        </div>
        <div className="absolute inset-y-0 left-0 right-0 border border-ink-500/40" />
      </div>

      <div className="flex items-baseline gap-3 text-xs tabular">
        {trigger > 0 && (
          <span title="Trigger" className="text-crt-amber">
            {trigger}
          </span>
        )}
        {plan > 0 && (
          <span title="Plan" className="text-crt-info">
            {plan}
          </span>
        )}
        {profit > 0 && (
          <span title="Profit" className="text-crt-long/80">
            {profit}
          </span>
        )}
        <span className="ml-1 w-8 text-right text-bone-100">{total}</span>
      </div>
    </div>
  );
}
