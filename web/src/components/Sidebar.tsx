import clsx from "clsx";
import { Activity, Bot, ListTodo, Radio, type LucideIcon } from "lucide-react";

export type ViewId = "signals" | "watchlist" | "swings" | "executor";

interface NavItem {
  id: ViewId;
  label: string;
  sublabel: string;
  badge?: number;
  highlight?: boolean;
  icon: LucideIcon;
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
    { id: "signals", label: "Signals", sublabel: "Discord signal feed", badge: signalCount, icon: Radio },
    { id: "watchlist", label: "Watchlist", sublabel: "Trade plans", badge: planCount, icon: ListTodo },
    { id: "swings", label: "Positions", sublabel: "Entries and exits", badge: openPositionsCount, highlight: openPositionsCount > 0, icon: Activity },
    { id: "executor", label: "Executor", sublabel: "Paper trader", badge: executorOpenCount, highlight: executorOpenCount > 0, icon: Bot },
  ];

  return (
    <>
      <aside className="sticky top-[65px] hidden h-[calc(100vh-65px)] w-[252px] shrink-0 flex-col border-r border-ink-500/30 bg-ink-900/45 px-4 py-6 backdrop-blur-xl md:flex">
        <div className="px-3 text-[10px] font-semibold uppercase tracking-[0.2em] text-bone-500">
          Workspace
        </div>

        <nav className="mt-3 flex flex-col gap-1.5">
          {items.map((item) => {
            const active = current === item.id;
            const Icon = item.icon;
            return (
              <button
                key={item.id}
                onClick={() => onChange(item.id)}
                className={clsx(
                  "group flex gap-3 border px-3 py-3 text-left transition-all",
                  active
                    ? "border-crt-info/30 bg-crt-info/10 shadow-glow"
                    : "border-transparent hover:border-ink-500/60 hover:bg-ink-800/60",
                )}
              >
                <div className={clsx("mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-lg", active ? "bg-crt-info/15 text-crt-info" : "bg-ink-800 text-bone-400")}>
                  <Icon className="h-4 w-4" />
                </div>
                <div className="min-w-0 flex-1">
                  <div className="flex items-center justify-between gap-2">
                    <span className={clsx("text-sm font-semibold", active ? "text-bone-50" : "text-bone-200 group-hover:text-bone-50")}>
                      {item.label}
                    </span>
                    <span className={clsx("tabular rounded-full px-1.5 py-0.5 text-[10px]", active ? "bg-crt-info/15 text-crt-info" : "bg-ink-800 text-bone-500")}>
                      {(item.badge ?? 0).toLocaleString()}
                    </span>
                  </div>
                  <div className="mt-0.5 flex items-center gap-2 text-[11px] text-bone-500">
                    {item.sublabel}
                    {item.highlight && <span className="h-1.5 w-1.5 animate-pulseDot rounded-full bg-crt-long" />}
                  </div>
                </div>
              </button>
            );
          })}
        </nav>

        <div className="mt-auto px-3 pt-6 text-[10px] font-semibold uppercase tracking-[0.2em] text-bone-500">
          System summary
        </div>
        <div className="mt-3 flex flex-col gap-2.5 rounded-xl border border-ink-500/30 bg-ink-950/40 p-3 text-[11px] text-bone-400">
          <Row label="signals" value={signalCount.toLocaleString()} />
          <Row label="plans" value={planCount.toLocaleString()} />
          <Row label="actions" value={swingCount.toLocaleString()} />
          <Row label="open positions" value={openPositionsCount.toLocaleString()} highlight={openPositionsCount > 0} />
          <Row label="pinned" value={pinnedCount.toLocaleString()} highlight={pinnedCount > 0} />
          <Row label="decisions" value={executorDecisionsCount.toLocaleString()} highlight={executorDecisionsCount > 0} />
        </div>
      </aside>

      <nav className="fixed inset-x-3 bottom-3 z-50 grid grid-cols-4 gap-1 rounded-2xl border border-ink-500/50 bg-ink-900/90 p-1.5 shadow-2xl backdrop-blur-xl md:hidden">
        {items.map((item) => {
          const Icon = item.icon;
          const active = current === item.id;
          return (
            <button key={item.id} onClick={() => onChange(item.id)} className={clsx("flex flex-col items-center gap-1 px-2 py-2 text-[10px] font-medium", active ? "bg-crt-info/15 text-crt-info" : "text-bone-400")}>
              <Icon className="h-4 w-4" />
              {item.label}
            </button>
          );
        })}
      </nav>
    </>
  );
}

function Row({ label, value, highlight }: { label: string; value: string; highlight?: boolean }) {
  return (
    <div className="flex items-baseline justify-between">
      <span className="text-bone-500">{label}</span>
      <span className={clsx("tabular font-medium", highlight ? "text-crt-info" : "text-bone-200")}>{value}</span>
    </div>
  );
}
