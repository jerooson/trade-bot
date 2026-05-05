import { useMemo, useState } from "react";
import clsx from "clsx";
import type { Signal, SignalKind } from "../lib/types";
import { SignalRow } from "./SignalRow";
import { SectionHeader } from "./SectionHeader";

interface Props {
  signals: Signal[];
  totalAll: number;
}

const FILTERS: { id: SignalKind | "ALL"; label: string; tone: string }[] = [
  { id: "ALL", label: "all", tone: "text-bone-100" },
  { id: "TRIGGER", label: "trg", tone: "text-crt-amber" },
  { id: "PLAN", label: "pln", tone: "text-crt-info" },
  { id: "PROFIT", label: "pft", tone: "text-crt-long" },
];

export function SignalFeed({ signals, totalAll }: Props) {
  const [filter, setFilter] = useState<SignalKind | "ALL">("ALL");
  const [tickerFilter, setTickerFilter] = useState("");

  const filtered = useMemo(() => {
    let out = signals;
    if (filter !== "ALL") out = out.filter((s) => s.kind === filter);
    if (tickerFilter.trim()) {
      const t = tickerFilter.trim().toUpperCase();
      out = out.filter((s) => s.ticker.includes(t));
    }
    return out;
  }, [signals, filter, tickerFilter]);

  return (
    <section>
      <SectionHeader
        index="01"
        label="live feed"
        hint="newest first · refreshes on stream"
        right={
          <span className="tabular">
            {filtered.length.toString().padStart(4, "0")} /{" "}
            {signals.length.toString().padStart(4, "0")}{" "}
            <span className="text-bone-500">
              · {totalAll.toLocaleString()} total
            </span>
          </span>
        }
      />

      {/* Filter bar */}
      <div className="flex items-center gap-1 border-b border-ink-500/40 bg-ink-800/40 px-4 py-2.5">
        <span className="mr-3 text-[10px] uppercase tracking-[0.32em] text-bone-500">filter</span>
        {FILTERS.map((f) => (
          <button
            key={f.id}
            onClick={() => setFilter(f.id)}
            className={clsx(
              "border px-3 py-1 text-[11px] font-medium uppercase tracking-[0.18em] transition-colors",
              filter === f.id
                ? "border-crt-amber bg-crt-amber/10 text-crt-amber"
                : "border-ink-500/60 text-bone-400 hover:border-ink-400 hover:text-bone-100",
            )}
          >
            {f.label}
          </button>
        ))}
        <div className="ml-6 h-4 w-px bg-ink-500" />
        <input
          type="text"
          placeholder="ticker…"
          value={tickerFilter}
          onChange={(e) => setTickerFilter(e.target.value)}
          className="ml-4 w-36 border border-ink-500/60 bg-transparent px-3 py-1 text-[11px] uppercase tracking-[0.18em] text-bone-100 placeholder:text-bone-500 focus:border-crt-amber/60 focus:outline-none"
        />
      </div>

      {/* Column headers */}
      <div className="grid grid-cols-[78px_1fr_82px_minmax(0,1.4fr)_84px] items-center gap-3 border-b border-ink-500/40 bg-ink-900/40 px-4 py-2 text-[9px] uppercase tracking-[0.32em] text-bone-500">
        <div>kind</div>
        <div>ticker</div>
        <div>side</div>
        <div>prices</div>
        <div className="text-right">time</div>
      </div>

      {/* Rows */}
      <div className="max-h-[680px] overflow-y-auto">
        {filtered.length === 0 ? (
          <div className="flex flex-col items-center justify-center gap-2 py-20 text-center">
            <div className="font-editorial text-3xl italic text-bone-400">
              {signals.length === 0
                ? "no signals in this range"
                : "no signals match the kind / ticker filters"}
            </div>
            <div className="text-[10px] uppercase tracking-[0.32em] text-bone-500">
              {signals.length === 0
                ? "expand the date range above, or wait for new events"
                : "try clearing the kind or ticker filters"}
            </div>
          </div>
        ) : (
          filtered.map((sig, i) => (
            <SignalRow key={`${sig.discord?.message_id ?? i}-${sig.kind}`} signal={sig} />
          ))
        )}
      </div>
    </section>
  );
}
