import clsx from "clsx";
import { DATE_RANGE_OPTIONS, type DateRange, describeRange } from "../lib/filters";

interface Props {
  value: DateRange;
  onChange: (next: DateRange) => void;
  totalInRange: number;
  totalAll: number;
}

export function DateRangeBar({ value, onChange, totalInRange, totalAll }: Props) {
  return (
    <div className="relative border border-ink-500/60 bg-ink-900/60">
      {/* corner brackets for editorial flavor */}
      <span className="absolute left-2 top-2 h-2 w-2 border-l border-t border-crt-amber/60" />
      <span className="absolute right-2 top-2 h-2 w-2 border-r border-t border-crt-amber/60" />
      <span className="absolute bottom-2 left-2 h-2 w-2 border-b border-l border-crt-amber/60" />
      <span className="absolute bottom-2 right-2 h-2 w-2 border-b border-r border-crt-amber/60" />

      <div className="flex flex-col items-stretch gap-4 px-6 py-4 sm:flex-row sm:items-center">
        <div className="flex items-baseline gap-3">
          <span className="text-[10px] uppercase tracking-[0.32em] text-bone-500">range</span>
          <span className="font-editorial text-base italic text-bone-300">
            {describeRange(value)}
          </span>
        </div>

        <div className="flex flex-1 items-center justify-center">
          <div className="flex divide-x divide-ink-500/60 border border-ink-500/60">
            {DATE_RANGE_OPTIONS.map((opt) => {
              const active = opt.id === value;
              return (
                <button
                  key={opt.id}
                  onClick={() => onChange(opt.id)}
                  className={clsx(
                    "group relative px-5 py-2 text-[11px] uppercase tracking-[0.22em] transition-colors",
                    active
                      ? "bg-crt-amber/10 text-crt-amber"
                      : "text-bone-400 hover:bg-ink-800 hover:text-bone-100",
                  )}
                  title={opt.sub}
                >
                  <span className="font-bold">{opt.label}</span>
                  {active && (
                    <span className="absolute inset-x-0 -top-px h-px bg-crt-amber" />
                  )}
                </button>
              );
            })}
          </div>
        </div>

        <div className="flex items-baseline gap-3 text-right tabular sm:gap-2">
          <span className="text-[10px] uppercase tracking-[0.32em] text-bone-500">in view</span>
          <span className="text-2xl font-medium text-bone-50">
            {totalInRange.toLocaleString()}
          </span>
          <span className="text-bone-500">/</span>
          <span className="text-sm text-bone-400">{totalAll.toLocaleString()}</span>
        </div>
      </div>
    </div>
  );
}
