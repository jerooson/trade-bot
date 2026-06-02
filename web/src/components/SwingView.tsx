import { useEffect, useMemo, useRef, useState } from "react";
import type { ActionKind, OpenPosition, TradeAction } from "../lib/types";
import { ActionRow } from "./ActionRow";
import { PositionCard } from "./PositionCard";

type Filter = "all" | "actionable" | "entries" | "adds" | "exits" | "stops";
type Tab = "book" | "tape";

const PAGE_SIZE_OPTIONS = [25, 50, 100] as const;
type PageSize = (typeof PAGE_SIZE_OPTIONS)[number];

const TAB_STORAGE_KEY = "swing-tab";

interface Props {
  actions: TradeAction[];
  openPositions: OpenPosition[];
  loading: boolean;
  error: string | null;
}

const FILTER_KIND_MAP: Partial<Record<Filter, ActionKind[]>> = {
  entries: ["ENTRY"],
  adds: ["ADD"],
  exits: ["CLOSE", "REDUCE"],
  stops: ["STOP_TRIGGER", "STOP_UPDATE"],
};

const ACTIONABLE_KINDS: Set<ActionKind> = new Set([
  "ENTRY",
  "ADD",
  "REDUCE",
  "CLOSE",
  "STOP_TRIGGER",
]);

export function SwingView({ actions, openPositions, loading, error }: Props) {
  // Persisted tab choice; defaults to "book" since that's the actionable view.
  const [tab, setTab] = useState<Tab>(() => {
    try {
      const saved = localStorage.getItem(TAB_STORAGE_KEY);
      return saved === "tape" ? "tape" : "book";
    } catch {
      return "book";
    }
  });
  useEffect(() => {
    try {
      localStorage.setItem(TAB_STORAGE_KEY, tab);
    } catch {
      // ignore storage errors (private mode, quota, etc.)
    }
  }, [tab]);

  const [filter, setFilter] = useState<Filter>("actionable");
  const [tickerSearch, setTickerSearch] = useState("");
  const [pageSize, setPageSize] = useState<PageSize>(50);
  const [page, setPage] = useState(1);
  const resultsAnchorRef = useRef<HTMLDivElement>(null);

  // Ticker search applies to BOTH tabs so a single query narrows the whole
  // execution surface to one symbol.
  const tickerNeedle = tickerSearch.trim().toUpperCase();

  const filteredPositions = useMemo(() => {
    if (!tickerNeedle) return openPositions;
    return openPositions.filter((p) => p.ticker.toUpperCase().includes(tickerNeedle));
  }, [openPositions, tickerNeedle]);

  const filteredActions = useMemo(() => {
    return actions.filter((a) => {
      if (filter === "actionable") {
        if (!ACTIONABLE_KINDS.has(a.kind)) return false;
      } else if (filter !== "all") {
        const allowed = FILTER_KIND_MAP[filter];
        if (allowed && !allowed.includes(a.kind)) return false;
      }
      if (tickerNeedle && !a.ticker.toUpperCase().includes(tickerNeedle)) return false;
      return true;
    });
  }, [actions, filter, tickerNeedle]);

  // Reset pagination when anything narrowing the view changes.
  useEffect(() => {
    setPage(1);
  }, [tab, filter, tickerSearch, pageSize, actions.length]);

  const totalPages = Math.max(1, Math.ceil(filteredActions.length / pageSize));
  const safePage = Math.min(page, totalPages);
  const startIdx = (safePage - 1) * pageSize;
  const endIdx = Math.min(startIdx + pageSize, filteredActions.length);
  const visible = filteredActions.slice(startIdx, endIdx);

  const goToPage = (p: number) => {
    const next = Math.max(1, Math.min(totalPages, p));
    setPage(next);
    requestAnimationFrame(() => {
      resultsAnchorRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
    });
  };

  // Always-visible book summary -- accurate even on the TAPE tab so the
  // user always knows their P/L without switching back.
  const winners = openPositions.filter((p) => (p.last_pnl_pct ?? 0) > 0).length;
  const losers = openPositions.filter((p) => (p.last_pnl_pct ?? 0) < 0).length;
  const avgPnl =
    openPositions.length === 0
      ? null
      : openPositions
          .map((p) => p.last_pnl_pct ?? 0)
          .reduce((a, b) => a + b, 0) / openPositions.length;

  return (
    <main className="relative z-10 mx-auto max-w-[1400px] px-6 pb-16 pt-8">
      {/* Page header */}
      <div className="mb-6">
        <div className="flex items-end justify-between border-b border-ink-500/40 pb-4">
          <div>
            <div className="text-[10px] uppercase tracking-[0.32em] text-bone-500">
              section · 02 · execution
            </div>
            <h2 className="mt-1 font-editorial text-5xl italic leading-none text-bone-50">
              live&nbsp;
              <span className="text-crt-amber">positions</span>
              <span className="text-bone-300">.</span>
            </h2>
          </div>
          <div className="hidden text-right md:block">
            <div className="text-[10px] uppercase tracking-[0.32em] text-bone-500">
              actions tracked
            </div>
            <div className="mt-1 tabular text-sm text-bone-300">
              {actions.length.toLocaleString()} total · {openPositions.length} open
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
          loading actions…
        </div>
      )}

      {/* Always-visible summary strip */}
      <div className="mb-4 flex flex-wrap items-baseline justify-between gap-x-6 gap-y-2 border border-ink-500/40 bg-ink-900/40 px-4 py-3">
        <div className="flex flex-wrap items-baseline gap-x-6 gap-y-1">
          <Stat label="open" value={openPositions.length.toString()} />
          <Stat label="winners" value={winners.toString()} tone={winners > 0 ? "long" : undefined} />
          <Stat label="losers" value={losers.toString()} tone={losers > 0 ? "short" : undefined} />
          {avgPnl != null && (
            <Stat
              label="avg p/l"
              value={`${avgPnl > 0 ? "+" : ""}${avgPnl.toFixed(1)}%`}
              tone={avgPnl > 0 ? "long" : avgPnl < 0 ? "short" : undefined}
            />
          )}
          <Stat label="actions" value={actions.length.toLocaleString()} />
        </div>
      </div>

      {/* Tab strip + shared ticker search */}
      <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-stretch border border-ink-500/40 bg-ink-900/40">
          <TabButton
            active={tab === "book"}
            onClick={() => setTab("book")}
            count={tickerNeedle ? filteredPositions.length : openPositions.length}
            countTotal={tickerNeedle ? openPositions.length : null}
          >
            <span className="font-editorial text-base italic">the book</span>
            <span className="ml-2 text-[10px] uppercase tracking-[0.32em] opacity-70">
              positions
            </span>
          </TabButton>
          <TabButton
            active={tab === "tape"}
            onClick={() => setTab("tape")}
            count={tickerNeedle ? filteredActions.length : actions.length}
            countTotal={tickerNeedle ? actions.length : null}
          >
            <span className="font-editorial text-base italic">the tape</span>
            <span className="ml-2 text-[10px] uppercase tracking-[0.32em] opacity-70">
              feed
            </span>
          </TabButton>
        </div>

        <div className="flex items-center gap-3">
          <input
            type="text"
            value={tickerSearch}
            onChange={(e) => setTickerSearch(e.target.value)}
            placeholder="ticker…"
            className="w-40 border border-ink-500/60 bg-ink-900 px-3 py-1.5 font-mono text-[11px] uppercase tracking-[0.18em] text-bone-100 placeholder:text-bone-500 focus:border-bone-300 focus:outline-none"
          />
          {tickerNeedle && (
            <button
              onClick={() => setTickerSearch("")}
              className="border border-ink-500/60 px-2 py-1.5 text-[10px] uppercase tracking-[0.18em] text-bone-400 hover:border-bone-300 hover:text-bone-100"
            >
              clear
            </button>
          )}
        </div>
      </div>

      <div ref={resultsAnchorRef} className="-mt-2 scroll-mt-6" aria-hidden />

      {/* Tab content */}
      {tab === "book" && (
        <BookContent positions={filteredPositions} totalPositions={openPositions.length} tickerNeedle={tickerNeedle} />
      )}

      {tab === "tape" && (
        <TapeContent
          filter={filter}
          setFilter={setFilter}
          pageSize={pageSize}
          setPageSize={setPageSize}
          visible={visible}
          totalActions={actions.length}
          filteredCount={filteredActions.length}
          page={safePage}
          totalPages={totalPages}
          startIdx={startIdx}
          endIdx={endIdx}
          onChangePage={goToPage}
        />
      )}
    </main>
  );
}

