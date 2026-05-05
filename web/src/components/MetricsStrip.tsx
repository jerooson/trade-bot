import type { Stats } from "../lib/types";
import type { DateRange } from "../lib/filters";

interface Props {
  stats: Stats;
  range: DateRange;
}

const RANGE_LABEL: Record<DateRange, string> = {
  TODAY: "today",
  YESTERDAY: "yday",
  "7D": "7d",
  "30D": "30d",
  ALL: "all-time",
};

export function MetricsStrip({ stats, range }: Props) {
  const label = RANGE_LABEL[range];

  const tiles: { label: string; value: string; sub?: string; tone?: "amber" | "long" | "short" | "info" }[] = [
    {
      label: `signals · ${label}`,
      value: stats.total.toLocaleString(),
      sub: range === "TODAY" ? "live" : "parsed",
    },
    {
      label: `triggers · ${label}`,
      value: (stats.by_kind.TRIGGER ?? 0).toString(),
      sub: "actionable",
      tone: "amber",
    },
    {
      label: `plans · ${label}`,
      value: (stats.by_kind.PLAN ?? 0).toString(),
      sub: "heads-up",
      tone: "info",
    },
    {
      label: `profit pings · ${label}`,
      value: (stats.by_kind.PROFIT ?? 0).toString(),
      sub: "informational",
    },
    {
      label: "long / short",
      value: `${stats.by_side.LONG ?? 0} / ${stats.by_side.SHORT ?? 0}`,
      tone: "long",
    },
    {
      label: "with target",
      value:
        stats.has_target + stats.no_target > 0
          ? `${stats.has_target} / ${stats.has_target + stats.no_target}`
          : "—",
      sub: "plan + trigger",
    },
  ];

  return (
    <div className="grid grid-cols-2 gap-px bg-ink-500/40 sm:grid-cols-3 lg:grid-cols-6">
      {tiles.map((t) => (
        <Tile key={t.label} {...t} />
      ))}
    </div>
  );
}

function Tile({
  label,
  value,
  sub,
  tone,
}: {
  label: string;
  value: string;
  sub?: string;
  tone?: "amber" | "long" | "short" | "info";
}) {
  const valueColor =
    tone === "amber"
      ? "text-crt-amber"
      : tone === "long"
        ? "text-crt-long"
        : tone === "short"
          ? "text-crt-short"
          : tone === "info"
            ? "text-crt-info"
            : "text-bone-50";

  return (
    <div className="relative overflow-hidden bg-ink-900 px-5 py-5">
      <div className="flex items-baseline justify-between">
        <span className="text-[10px] uppercase tracking-[0.32em] text-bone-500">{label}</span>
        {tone && (
          <span
            className={`h-1.5 w-1.5 rounded-full ${
              tone === "amber"
                ? "bg-crt-amber"
                : tone === "long"
                  ? "bg-crt-long"
                  : tone === "short"
                    ? "bg-crt-short"
                    : "bg-crt-info"
            }`}
          />
        )}
      </div>
      <div className={`tabular mt-3 text-4xl font-medium leading-none ${valueColor}`}>{value}</div>
      {sub && (
        <div className="mt-2 text-[11px] uppercase tracking-[0.18em] text-bone-500">{sub}</div>
      )}
    </div>
  );
}
