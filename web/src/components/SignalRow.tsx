import clsx from "clsx";
import { ArrowDownRight, ArrowUpRight, ExternalLink, Target, Zap } from "lucide-react";
import type { Signal } from "../lib/types";
import { fmtPrice, fmtTime, relativeTime } from "../lib/format";

interface Props {
  signal: Signal;
}

const KIND_META = {
  TRIGGER: {
    label: "TRG",
    accent: "text-crt-amber",
    border: "border-l-crt-amber",
    icon: Zap,
  },
  PLAN: {
    label: "PLN",
    accent: "text-crt-info",
    border: "border-l-crt-info/70",
    icon: Target,
  },
  PROFIT: {
    label: "PFT",
    accent: "text-crt-long",
    border: "border-l-crt-long/70",
    icon: ArrowUpRight,
  },
} as const;

export function SignalRow({ signal }: Props) {
  const meta = KIND_META[signal.kind];
  const Icon = meta.icon;
  const isLong = signal.side === "LONG";
  const created = signal.discord?.created_at ?? signal.received_at;
  const sideColor = isLong ? "text-crt-long" : signal.side === "SHORT" ? "text-crt-short" : "text-bone-400";
  const isActionable = signal.kind === "TRIGGER";
  const showProfit = signal.kind === "PROFIT" && signal.profit_pct !== null && signal.profit_pct !== undefined;

  return (
    <div
      className={clsx(
        "group relative grid animate-slideIn grid-cols-[78px_1fr_82px_minmax(0,1.4fr)_84px] items-center gap-3 border-b border-ink-500/40 border-l-2 px-4 py-3 transition-colors hover:bg-ink-800/60",
        meta.border,
        isActionable && "bg-crt-amber/[0.025]",
      )}
    >
      {/* Kind tag */}
      <div className="flex items-center gap-1.5">
        <Icon className={clsx("h-3.5 w-3.5 shrink-0", meta.accent)} strokeWidth={2.5} />
        <span className={clsx("text-[11px] font-bold tracking-[0.22em]", meta.accent)}>
          {meta.label}
        </span>
      </div>

      {/* Ticker + chart link */}
      <div className="flex min-w-0 items-center gap-2">
        <span className="text-base font-bold tracking-tight text-bone-50">{signal.ticker}</span>
        {signal.chart_url && (
          <a
            href={signal.chart_url}
            target="_blank"
            rel="noreferrer"
            className="text-bone-500 transition-colors hover:text-crt-amber"
            title="Open chart"
          >
            <ExternalLink className="h-3 w-3" />
          </a>
        )}
      </div>

      {/* Side */}
      <div className="flex items-center gap-1">
        {signal.side &&
          (isLong ? (
            <ArrowUpRight className={clsx("h-3.5 w-3.5", sideColor)} strokeWidth={2.5} />
          ) : (
            <ArrowDownRight className={clsx("h-3.5 w-3.5", sideColor)} strokeWidth={2.5} />
          ))}
        <span className={clsx("text-xs font-medium tracking-wider", sideColor)}>
          {signal.side ?? "—"}
        </span>
      </div>

      {/* Prices */}
      <div className="flex min-w-0 items-baseline gap-x-4 text-xs">
        <PriceCell label="trg" value={signal.trigger} valueClass={meta.accent} />
        {!showProfit && <PriceCell label="tgt" value={signal.target} />}
        <PriceCell label="now" value={signal.current_price} />
        {showProfit && (
          <PriceCell
            label="p/l"
            value={signal.profit_pct}
            suffix="%"
            valueClass={(signal.profit_pct ?? 0) >= 0 ? "text-crt-long" : "text-crt-short"}
          />
        )}
      </div>

      {/* Time */}
      <div className="flex flex-col text-right">
        <span className="tabular text-xs text-bone-100">{fmtTime(created)}</span>
        <span className="text-[10px] uppercase tracking-wider text-bone-500">
          {relativeTime(created)}
        </span>
      </div>
    </div>
  );
}

function PriceCell({
  label,
  value,
  suffix = "",
  valueClass,
}: {
  label: string;
  value: number | null | undefined;
  suffix?: string;
  valueClass?: string;
}) {
  const isNull = value === null || value === undefined;
  return (
    <div className="flex flex-col items-start">
      <span className="text-[9px] uppercase tracking-[0.18em] text-bone-500">{label}</span>
      <span
        className={clsx(
          "tabular text-sm font-medium leading-tight",
          isNull ? "text-bone-500" : valueClass ?? "text-bone-50",
        )}
      >
        {isNull ? "—" : `${fmtPrice(value)}${suffix}`}
      </span>
    </div>
  );
}