// -- Tab content sub-components ---------------------------------------------

function BookContent({
  positions,
  totalPositions,
  tickerNeedle,
}: {
  positions: OpenPosition[];
  totalPositions: number;
  tickerNeedle: string;
}) {
  if (totalPositions === 0) {
    return (
      <div className="border border-dashed border-ink-500/40 px-6 py-16 text-center">
        <div className="font-editorial text-xl italic text-bone-300">
          no open positions
        </div>
        <div className="mt-1 text-[10px] uppercase tracking-[0.32em] text-bone-500">
          waiting for the next ENTRY
        </div>
      </div>
    );
  }

  if (positions.length === 0) {
    return (
      <div className="border border-dashed border-ink-500/40 px-6 py-12 text-center">
        <div className="font-editorial text-xl italic text-bone-300">
          no open position matches “{tickerNeedle}”
        </div>
        <div className="mt-1 text-[10px] uppercase tracking-[0.32em] text-bone-500">
          clear the ticker filter to see all {totalPositions} positions
        </div>
      </div>
    );
  }

  return (
    <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
      {positions.map((p) => (
        <PositionCard key={p.ticker} pos={p} />
      ))}
    </div>
  );
}

function TapeContent({
  filter,
  setFilter,
  pageSize,
  setPageSize,
  visible,
  totalActions,
  filteredCount,
  page,
  totalPages,
  startIdx,
  endIdx,
  onChangePage,
}: {
  filter: Filter;
  setFilter: (f: Filter) => void;
  pageSize: PageSize;
  setPageSize: (n: PageSize) => void;
  visible: TradeAction[];
  totalActions: number;
  filteredCount: number;
  page: number;
  totalPages: number;
  startIdx: number;
  endIdx: number;
  onChangePage: (n: number) => void;
}) {
  return (
    <>
      {/* Tape-specific filter bar */}
      <div className="mb-3 flex flex-wrap items-center justify-between gap-3 border border-ink-500/40 bg-ink-900/40 px-4 py-3">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-[10px] uppercase tracking-[0.32em] text-bone-500">show</span>
          {(["actionable", "all", "entries", "adds", "exits", "stops"] as Filter[]).map((f) => (
            <FilterPill key={f} active={filter === f} onClick={() => setFilter(f)}>
              {f}
            </FilterPill>
          ))}
        </div>
        <div className="flex items-center gap-3">
          <div className="hidden items-center gap-1.5 sm:flex">
            <span className="text-[10px] uppercase tracking-[0.32em] text-bone-500">
              per page
            </span>
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
          <span className="tabular text-[11px] text-bone-500">
            {filteredCount.toString().padStart(4, "0")} /{" "}
            {totalActions.toString().padStart(4, "0")}
          </span>
        </div>
      </div>

      {filteredCount === 0 ? (
        <div className="border border-dashed border-ink-500/40 px-6 py-12 text-center">
          <div className="font-editorial text-xl italic text-bone-300">
            {totalActions === 0 ? "no actions yet" : "no actions match this filter"}
          </div>
          <div className="mt-1 text-[10px] uppercase tracking-[0.32em] text-bone-500">
            {totalActions === 0
              ? "run `python -m bot.history --kind swing` to backfill"
              : "try a different filter"}
          </div>
        </div>
      ) : (
        <>
          <div className="mb-2 flex items-baseline justify-between text-[10px] uppercase tracking-[0.32em] text-bone-500">
            <span>
              showing{" "}
              <span className="tabular text-bone-200">
                {(startIdx + 1).toString().padStart(3, "0")}–{endIdx.toString().padStart(3, "0")}
              </span>{" "}
              of <span className="tabular text-bone-200">{filteredCount}</span>
            </span>
            <span>
              page <span className="tabular text-bone-200">{page}</span> /{" "}
              <span className="tabular text-bone-200">{totalPages}</span>
            </span>
          </div>

          <div className="flex flex-col divide-y divide-ink-500/30 border border-ink-500/40 bg-ink-900/20">
            {visible.map((a) => (
              <ActionRow
                key={a.discord?.message_id ?? `${a.ticker}-${a.received_at}-${a.kind}`}
                action={a}
              />
            ))}
          </div>

          {totalPages > 1 && (
            <Pagination page={page} totalPages={totalPages} onChange={onChangePage} />
          )}
        </>
      )}
    </>
  );
}

