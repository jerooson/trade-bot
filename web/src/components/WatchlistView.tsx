import { useEffect, useMemo, useRef, useState } from "react";
import type { TradePlan } from "../lib/types";
import { PlanCard } from "./PlanCard";

type Filter = "all" | "pinned" | "with-levels" | "today";

const PAGE_SIZE_OPTIONS = [10, 20, 50] as const;
type PageSize = (typeof PAGE_SIZE_OPTIONS)[number];

interface Props {
  plans: TradePlan[];
  loading: boolean;
  error: string | null;
  isPinned: (id: number | string | undefined) => boolean;
  onTogglePin: (id: number | string) => void;
  pinnedCount: number;
}

export function WatchlistView({ plans, loading, error, isPinned, onTogglePin, pinnedCount }: Props) {
  const [filter, setFilter] = useState<Filter>("all");
  const [tickerSearch, setTickerSearch] = useState("");
  const [pageSize, setPageSize] = useState<PageSize>(20);
  const [page, setPage] = useState(1);

  const resultsAnchorRef = useRef<HTMLDivElement>(null);

  const filtered = useMemo(() => {
    const todayKey = new Date().toLocaleDateString("en-CA"); // YYYY-MM-DD in local TZ

    return plans.filter((p) => {
      if (filter === "pinned") {
        if (!isPinned(p.discord?.message_id)) return false;
      } else if (filter === "with-levels") {
        if (p.watch_levels.length === 0) return false;
      } else if (filter === "today") {
        const created = p.discord?.created_at ?? p.received_at;
        if (!created) return false;
        const localKey = new Date(created).toLocaleDateString("en-CA");
        if (localKey !== todayKey) return false;
      }

      if (tickerSearch.trim()) {
        const needle = tickerSearch.trim().toUpperCase();
        if (!(p.ticker ?? "").toUpperCase().includes(needle)) return false;
      }

      return true;
    });
  }, [plans, filter, tickerSearch, isPinned]);

  const tickersInRange = useMemo(() => {
    const set = new Set<string>();
    filtered.forEach((p) => p.ticker && set.add(p.ticker));
    return set.size;
  }, [filtered]);

  // Reset to page 1 whenever the result set or page size changes.
  useEffect(() => {
    setPage(1);
  }, [filter, tickerSearch, pageSize, plans.length]);

  const totalPages = Math.max(1, Math.ceil(filtered.length / pageSize));
  const safePage = Math.min(page, totalPages);
  const startIdx = (safePage - 1) * pageSize;
  const endIdx = Math.min(startIdx + pageSize, filtered.length);
  const visible = filtered.slice(startIdx, endIdx);

  const goToPage = (p: number) => {
    const next = Math.max(1, Math.min(totalPages, p));
    setPage(next);
    // Smooth-scroll the results anchor back into view so the user doesn't
    // lose their place when paging on a long card grid.
    requestAnimationFrame(() => {
      resultsAnchorRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
    });
  };

  return (
    <main className="relative z-10 mx-auto max-w-[1200px] px-6 pb-16 pt-8">
      <div className="mb-6">
        <div className="flex items-end justify-between border-b border-ink-500/40 pb-4">
          <div>
            <div className="text-[10px] uppercase tracking-[0.32em] text-bone-500">
              section · 01 · watchlist
            </div>
            <h2 className="mt-1 font-editorial text-5xl italic leading-none text-bone-50">
              what to&nbsp;
              <span className="text-crt-amber">watch</span>
              <span className="text-bone-300">.</span>
            </h2>
          </div>
          <div className="hidden text-right md:block">
            <div className="text-[10px] uppercase tracking-[0.32em] text-bone-500">
              plans loaded
            </div>
            <div className="mt-1 tabular text-sm text-bone-300">
              {plans.length.toLocaleString()} total · {tickersInRange} unique tickers in view
            </div>
          </div>
        </div>
      </div>

      {error && (
        <div className="mb-6 border border-crt-short/60 bg-crt-short/10 px-4 py-3 text-sm text-crt-short">
          ERR — {error}
        </div>
      )}

      {loading && (
        <div className="mb-6 border border-ink-500/40 px-4 py-3 text-[11px] uppercase tracking-[0.32em] text-bone-400">
          loading plans…
        </div>
      )}

      {/* Filter bar */}
      <div className="mb-6 flex flex-wrap items-center justify-between gap-3 border border-ink-500/40 bg-ink-900/40 px-4 py-3">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-[10px] uppercase tracking-[0.32em] text-bone-500">show</span>
          {(["all", "today", "with-levels", "pinned"] as Filter[]).map((f) => (
            <FilterPill key={f} active={filter === f} onClick={() => setFilter(f)}>
              {f === "all" && "all"}
              {f === "today" && "today"}
              {f === "with-levels" && "with levels"}
              {f === "pinned" && (
                <>
                  pinned
                  {pinnedCount > 0 && (
                    <span className="ml-1.5 tabular text-[9px] text-bone-500">
                      {pinnedCount}
                    </span>
                  )}
                </>
              )}
            </FilterPill>
          ))}
        </div>

        <div className="flex items-center gap-3">
          <div className="hidden items-center gap-1.5 sm:flex">
            <span className="text-[10px] uppercase tracking-[0.32em] text-bone-500">per page</span>
            {PAGE_SIZE_OPTIONS.map((n) => (
              <button
                key={n}
                onClick={() => setPageSize(n)}
                className={
                  "tabular border px-2 py-1 text-[10px] transition-colors " +
                  (pageSize === n
                    ? "border-crt-amber bg-crt-amber/10 text-crt-amber"
                    : "border-ink-500/60 text-bone-400 hover:border-bone-300 hover:text-bone-100")
                }
              >
                {n}
              </button>
            ))}
          </div>
          <input
            type="text"
            value={tickerSearch}
            onChange={(e) => setTickerSearch(e.target.value)}
            placeholder="ticker…"
            className="w-40 border border-ink-500/60 bg-ink-900 px-3 py-1.5 font-mono text-[11px] uppercase tracking-[0.18em] text-bone-100 placeholder:text-bone-500 focus:border-bone-300 focus:outline-none"
          />
          <span className="tabular text-[11px] text-bone-500">
            {filtered.length.toString().padStart(4, "0")} / {plans.length.toString().padStart(4, "0")}
          </span>
        </div>
      </div>

      {/* Anchor used by pagination to jump back to the top of results */}
      <div ref={resultsAnchorRef} className="-mt-2 mb-2 scroll-mt-6" aria-hidden />

      {/* Results */}
      {filtered.length === 0 ? (
        <div className="border border-dashed border-ink-500/40 px-8 py-16 text-center">
          <div className="font-editorial text-2xl italic text-bone-300">
            {plans.length === 0
              ? "no plans yet"
              : filter === "pinned"
              ? "no pinned plans yet"
              : "no plans match this filter"}
          </div>
          <div className="mt-2 text-[11px] uppercase tracking-[0.32em] text-bone-500">
            {plans.length === 0
              ? "run `python -m bot.history --kind plan` to backfill"
              : "loosen the filter or wait for new posts"}
          </div>
        </div>
      ) : (
        <>
          {/* "Showing X-Y of Z" header above the cards */}
          <div className="mb-3 flex items-baseline justify-between text-[10px] uppercase tracking-[0.32em] text-bone-500">
            <span>
              showing{" "}
              <span className="tabular text-bone-200">
                {(startIdx + 1).toString().padStart(3, "0")}–{endIdx.toString().padStart(3, "0")}
              </span>{" "}
              of{" "}
              <span className="tabular text-bone-200">{filtered.length}</span>
            </span>
            <span>
              page{" "}
              <span className="tabular text-bone-200">{safePage}</span> /{" "}
              <span className="tabular text-bone-200">{totalPages}</span>
            </span>
          </div>

          <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
            {visible.map((p) => {
              const id = p.discord?.message_id;
              return (
                <PlanCard
                  key={id ?? `${p.ticker}-${p.received_at}`}
                  plan={p}
                  isPinned={isPinned(id)}
                  onTogglePin={() => id != null && onTogglePin(id)}
                />
              );
            })}
          </div>

          {/* Pagination control under the cards */}
          {totalPages > 1 && (
            <Pagination
              page={safePage}
              totalPages={totalPages}
              onChange={goToPage}
            />
          )}
        </>
      )}
    </main>
  );
}

