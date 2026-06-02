import clsx from "clsx";
import type { TradePlan } from "../lib/types";
import { fmtPrice, relativeTime } from "../lib/format";

interface Props {
  plan: TradePlan;
  isPinned: boolean;
  onTogglePin: () => void;
}

/**
 * One plan = one card.
 *
 * Layout:
 *   Header        ticker + posted-at + pin button
 *   Levels row    chips for each watch level (numeric)
 *   Narrative     full body, monospace, editorial-quoted style
 *   Glossary      collapsed inline list of definitions, if present
 *   Footer        chart link out + raw discord link metadata
 */
export function PlanCard({ plan, isPinned, onTogglePin }: Props) {
  const ticker = plan.ticker ?? "—";
  const posted = plan.discord?.created_at ?? plan.received_at;
  const hasLevels = plan.watch_levels.length > 0;
  const glossaryEntries = Object.entries(plan.glossary ?? {});

  return (
    <article
      className={clsx(
        "group relative border bg-ink-900/40 transition-colors",
        isPinned
          ? "border-crt-amber/60 bg-crt-amber/[0.04]"
          : "border-ink-500/40 hover:border-ink-500/80",
      )}
    >
      {/* corner brackets, editorial flourish */}
      <span className="pointer-events-none absolute -left-px -top-px h-3 w-3 border-l border-t border-crt-amber/60" />
      <span className="pointer-events-none absolute -right-px -top-px h-3 w-3 border-r border-t border-crt-amber/60" />
      <span className="pointer-events-none absolute -bottom-px -left-px h-3 w-3 border-b border-l border-crt-amber/60" />
      <span className="pointer-events-none absolute -bottom-px -right-px h-3 w-3 border-b border-r border-crt-amber/60" />

      <header className="flex items-start justify-between gap-4 border-b border-ink-500/40 px-5 py-4">
        <div className="min-w-0">
          <div className="flex items-baseline gap-3">
            <h3 className="font-editorial text-3xl italic leading-none text-bone-50">
              {ticker}
            </h3>
            {hasLevels && (
              <span className="text-[10px] uppercase tracking-[0.32em] text-bone-500">
                watch
              </span>
            )}
            {hasLevels && (
              <div className="flex flex-wrap items-baseline gap-1.5">
                {plan.watch_levels.slice(0, 6).map((lvl, i) => (
                  <span
                    key={`${lvl}-${i}`}
                    className="tabular border border-crt-amber/30 bg-crt-amber/5 px-2 py-0.5 text-[12px] text-crt-amber"
                  >
                    ${fmtPrice(lvl)}
                  </span>
                ))}
                {plan.watch_levels.length > 6 && (
                  <span className="tabular text-[11px] text-bone-500">
                    +{plan.watch_levels.length - 6} more
                  </span>
                )}
              </div>
            )}
          </div>
          <div className="mt-1.5 flex items-baseline gap-3 text-[10px] uppercase tracking-[0.18em] text-bone-500">
            <span>posted {relativeTime(posted)}</span>
            <span>·</span>
            <span className="tabular">
              {new Date(posted).toLocaleString(undefined, {
                month: "short",
                day: "2-digit",
                hour: "2-digit",
                minute: "2-digit",
              })}
            </span>
          </div>
        </div>

        <button
          onClick={onTogglePin}
          className={clsx(
            "shrink-0 border px-3 py-1.5 text-[10px] uppercase tracking-[0.32em] transition-colors",
            isPinned
              ? "border-crt-amber bg-crt-amber/10 text-crt-amber"
              : "border-ink-500/60 text-bone-400 hover:border-bone-300 hover:text-bone-100",
          )}
          aria-pressed={isPinned}
        >
          {isPinned ? "pinned" : "pin"}
        </button>
      </header>

      <div className="px-5 py-4">
        <div className="text-[10px] uppercase tracking-[0.32em] text-bone-500">
          rationale
        </div>
        <p className="mt-2 whitespace-pre-wrap leading-relaxed text-bone-200">
          {plan.narrative.trim() || "—"}
        </p>
      </div>

      {glossaryEntries.length > 0 && (
        <div className="border-t border-ink-500/40 px-5 py-3">
          <div className="text-[10px] uppercase tracking-[0.32em] text-bone-500">
            glossary
          </div>
          <dl className="mt-2 grid grid-cols-1 gap-x-6 gap-y-1 text-[12px] sm:grid-cols-2">
            {glossaryEntries.map(([term, def]) => (
              <div key={term} className="flex items-baseline gap-2">
                <dt className="shrink-0 tabular text-crt-long/80">{term}</dt>
                <dd className="text-bone-300">{def}</dd>
              </div>
            ))}
          </dl>
        </div>
      )}

      <footer className="flex flex-wrap items-center justify-between gap-3 border-t border-ink-500/40 px-5 py-3">
        <div className="text-[10px] uppercase tracking-[0.18em] text-bone-500">
          {plan.discord?.author_name ?? "—"}
        </div>
        {plan.chart_url && (
          <a
            href={plan.chart_url}
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-2 border border-ink-500/60 px-3 py-1.5 text-[10px] uppercase tracking-[0.32em] text-bone-300 hover:border-bone-300 hover:text-bone-50"
          >
            chart →
          </a>
        )}
      </footer>
    </article>
  );
}
