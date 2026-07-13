import { useState } from "react";
import clsx from "clsx";
import type { DayTradePosition, DayTradePnl } from "../lib/types";
import { SignalsView, type DashLike } from "./SignalsView";
import { relativeTime } from "../lib/format";

type SubTab = "plans" | "active" | "pnl";

interface Props {
  dash: DashLike;
  positions: DayTradePosition[];
  pnl: DayTradePnl | null;
  serviceRunning: boolean;
}

export function DayTradeView({ dash, positions, pnl, serviceRunning }: Props) {
  const [sub, setSub] = useState<SubTab>("plans");

  const openCount = positions.filter((p) => p.status === "open" || p.status === "pending_exit").length;
  const totalPnl = pnl?.total_realized_pnl ?? 0;

  return (
    <div className="relative z-10 mx-auto max-w-[1400px] px-6 pb-16 pt-8">
      {/* Header */}
      <div className="mb-6">
        <div className="flex items-end justify-between border-b border-ink-500/40 pb-4">
          <div>
            <div className="text-[10px] uppercase tracking-[0.32em] text-bone-500">
              day trading · 日内短线
            </div>
            <h2 className="mt-1 font-editorial text-5xl italic leading-none text-bone-50">
              day&nbsp;
              <span className={clsx(totalPnl >= 0 ? "text-crt-long" : "text-crt-short")}>
                trade
              </span>
              <span className="text-bone-300">.</span>
            </h2>
          </div>
          <div className="flex items-center gap-2">
            <span className={clsx(
              "inline-flex items-center gap-1.5 border px-2 py-1 text-[10px] uppercase tracking-[0.18em]",
              serviceRunning
                ? "border-crt-long/50 bg-crt-long/10 text-crt-long"
                : "border-ink-500/50 text-bone-500"
            )}>
              <span className={clsx("h-1.5 w-1.5 rounded-full", serviceRunning ? "animate-pulseDot bg-crt-long" : "bg-bone-600")} />
              {serviceRunning ? "bot active" : "bot offline"}
            </span>
          </div>
        </div>
      </div>

      {/* Quick stats */}
      <div className="mb-6 grid grid-cols-2 gap-3 md:grid-cols-4">
        <StatTile
          label="Watching"
          value={(positions.filter(p => p.status === "watching" || p.status === "pending_entry").length).toString()}
          caption="plans / limit orders"
        />
        <StatTile label="Open Trades" value={openCount.toString()} caption="in market" highlight={openCount > 0} />
        <StatTile
          label="Realized P&L"
          value={`${totalPnl >= 0 ? "+" : ""}$${totalPnl.toFixed(2)}`}
          valueClass={totalPnl >= 0 ? "text-crt-long" : "text-crt-short"}
          caption={`${pnl?.wins ?? 0}W · ${pnl?.losses ?? 0}L today`}
        />
        <StatTile label="Plans Today" value={(pnl?.trades_today ?? 0).toString()} caption="executed" />
      </div>

      {/* Sub-tab nav */}
      <div className="mb-4 flex items-center gap-2">
        {(["plans", "active", "pnl"] as SubTab[]).map((t) => (
          <button
            key={t}
            onClick={() => setSub(t)}
            className={clsx(
              "border px-3 py-1.5 text-[10px] uppercase tracking-[0.28em] transition-colors",
              sub === t
                ? "border-crt-amber/60 bg-crt-amber/10 text-crt-amber"
                : "border-ink-500/60 text-bone-400 hover:border-bone-300 hover:text-bone-100",
            )}
          >
            {t === "plans" ? "Signal Plans" : t === "active" ? "Active Trades" : "P&L"}
          </button>
        ))}
      </div>

      {sub === "plans" && <SignalsView dash={dash} />}
      {sub === "active" && <ActiveTab positions={positions} serviceRunning={serviceRunning} />}
      {sub === "pnl" && <PnlTab pnl={pnl} />}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Active trades tab
// ---------------------------------------------------------------------------

function ActiveTab({ positions, serviceRunning }: { positions: DayTradePosition[]; serviceRunning: boolean }) {
  if (!serviceRunning) {
    return (
      <div className="border border-dashed border-crt-amber/40 bg-crt-amber/[0.04] px-6 py-12 text-center">
        <div className="font-editorial text-2xl italic text-bone-300">day trader not running</div>
        <div className="mt-2 text-[11px] uppercase tracking-[0.32em] text-bone-500">
          deploy bot/day_trader.py to activate automated day trading
        </div>
      </div>
    );
  }

  const watching = positions.filter((p) => p.status === "watching" || p.status === "pending_entry");
  const open = positions.filter((p) => p.status === "open" || p.status === "pending_exit");
  const closed = positions.filter((p) => p.status === "closed");

  return (
    <>
      {watching.length > 0 && (
        <Section title="Watching" subtitle="waiting for trigger price to be crossed">
          <div className="flex flex-col">
            <GridHeader cols={["ticker", "trigger", "setup", "plan age", "status"]} spans={[2,2,4,2,2]} />
            {watching.map((p) => (
              <div key={p.id} className="grid grid-cols-12 items-center gap-3 border-b border-ink-500/20 px-4 py-3 hover:bg-ink-800/30">
                <div className="col-span-2 font-editorial text-xl italic text-bone-50">{p.ticker}</div>
                <div className="col-span-2 tabular text-sm text-crt-amber">
                  ${p.trigger_price?.toFixed(2) ?? "—"}
                  {p.status === "pending_entry" && p.entry_limit_price != null && (
                    <span className="ml-1 text-[9px] text-bone-500">cap ${p.entry_limit_price.toFixed(2)}</span>
                  )}
                </div>
                <div className="col-span-4 text-[11px] text-bone-400">{p.setup ?? "—"}</div>
                <div className="col-span-2 tabular text-[11px] text-bone-500">{relativeTime(p.plan_received_at)}</div>
                <div className="col-span-2">
                  <span className="inline-flex items-center gap-1 border border-crt-amber/50 bg-crt-amber/10 px-1.5 py-0.5 text-[9px] uppercase tracking-[0.18em] text-crt-amber">
                    <span className="h-1.5 w-1.5 animate-pulseDot rounded-full bg-crt-amber" />
                    {p.status === "pending_entry" ? "limit pending" : "watching"}
                  </span>
                </div>
              </div>
            ))}
          </div>
        </Section>
      )}

      {open.length > 0 && (
        <Section title="Open Positions" subtitle="in market — stop loss / broker exit tracking active">
          <div className="flex flex-col">
            <GridHeader cols={["ticker", "fill", "stop", "high", "p&l est.", "since"]} spans={[2,2,2,2,2,2]} />
            {open.map((p) => {
              const pnlEst = p.fill_price && p.current_price
                ? ((p.current_price - p.fill_price) / p.fill_price * 100)
                : null;
              return (
                <div key={p.id} className="grid grid-cols-12 items-center gap-3 border-b border-ink-500/20 px-4 py-3 hover:bg-ink-800/30">
                  <div className="col-span-2 font-editorial text-xl italic text-bone-50">{p.ticker}</div>
                  <div className="col-span-2 tabular text-sm text-bone-200">${p.fill_price?.toFixed(2) ?? "—"}</div>
                  <div className="col-span-2 tabular text-sm text-crt-short">
                    {p.status === "pending_exit"
                      ? `selling · ${p.exit_reason ?? "exit"}`
                      : `$${p.stop_price?.toFixed(2) ?? "—"}`}
                  </div>
                  <div className="col-span-2 tabular text-sm text-bone-400">${p.high_water_mark?.toFixed(2) ?? "—"}</div>
                  <div className="col-span-2">
                    {pnlEst != null ? (
                      <span className={clsx("tabular text-sm font-medium", pnlEst >= 0 ? "text-crt-long" : "text-crt-short")}>
                        {pnlEst >= 0 ? "+" : ""}{pnlEst.toFixed(2)}%
                      </span>
                    ) : <span className="text-bone-500">—</span>}
                  </div>
                  <div className="col-span-2 tabular text-[11px] text-bone-500">{relativeTime(p.entered_at)}</div>
                </div>
              );
            })}
          </div>
        </Section>
      )}

      {open.length === 0 && watching.length === 0 && (
        <Section title="Active Trades" subtitle="no active day trades">
          <div className="px-6 py-12 text-center font-editorial text-lg italic text-bone-400">
            No active day trades. Waiting for a new PLAN signal from Will.
          </div>
        </Section>
      )}

      {closed.length > 0 && (
        <Section title="Closed Today" subtitle="completed day trades">
          <div className="flex flex-col">
            <GridHeader cols={["ticker", "fill", "exit", "p&l $", "p&l %", "reason"]} spans={[2,2,2,2,2,2]} />
            {closed.map((p) => (
              <div key={p.id} className="grid grid-cols-12 items-center gap-3 border-b border-ink-500/20 px-4 py-3 hover:bg-ink-800/30">
                <div className="col-span-2 font-editorial text-xl italic text-bone-50">{p.ticker}</div>
                <div className="col-span-2 tabular text-sm text-bone-300">${p.fill_price?.toFixed(2) ?? "—"}</div>
                <div className="col-span-2 tabular text-sm text-bone-300">${p.exit_price?.toFixed(2) ?? "—"}</div>
                <div className="col-span-2">
                  <span className={clsx("tabular text-sm font-medium", (p.realized_pnl ?? 0) >= 0 ? "text-crt-long" : "text-crt-short")}>
                    {(p.realized_pnl ?? 0) >= 0 ? "+" : ""}${p.realized_pnl?.toFixed(2) ?? "0.00"}
                  </span>
                </div>
                <div className="col-span-2">
                  <span className={clsx("tabular text-sm", (p.realized_pnl_pct ?? 0) >= 0 ? "text-crt-long" : "text-crt-short")}>
                    {(p.realized_pnl_pct ?? 0) >= 0 ? "+" : ""}{p.realized_pnl_pct?.toFixed(2) ?? "0.00"}%
                  </span>
                </div>
                <div className="col-span-2 tabular text-[10px] uppercase tracking-[0.18em] text-bone-500">{p.exit_reason ?? "—"}</div>
              </div>
            ))}
          </div>
        </Section>
      )}
    </>
  );
}

// ---------------------------------------------------------------------------
// P&L tab
// ---------------------------------------------------------------------------

function PnlTab({ pnl }: { pnl: DayTradePnl | null }) {
  if (!pnl || pnl.records.length === 0) {
    return (
      <Section title="Day Trade P&L" subtitle="no completed day trades yet">
        <div className="px-6 py-12 text-center font-editorial text-lg italic text-bone-400">
          No completed day trades yet. P&L will appear here after the first trade closes.
        </div>
      </Section>
    );
  }

  return (
    <Section title="Day Trade P&L" subtitle="all completed day trades">
      <div className="flex flex-col">
        <GridHeader cols={["when", "ticker", "setup", "fill", "exit", "p&l $", "p&l %", "reason"]} spans={[2,1,3,1,1,2,1,1]} />
        {pnl.records.map((r) => (
          <div key={r.id} className="grid grid-cols-12 items-baseline gap-3 border-b border-ink-500/20 px-4 py-2.5 hover:bg-ink-800/30">
            <div className="col-span-2 tabular text-[11px] text-bone-400">{relativeTime(r.closed_at)}</div>
            <div className="col-span-1 font-editorial text-base italic text-bone-100">{r.ticker}</div>
            <div className="col-span-3 truncate text-[11px] text-bone-500">{r.setup ?? "—"}</div>
            <div className="col-span-1 tabular text-[11px] text-bone-300">${r.fill_price?.toFixed(2) ?? "—"}</div>
            <div className="col-span-1 tabular text-[11px] text-bone-300">${r.exit_price?.toFixed(2) ?? "—"}</div>
            <div className="col-span-2">
              <span className={clsx("tabular text-sm font-medium", (r.realized_pnl ?? 0) >= 0 ? "text-crt-long" : "text-crt-short")}>
                {(r.realized_pnl ?? 0) >= 0 ? "+" : ""}${r.realized_pnl?.toFixed(2) ?? "0.00"}
              </span>
            </div>
            <div className="col-span-1">
              <span className={clsx("tabular text-sm", (r.realized_pnl_pct ?? 0) >= 0 ? "text-crt-long" : "text-crt-short")}>
                {(r.realized_pnl_pct ?? 0) >= 0 ? "+" : ""}{r.realized_pnl_pct?.toFixed(2) ?? "0.00"}%
              </span>
            </div>
            <div className="col-span-1 tabular text-[10px] uppercase tracking-[0.18em] text-bone-500">{r.exit_reason ?? "—"}</div>
          </div>
        ))}
      </div>
    </Section>
  );
}

// ---------------------------------------------------------------------------
// Shared primitives
// ---------------------------------------------------------------------------

function Section({ title, subtitle, children }: { title: string; subtitle?: string; children: React.ReactNode }) {
  return (
    <section className="mb-6 border border-ink-500/40 bg-ink-900/20">
      <header className="border-b border-ink-500/30 px-4 py-3">
        <h3 className="font-editorial text-xl italic text-bone-100">{title}</h3>
        {subtitle && <div className="mt-0.5 text-[10px] uppercase tracking-[0.32em] text-bone-500">{subtitle}</div>}
      </header>
      {children}
    </section>
  );
}

function GridHeader({ cols, spans }: { cols: string[]; spans: number[] }) {
  return (
    <div className="grid grid-cols-12 gap-3 border-b border-ink-500/30 bg-ink-900/40 px-4 py-2 text-[9px] uppercase tracking-[0.32em] text-bone-500">
      {cols.map((c, i) => (
        <div key={c} className={`col-span-${spans[i]}`}>{c}</div>
      ))}
    </div>
  );
}

function StatTile({ label, value, valueClass, caption, highlight }: {
  label: string; value: string; valueClass?: string; caption?: string; highlight?: boolean;
}) {
  return (
    <div className={clsx("border bg-ink-900/40 px-4 py-3", highlight ? "border-crt-amber/40" : "border-ink-500/40")}>
      <div className="text-[9px] uppercase tracking-[0.32em] text-bone-500">{label}</div>
      <div className={clsx("mt-1 font-mono text-2xl", valueClass ?? "text-bone-50")}>{value}</div>
      {caption && <div className="mt-0.5 text-[10px] uppercase tracking-[0.18em] text-bone-500">{caption}</div>}
    </div>
  );
}