// -- Pagination ---------------------------------------------------------------

function Pagination({
  page,
  totalPages,
  onChange,
}: {
  page: number;
  totalPages: number;
  onChange: (n: number) => void;
}) {
  const numbers = pageNumberWindow(page, totalPages);

  return (
    <nav
      aria-label="watchlist pagination"
      className="mt-8 flex flex-wrap items-center justify-center gap-1 border-t border-ink-500/40 pt-6"
    >
      <PageBtn disabled={page <= 1} onClick={() => onChange(1)} aria-label="first page">
        «
      </PageBtn>
      <PageBtn disabled={page <= 1} onClick={() => onChange(page - 1)} aria-label="previous page">
        ‹ prev
      </PageBtn>

      {numbers.map((n, i) =>
        n === "…" ? (
          <span
            key={`gap-${i}`}
            className="px-2 py-1 tabular text-[11px] text-bone-500"
            aria-hidden
          >
            …
          </span>
        ) : (
          <PageBtn
            key={n}
            active={n === page}
            onClick={() => onChange(n as number)}
            aria-label={`page ${n}`}
            aria-current={n === page ? "page" : undefined}
          >
            {String(n).padStart(2, "0")}
          </PageBtn>
        ),
      )}

      <PageBtn disabled={page >= totalPages} onClick={() => onChange(page + 1)} aria-label="next page">
        next ›
      </PageBtn>
      <PageBtn disabled={page >= totalPages} onClick={() => onChange(totalPages)} aria-label="last page">
        »
      </PageBtn>
    </nav>
  );
}