// -- Small UI primitives -----------------------------------------------------

function TabButton({
  active,
  onClick,
  children,
  count,
  countTotal,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
  count: number;
  /** When set, renders "count / countTotal" to show filtering. */
  countTotal: number | null;
}) {
  return (
    <button
      onClick={onClick}
      className={
        "group flex items-baseline gap-3 border-r border-ink-500/40 px-5 py-2.5 last:border-r-0 transition-colors " +
        (active
          ? "bg-crt-amber/10 text-crt-amber"
          : "text-bone-300 hover:bg-ink-800/50 hover:text-bone-50")
      }
      aria-pressed={active}
    >
      {children}
      <span
        className={
          "tabular ml-1 text-[10px] " +
          (active ? "text-crt-amber/80" : "text-bone-500 group-hover:text-bone-400")
        }
      >
        {countTotal != null ? `${count}/${countTotal}` : count.toLocaleString()}
      </span>
    </button>
  );
}

function Stat({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone?: "long" | "short";
}) {
  return (
    <div className="flex items-baseline gap-2">
      <span className="text-[10px] uppercase tracking-[0.32em] text-bone-500">
        {label}
      </span>
      <span
        className={
          "tabular text-sm " +
          (tone === "long"
            ? "text-crt-long"
            : tone === "short"
            ? "text-crt-short"
            : "text-bone-200")
        }
      >
        {value}
      </span>
    </div>
  );
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

// -- Pagination (mirrors WatchlistView) ---------------------------------------

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
      aria-label="swing-trades pagination"
      className="mt-6 flex flex-wrap items-center justify-center gap-1 border-t border-ink-500/40 pt-4"
    >
      <PageBtn disabled={page <= 1} onClick={() => onChange(1)} aria-label="first page">
        «
      </PageBtn>
      <PageBtn disabled={page <= 1} onClick={() => onChange(page - 1)} aria-label="prev page">
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
      <PageBtn
        disabled={page >= totalPages}
        onClick={() => onChange(page + 1)}
        aria-label="next page"
      >
        next ›
      </PageBtn>
      <PageBtn
        disabled={page >= totalPages}
        onClick={() => onChange(totalPages)}
        aria-label="last page"
      >
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

function pageNumberWindow(page: number, totalPages: number): (number | "…")[] {
  if (totalPages <= 7) return Array.from({ length: totalPages }, (_, i) => i + 1);
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
