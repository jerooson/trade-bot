import type { Signal } from "./types";

export type DateRange = "TODAY" | "YESTERDAY" | "7D" | "30D" | "ALL";

export const DATE_RANGE_OPTIONS: { id: DateRange; label: string; sub: string }[] = [
  { id: "TODAY", label: "today", sub: "intraday" },
  { id: "YESTERDAY", label: "yday", sub: "prev session" },
  { id: "7D", label: "7d", sub: "rolling week" },
  { id: "30D", label: "30d", sub: "rolling month" },
  { id: "ALL", label: "all", sub: "full capture" },
];

/** YYYY-MM-DD in the *user's local* timezone. */
export function localDateKey(d: Date): string {
  // en-CA always formats as YYYY-MM-DD, in local time.
  return d.toLocaleDateString("en-CA");
}

/** True if `iso` (UTC ISO timestamp) falls on the same local date as today. */
function sameLocalDay(iso: string, refDate: Date): boolean {
  return localDateKey(new Date(iso)) === localDateKey(refDate);
}

/** Beginning of N days ago in local time (00:00:00 local). */
function nDaysAgoStart(n: number): Date {
  const d = new Date();
  d.setHours(0, 0, 0, 0);
  d.setDate(d.getDate() - n);
  return d;
}

/**
 * Filter signals by a date range in the user's local timezone.
 * Uses Discord's created_at when available, else received_at.
 */
export function filterByRange(signals: Signal[], range: DateRange): Signal[] {
  if (range === "ALL") return signals;

  const now = new Date();

  if (range === "TODAY") {
    return signals.filter((s) => {
      const iso = s.discord?.created_at ?? s.received_at;
      return iso && sameLocalDay(iso, now);
    });
  }

  if (range === "YESTERDAY") {
    const y = new Date();
    y.setDate(y.getDate() - 1);
    return signals.filter((s) => {
      const iso = s.discord?.created_at ?? s.received_at;
      return iso && sameLocalDay(iso, y);
    });
  }

  const cutoff =
    range === "7D" ? nDaysAgoStart(6) : range === "30D" ? nDaysAgoStart(29) : null;
  if (!cutoff) return signals;

  return signals.filter((s) => {
    const iso = s.discord?.created_at ?? s.received_at;
    if (!iso) return false;
    return new Date(iso) >= cutoff;
  });
}

/** Human-readable subtitle describing the active range. */
export function describeRange(range: DateRange): string {
  const today = localDateKey(new Date());
  if (range === "TODAY") return today;
  if (range === "YESTERDAY") {
    const y = new Date();
    y.setDate(y.getDate() - 1);
    return localDateKey(y);
  }
  if (range === "7D") {
    const start = nDaysAgoStart(6);
    return `${localDateKey(start)} → ${today}`;
  }
  if (range === "30D") {
    const start = nDaysAgoStart(29);
    return `${localDateKey(start)} → ${today}`;
  }
  return "full capture";
}

/** True if range covers a single calendar day (so multi-day charts should hide). */
export function isSingleDay(range: DateRange): boolean {
  return range === "TODAY" || range === "YESTERDAY";
}
