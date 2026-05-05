import type { Signal, Stats } from "./types";

/**
 * Compute Stats-shape aggregates from an arbitrary signal list (typically the
 * date-filtered subset). Mirrors the server's /api/stats logic but runs on
 * the client so filters apply instantly with no round-trip.
 *
 * Times are in UTC (matching what Discord stamps); the by_hour_utc and by_day
 * fields are honest about that. The "today_count" reflects today in the user's
 * *local* timezone -- consistent with the date filters.
 */
export function deriveStats(signals: Signal[]): Stats {
  const by_kind: Record<string, number> = {};
  const by_side: Record<string, number> = {};
  const by_hour: Record<number, number> = {};
  const by_day: Record<string, number> = {};
  const by_ticker_kind: Record<string, Record<string, number>> = {};
  const triggers_per_ticker: Record<string, number[]> = {};

  let earliest: Date | null = null;
  let latest: Date | null = null;
  let has_target = 0;
  let no_target = 0;

  for (const r of signals) {
    const kind = r.kind;
    by_kind[kind] = (by_kind[kind] ?? 0) + 1;
    const sideKey = r.side ?? "UNK";
    by_side[sideKey] = (by_side[sideKey] ?? 0) + 1;

    const ticker = r.ticker;
    if (!by_ticker_kind[ticker]) by_ticker_kind[ticker] = {};
    by_ticker_kind[ticker][kind] = (by_ticker_kind[ticker][kind] ?? 0) + 1;

    if (kind === "TRIGGER" && r.trigger !== null && r.trigger !== undefined) {
      (triggers_per_ticker[ticker] ??= []).push(r.trigger);
    }

    if (kind === "PLAN" || kind === "TRIGGER") {
      if (r.target !== null && r.target !== undefined) has_target++;
      else no_target++;
    }

    const iso = r.discord?.created_at ?? r.received_at;
    if (iso) {
      const d = new Date(iso);
      if (!earliest || d < earliest) earliest = d;
      if (!latest || d > latest) latest = d;
      const hour = d.getUTCHours();
      by_hour[hour] = (by_hour[hour] ?? 0) + 1;
      const dayKey = d.toISOString().slice(0, 10);
      by_day[dayKey] = (by_day[dayKey] ?? 0) + 1;
    }
  }

  // Today (local timezone)
  const todayLocal = new Date().toLocaleDateString("en-CA");
  const today_count = signals.filter((r) => {
    const iso = r.discord?.created_at ?? r.received_at;
    if (!iso) return false;
    return new Date(iso).toLocaleDateString("en-CA") === todayLocal;
  }).length;

  const top_tickers = Object.entries(by_ticker_kind)
    .map(([ticker, c]) => ({
      ticker,
      total: (c.PLAN ?? 0) + (c.TRIGGER ?? 0) + (c.PROFIT ?? 0),
      trigger: c.TRIGGER ?? 0,
      plan: c.PLAN ?? 0,
      profit: c.PROFIT ?? 0,
    }))
    .sort((a, b) => b.total - a.total)
    .slice(0, 15);

  const trigger_prices = Object.entries(triggers_per_ticker)
    .map(([ticker, prices]) => ({
      ticker,
      prices: Array.from(new Set(prices)).sort((a, b) => a - b),
    }))
    .sort((a, b) => b.prices.length - a.prices.length)
    .slice(0, 10);

  return {
    total: signals.length,
    by_kind,
    by_side,
    earliest: earliest ? (earliest as Date).toISOString() : null,
    latest: latest ? (latest as Date).toISOString() : null,
    today_count,
    today_date_utc: todayLocal,
    has_target,
    no_target,
    by_hour_utc: Array.from({ length: 24 }, (_, h) => ({ hour: h, count: by_hour[h] ?? 0 })),
    by_day: Object.entries(by_day)
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([date, count]) => ({ date, count })),
    top_tickers,
    trigger_prices,
  };
}
