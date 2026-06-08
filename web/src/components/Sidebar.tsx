import clsx from "clsx";

export type ViewId = "signals" | "watchlist" | "swings" | "executor";

interface NavItem {
  id: ViewId;
  num: string;
  label: string;
  sublabel: string;
  badge?: number;
  highlight?: boolean;
}

interface Props {
  current: ViewId;
  onChange: (id: ViewId) => void;
  signalCount: number;
  planCount: number;
  swingCount: number;
  openPositionsCount: number;
  pinnedCount: number;
  executorOpenCount: number;
  executorDecisionsCount: number;
}

export function Sidebar({
  current,
  onChange,
  signalCount,
  planCount,
  swingCount,
  openPositionsCount,
  pinnedCount,
  executorOpenCount,
  executorDecisionsCount,
}: Props) {
  const items: NavItem[] = [
    { id: "signals",   num: "00", label: "signal terminal", sublabel: "live PLAN/TRIGGER/PROFIT", badge: signalCount },
    { id: "watchlist", num: "01", label: "watchlist",       sublabel: "swing-trade plans",        badge: planCount },
    { id: "swings",    num: "02", label: "execution",       sublabel: "live entries / exits",     badge: swingCount, highlight: true },
    { id: "executor",  num: "03", label: "executor",        sublabel: "auto-trader · dry run",    badge: executorOpenCount, highlight: executorOpenCount > 0 },
  ];

  return (
    <aside className="sticky top-0 hidden h-screen w-[220px] shrink-0 flex-col border-r border-ink-500/40 bg-ink-900/50 px-4 py-6 md:flex">
      <div className="text-[10px] uppercase tracking-[0.32em] text-bone-500">
        section index
      </div>
      <div className="mt-1 h-px bg-gradient-to-r from-ink-500/60 to-transparent" />

      <nav className="mt-4 flex flex-col gap-1">
        {items.map((it) => {
          const active = current === it.id;
          return (
            <button
              key={it.id}
              onClick={() => onChange(it.id)}
              className={clsx(
                "group relative flex flex-col gap-0.5 border-l-2 px-3 py-2.5 text-left transition-colors",
                active
                  ? "border-crt-amber bg-ink-800/60"
                  : "border-transparent hover:border-bone-500/40 hover:bg-ink-800/30",
              )}
            >
              <div className="flex items-baseline justify-between gap-2">
                <div className="flex items-baseline gap-2">
                  <span
                    className={clsx(
                      "tabular text-[11px]",
                      active ? "text-crt-amber" : "text-bone-500",
                    )}
                  >
                    {it.num}
                  </span>
                  <span
                    className={clsx(
                      "text-sm uppercase tracking-[0.18em]",
                      active ? "text-bone-50" : "text-bone-200 group-hover:text-bone-50",
                    )}
                  >
                    {it.label}
                  </span>
                  {it.highlight && (
                    <span
                      className="h-1.5 w-1.5 animate-pulseDot rounded-full bg-crt-long"
                      aria-label="live"
                    />
                  )}
                </div>
                {it.badge != null && (
                  <span
                    className={clsx(
                      "tabular text-[10px]",
                      active ? "text-bone-300" : "text-bone-500",
                    )}
                  >
                    {it.badge.toLocaleString()}
                  </span>
                )}
              </div>
              <div className="text-[10px] tracking-[0.05em] text-bone-500">
                {it.sublabel}
              </div>
            </button>
          );
        })}
      </nav>

      <div className="mt-auto pt-6 text-[10px] uppercase tracking-[0.32em] text-bone-500">
        instrument state
      </div>
      <div className="mt-1 h-px bg-gradient-to-r from-ink-500/60 to-transparent" />
      <div className="mt-3 flex flex-col gap-2 text-[11px] text-bone-400">
        <Row label="signals" value={signalCount.toLocaleString()} />
        <Row label="plans" value={planCount.toLocaleString()} />
        <Row label="actions" value={swingCount.toLocaleString()} />
        <Row label="open" value={openPositionsCount.toLocaleString()} highlight={openPositionsCount > 0} />
        <Row label="pinned" value={pinnedCount.toLocaleString()} highlight={pinnedCount > 0} />
        <Row label="decisions" value={executorDecisionsCount.toLocaleString()} highlight={executorDecisionsCount > 0} />
      </div>
    </aside>
  );
}

function Row({ label, value, highlight }: { label: string; value: string; highlight?: boolean }) {
  return (
    <div className="flex items-baseline justify-between">
      <span className="uppercase tracking-[0.18em] text-bone-500">{label}</span>
      <span className={clsx("tabular", highlight ? "text-crt-amber" : "text-bone-200")}>
        {value}
      </span>
    </div>
  );
}