function PageBtn({
  children,
  onClick,
  disabled,
  active,
  ...rest
}: {
  children: React.ReactNode;
  onClick: () => void;
  disabled?: boolean;
  active?: boolean;
} & React.ButtonHTMLAttributes<HTMLButtonElement>) {
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      className={
        "tabular min-w-[34px] border px-2.5 py-1.5 text-[11px] uppercase tracking-[0.18em] transition-colors " +
        (active
          ? "border-crt-amber bg-crt-amber/10 text-crt-amber"
          : disabled
          ? "cursor-not-allowed border-ink-500/30 text-bone-600"
          : "border-ink-500/60 text-bone-300 hover:border-bone-300 hover:text-bone-50")
      }
      {...rest}
    >
      {children}
    </button>
  );
}

/**
 * Build a windowed list of page numbers with ellipses, e.g.
 *   page 7 of 25  ->  [1, "…", 5, 6, 7, 8, 9, "…", 25]
 *   page 1 of 4   ->  [1, 2, 3, 4]
 *   page 1 of 50  ->  [1, 2, 3, 4, 5, "…", 50]
 *
 * We always include first + last, plus ±2 around the current page.
 */
function pageNumberWindow(page: number, totalPages: number): (number | "…")[] {
  if (totalPages <= 7) {
    return Array.from({ length: totalPages }, (_, i) => i + 1);
  }

  const window = new Set<number>([1, totalPages, page - 1, page, page + 1, page - 2, page + 2]);
  const sorted = [...window].filter((n) => n >= 1 && n <= totalPages).sort((a, b) => a - b);

  const out: (number | "…")[] = [];
  let prev = 0;
  for (const n of sorted) {
    if (prev && n - prev > 1) out.push("…");
    out.push(n);
    prev = n;
  }
  return out;
}

function FilterPill({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      onClick={onClick}
      className={
        "inline-flex items-center border px-3 py-1.5 text-[11px] uppercase tracking-[0.18em] transition-colors " +
        (active
          ? "border-crt-amber bg-crt-amber/10 text-crt-amber"
          : "border-ink-500/60 text-bone-300 hover:border-bone-300 hover:text-bone-50")
      }
    >
      {children}
    </button>
  );
}
