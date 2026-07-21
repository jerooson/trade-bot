import { useState, type FormEvent } from "react";
import clsx from "clsx";
import type { DayTradePosition, DayTradePnl, HeatIdea, HeatSettings, ManualDayPlan, Signal } from "../lib/types";
import { approveHeatIdea, cancelManualDayPlan, createManualDayPlan, rejectHeatIdea, setHeatAutoTrading } from "../lib/api";
import { SignalsView, type DashLike } from "./SignalsView";
import { relativeTime } from "../lib/format";

type SubTab = "plans" | "heat" | "manual" | "active" | "pnl";
type HeatFilter = "review" | "swing" | "context" | "all";

interface Props {
  dash: DashLike;
  positions: DayTradePosition[];
  manualPlans: ManualDayPlan[];
  heatIdeas: HeatIdea[];
  heatSettings: HeatSettings;
  pnl: DayTradePnl | null;
  serviceRunning: boolean;
  onManualPlansChanged: () => Promise<void>;
}

export function DayTradeView({ dash, positions, manualPlans, heatIdeas, heatSettings, pnl, serviceRunning, onManualPlansChanged }: Props) {
  const [sub, setSub] = useState<SubTab>("plans");
  const approvedHeatIdeaIds = new Set(
    heatIdeas.filter((idea) => idea.decision === "approved").map((idea) => idea.id),
  );
  const visiblePositions = positions.filter(
    (position) => position.source !== "heat"
      || !position.heat_idea_id
      || approvedHeatIdeaIds.has(position.heat_idea_id),
  );

  const openCount = visiblePositions.filter((p) => p.status === "open" || p.status === "pending_exit").length;
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
          value={(visiblePositions.filter(p => p.status === "watching" || p.status === "pending_entry").length).toString()}
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
        {(["plans", "heat", "manual", "active", "pnl"] as SubTab[]).map((t) => (
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
            {t === "plans" ? "Signal Plans" : t === "heat" ? `Heat Ideas${heatIdeas.filter(i => i.decision === null && ["actionable_setup", "needs_level"].includes(i.classification)).length ? ` (${heatIdeas.filter(i => i.decision === null && ["actionable_setup", "needs_level"].includes(i.classification)).length})` : ""}` : t === "manual" ? "Manual Watches" : t === "active" ? "Active Trades" : "P&L"}
          </button>
        ))}
      </div>

      {sub === "plans" && <SignalsView dash={dash} />}
      {sub === "heat" && <HeatIdeasTab ideas={heatIdeas} settings={heatSettings} onChanged={onManualPlansChanged} />}
      {sub === "manual" && <ManualWatchTab plans={manualPlans} heatWatches={visiblePositions.filter((p) => p.source === "heat" && (p.status === "watching" || p.status === "pending_entry"))} onChanged={onManualPlansChanged} />}
      {sub === "active" && <ActiveTab positions={visiblePositions} heatIdeas={heatIdeas} signals={dash.signals} serviceRunning={serviceRunning} onChanged={onManualPlansChanged} />}
      {sub === "pnl" && <PnlTab pnl={pnl} />}
    </div>
  );
}

function HeatIdeasTab({ ideas, settings, onChanged }: {
  ideas: HeatIdea[];
  settings: HeatSettings;
  onChanged: () => Promise<void>;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<HeatFilter>("review");
  const [latestOnly, setLatestOnly] = useState(true);

  const counts = {
    review: ideas.filter((idea) => idea.decision === null && ["actionable_setup", "needs_level"].includes(idea.classification)).length,
    swing: ideas.filter((idea) => idea.decision !== "rejected" && idea.classification === "swing_dca").length,
    context: ideas.filter((idea) => idea.decision !== "rejected" && ["market_context", "position_update"].includes(idea.classification)).length,
    all: ideas.length,
  };
  const filteredIdeas = ideas.filter((idea) => {
    if (filter === "review") return idea.decision === null && ["actionable_setup", "needs_level"].includes(idea.classification);
    if (filter === "swing") return idea.decision !== "rejected" && idea.classification === "swing_dca";
    if (filter === "context") return idea.decision !== "rejected" && ["market_context", "position_update"].includes(idea.classification);
    return true;
  });
  const visibleIdeas = latestOnly
    ? filteredIdeas.filter((idea, index) => filteredIdeas.findIndex((candidate) => candidate.ticker === idea.ticker) === index)
    : filteredIdeas;

  async function toggle() {
    setBusy(true);
    setError(null);
    try {
      await setHeatAutoTrading(!settings.auto_trading_enabled);
      await onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-4">
      <Section title="Heat Automation" subtitle="explicit levels and confident chart levels auto-queue">
        <div className="flex flex-col gap-3 p-4 md:flex-row md:items-center md:justify-between">
          <div>
            <div className={clsx("text-sm uppercase tracking-[0.18em]", settings.auto_trading_enabled ? "text-crt-long" : "text-crt-short")}>
              {settings.auto_trading_enabled ? "Auto trading enabled" : "Auto trading paused"}
            </div>
            <div className="mt-1 max-w-3xl text-[11px] leading-5 text-bone-500">
              Source ticker triggers only. Liquid leveraged ETFs are preferred at entry; bullish ideas fall back to the underlying stock. Option show-and-tell is ignored.
            </div>
          </div>
          <button disabled={busy} onClick={toggle} className={clsx("border px-4 py-2 text-[10px] uppercase tracking-[0.2em] disabled:opacity-40", settings.auto_trading_enabled ? "border-crt-short/50 text-crt-short" : "border-crt-long/50 text-crt-long")}>
            {settings.auto_trading_enabled ? "Pause Heat Entries" : "Enable Heat Entries"}
          </button>
        </div>
      </Section>

      {error && <div className="border border-crt-short/50 bg-crt-short/10 px-4 py-3 text-sm text-crt-short">{error}</div>}

      <Section title="Heat Ideas" subtitle="newest first · images are saved locally before Discord links expire">
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-ink-500/30 bg-ink-950 p-3">
          <div className="flex flex-wrap gap-2">
            {(["review", "swing", "context", "all"] as HeatFilter[]).map((item) => (
              <button
                key={item}
                onClick={() => setFilter(item)}
                className={clsx(
                  "border px-2.5 py-1 text-[9px] uppercase tracking-[0.16em]",
                  filter === item ? "border-crt-amber/60 text-crt-amber" : "border-ink-500/60 text-bone-500",
                )}
              >
                {item === "review" ? "Trade Review" : item === "swing" ? "Swing / DCA" : item === "context" ? "Context" : "All"} ({counts[item]})
              </button>
            ))}
          </div>
          <button
            onClick={() => setLatestOnly((value) => !value)}
            className={clsx("border px-2.5 py-1 text-[9px] uppercase tracking-[0.16em]", latestOnly ? "border-crt-long/50 text-crt-long" : "border-ink-500/60 text-bone-500")}
          >
            {latestOnly ? "Latest per ticker" : "Full message log"}
          </button>
        </div>
        <div className="grid grid-cols-1 gap-px bg-ink-500/20 lg:grid-cols-2">
          {visibleIdeas.map((idea) => <HeatIdeaCard key={idea.id} idea={idea} onChanged={onChanged} />)}
          {visibleIdeas.length === 0 && <div className="col-span-full bg-ink-950 px-6 py-12 text-center font-editorial italic text-bone-400">No Heat items in this view.</div>}
        </div>
      </Section>
    </div>
  );
}

function HeatIdeaCard({ idea, onChanged }: { idea: HeatIdea; onChanged: () => Promise<void> }) {
  const [ticker, setTicker] = useState(idea.ticker ?? "");
  const [trigger, setTrigger] = useState(idea.trigger_price?.toString() ?? "");
  const [target, setTarget] = useState(idea.target_price?.toString() ?? "");
  const [setup, setSetup] = useState(idea.setup ?? idea.text ?? "");
  const [direction, setDirection] = useState<"long" | "short">(idea.direction ?? "long");
  const [triggerOperator, setTriggerOperator] = useState<"above" | "below">(idea.trigger_operator ?? "above");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const executed = idea.derived_status === "executed";
  const tradeReviewable = ["actionable_setup", "needs_level"].includes(idea.classification);
  const categoryLabel = {
    actionable_setup: "actionable",
    needs_level: "needs level",
    market_context: "market context",
    position_update: "position update",
    swing_dca: "swing / dca",
  }[idea.classification];

  async function approve() {
    const triggerPrice = Number(trigger);
    const targetPrice = target.trim() ? Number(target) : null;
    if (!ticker.trim() || !Number.isFinite(triggerPrice) || triggerPrice <= 0) {
      setError("Ticker and positive trigger are required.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await approveHeatIdea(idea.id, {
        ticker: ticker.trim().toUpperCase(),
        trigger_price: triggerPrice,
        target_price: targetPrice,
        setup: setup.trim() || null,
        direction,
        trigger_operator: triggerOperator,
      });
      await onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  async function reject() {
    setBusy(true);
    setError(null);
    try {
      await rejectHeatIdea(idea.id);
      await onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <article className="bg-ink-950 p-4">
      <div className="flex items-start justify-between gap-3">
        <div>
          <span className="font-editorial text-2xl italic text-bone-50">{idea.ticker}</span>
          <span className={clsx("ml-2 border px-1.5 py-0.5 text-[8px] uppercase tracking-[0.15em]", idea.auto_eligible ? "border-crt-long/40 text-crt-long" : "border-crt-amber/40 text-crt-amber")}>
            {categoryLabel}
          </span>
        </div>
        <div className="text-right text-[9px] uppercase tracking-[0.14em] text-bone-500">
          <div>{idea.derived_status.replaceAll("_", " ")}</div>
          <div className="mt-1 normal-case tracking-normal">{relativeTime(idea.created_at)}</div>
        </div>
      </div>

      <p className="mt-3 whitespace-pre-wrap text-[12px] leading-5 text-bone-300">{idea.text}</p>
      <div className="mt-2 text-[10px] uppercase tracking-[0.14em] text-bone-500">
        {idea.classification === "swing_dca"
          ? "Swing / DCA context — intraday automation disabled"
          : ["market_context", "position_update"].includes(idea.classification)
            ? "Context only — no trade approval"
            : idea.mapping_supported
              ? `Execution route: ${idea.leveraged_candidates.join(" / ")}`
              : "No supported execution route — review only"}
      </div>
      {idea.reply_text && <p className="mt-2 border-l border-ink-500 pl-3 text-[10px] leading-4 text-bone-500">Reply context: {idea.reply_text}</p>}

      {idea.attachment_urls.length > 0 && (
        <div className="mt-3 grid grid-cols-2 gap-2">
          {idea.attachment_urls.map((url) => <a key={url} href={url} target="_blank" rel="noreferrer"><img src={url} alt={`${idea.ticker} Heat chart`} className="max-h-72 w-full border border-ink-500/40 object-contain" /></a>)}
        </div>
      )}

      {idea.ticker_history.length > 0 && (
        <details className="mt-3 border border-ink-500/40 bg-ink-900/30 px-3 py-2">
          <summary className="cursor-pointer text-[9px] uppercase tracking-[0.16em] text-crt-amber">
            Earlier {idea.ticker} context ({idea.history_count})
          </summary>
          <div className="mt-2 space-y-2">
            {idea.ticker_history.map((previous) => (
              <div key={previous.id} className="border-l border-ink-500 pl-3 text-[10px] leading-4 text-bone-400">
                <div className="uppercase tracking-[0.12em] text-bone-600">
                  {relativeTime(previous.created_at)} · {previous.classification.replaceAll("_", " ")}
                  {previous.trigger_price != null ? ` · level ${previous.trigger_price}` : ""}
                </div>
                <div className="mt-1 whitespace-pre-wrap">{previous.text}</div>
                {previous.attachment_urls.length > 0 && (
                  <div className="mt-2 grid grid-cols-3 gap-2">
                    {previous.attachment_urls.map((url) => (
                      <a key={url} href={url} target="_blank" rel="noreferrer">
                        <img src={url} alt={`${idea.ticker} earlier Heat chart`} className="max-h-28 w-full border border-ink-500/40 object-contain" />
                      </a>
                    ))}
                  </div>
                )}
              </div>
            ))}
          </div>
        </details>
      )}
      {idea.classification === "needs_level" && idea.history_count === 0 && (
        <div className="mt-3 border-l border-crt-amber/50 pl-3 text-[10px] leading-4 text-bone-500">
          No earlier saved {idea.ticker} context. The listener does not backfill older Discord history.
        </div>
      )}

      {!executed && tradeReviewable && (
        <div className="mt-4 grid grid-cols-2 gap-2">
          <Field label="Ticker" value={ticker} onChange={setTicker} placeholder="GOOGL" span="" />
          <Field label="Trigger >" value={trigger} onChange={setTrigger} placeholder="360.00" type="number" span="" />
          <Field label="Target" value={target} onChange={setTarget} placeholder="optional" type="number" span="" />
          <Field label="Setup" value={setup} onChange={setSetup} placeholder="Heat chart breakout" span="" />
          <label>
            <span className="mb-1 block text-[9px] uppercase tracking-[0.18em] text-bone-500">Direction</span>
            <select value={direction} onChange={(e) => setDirection(e.target.value as "long" | "short")} className="h-[38px] w-full border border-ink-500/60 bg-ink-900/50 px-3 text-sm text-bone-100 outline-none">
              <option value="long">Bullish / long ETF</option>
              <option value="short">Bearish / inverse ETF</option>
            </select>
          </label>
          <label>
            <span className="mb-1 block text-[9px] uppercase tracking-[0.18em] text-bone-500">Trigger</span>
            <select value={triggerOperator} onChange={(e) => setTriggerOperator(e.target.value as "above" | "below")} className="h-[38px] w-full border border-ink-500/60 bg-ink-900/50 px-3 text-sm text-bone-100 outline-none">
              <option value="above">Crosses above</option>
              <option value="below">Breaks below</option>
            </select>
          </label>
        </div>
      )}

      {error && <div className="mt-2 text-[10px] text-crt-short">{error}</div>}
      {!executed && tradeReviewable && (
        <div className="mt-3 flex gap-2">
          <button disabled={busy} onClick={approve} className="border border-crt-long/50 px-3 py-1.5 text-[9px] uppercase tracking-[0.16em] text-crt-long disabled:opacity-40">Approve / Update</button>
          {idea.decision !== "rejected" && <button disabled={busy} onClick={reject} className="border border-crt-short/50 px-3 py-1.5 text-[9px] uppercase tracking-[0.16em] text-crt-short disabled:opacity-40">Reject</button>}
        </div>
      )}
      {!executed && !tradeReviewable && idea.decision !== "rejected" && (
        <div className="mt-3">
          <button disabled={busy} onClick={reject} className="border border-ink-500/60 px-3 py-1.5 text-[9px] uppercase tracking-[0.16em] text-bone-500 disabled:opacity-40">Dismiss</button>
        </div>
      )}
    </article>
  );
}

function ManualWatchTab({ plans, heatWatches, onChanged }: { plans: ManualDayPlan[]; heatWatches: DayTradePosition[]; onChanged: () => Promise<void> }) {
  const [ticker, setTicker] = useState("");
  const [trigger, setTrigger] = useState("");
  const [target, setTarget] = useState("");
  const [setup, setSetup] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    const triggerPrice = Number(trigger);
    const targetPrice = target.trim() ? Number(target) : null;
    if (!ticker.trim() || !Number.isFinite(triggerPrice) || triggerPrice <= 0) {
      setError("Enter a ticker and a positive trigger price.");
      return;
    }
    if (target.trim() && (!Number.isFinite(targetPrice) || (targetPrice ?? 0) <= 0)) {
      setError("Target must be a positive price when provided.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await createManualDayPlan({
        ticker: ticker.trim().toUpperCase(),
        trigger_price: triggerPrice,
        target_price: targetPrice,
        setup: setup.trim() || null,
      });
      setTicker("");
      setTrigger("");
      setTarget("");
      setSetup("");
      await onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  async function cancel(plan: ManualDayPlan) {
    setBusy(true);
    setError(null);
    try {
      await cancelManualDayPlan(plan.id);
      await onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  async function cancelHeat(position: DayTradePosition) {
    if (!position.heat_idea_id) return;
    setBusy(true);
    setError(null);
    try {
      await rejectHeatIdea(position.heat_idea_id);
      await onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-4">
      <Section title="Add Manual Watch" subtitle="good until executed or cancelled · max 25">
        <form onSubmit={submit} className="grid grid-cols-1 gap-3 p-4 md:grid-cols-12">
          <Field label="Ticker" value={ticker} onChange={setTicker} placeholder="GTLB" span="md:col-span-2" />
          <Field label="Breakout >" value={trigger} onChange={setTrigger} placeholder="34.06" type="number" span="md:col-span-2" />
          <Field label="Target (optional)" value={target} onChange={setTarget} placeholder="—" type="number" span="md:col-span-2" />
          <Field label="Setup / note" value={setup} onChange={setSetup} placeholder="Manual breakout watch" span="md:col-span-4" />
          <div className="flex items-end md:col-span-2">
            <button disabled={busy} className="h-[38px] w-full border border-crt-amber/60 bg-crt-amber/10 px-3 text-[10px] uppercase tracking-[0.2em] text-crt-amber disabled:opacity-40">
              {busy ? "Saving…" : "Add Watch"}
            </button>
          </div>
        </form>
        <div className="border-t border-ink-500/20 px-4 py-2 text-[10px] text-bone-500">
          New watches wait for price below the trigger before arming. Entry cap remains trigger +0.2%.
        </div>
      </Section>

      {error && <div className="border border-crt-short/50 bg-crt-short/10 px-4 py-3 text-sm text-crt-short">{error}</div>}

      <Section title="Manual Watches" subtitle="persistent across trading days">
        <div className="flex flex-col">
          <GridHeader cols={["ticker", "trigger / cap", "target", "setup", "status", "action"]} spans={[2,2,1,3,2,2]} />
          {plans.map((plan) => {
            const cancellable = !["executed", "cancelled"].includes(plan.derived_status);
            return (
              <div key={plan.id} className="grid grid-cols-12 items-center gap-3 border-b border-ink-500/20 px-4 py-3">
                <div className="col-span-2 font-editorial text-xl italic text-bone-50">{plan.ticker}</div>
                <div className="col-span-2 tabular text-sm text-crt-amber">
                  ${plan.trigger_price.toFixed(2)}
                  <span className="ml-1 text-[9px] text-bone-500">cap ${(plan.trigger_price * 1.002).toFixed(2)}</span>
                </div>
                <div className="col-span-1 tabular text-sm text-bone-300">{plan.target_price == null ? "—" : `$${plan.target_price.toFixed(2)}`}</div>
                <div className="col-span-3 truncate text-[11px] text-bone-400">{plan.setup ?? "—"}</div>
                <div className="col-span-2 text-[9px] uppercase tracking-[0.15em] text-bone-300">{plan.derived_status.replaceAll("_", " ")}</div>
                <div className="col-span-2">
                  {cancellable ? (
                    <button disabled={busy} onClick={() => cancel(plan)} className="border border-crt-short/40 px-2 py-1 text-[9px] uppercase tracking-[0.16em] text-crt-short disabled:opacity-40">Cancel</button>
                  ) : <span className="text-[9px] uppercase tracking-[0.16em] text-bone-600">{plan.derived_status}</span>}
                </div>
              </div>
            );
          })}
          {heatWatches.map((position) => (
            <div key={position.id} className="grid grid-cols-12 items-center gap-3 border-b border-ink-500/20 px-4 py-3">
              <div className="col-span-2 font-editorial text-xl italic text-bone-50">
                {position.ticker}
                <span className="ml-2 font-mono text-[9px] not-italic uppercase tracking-[0.14em] text-crt-long">Heat</span>
              </div>
              <div className="col-span-2 tabular text-sm text-crt-amber">
                ${position.trigger_price?.toFixed(2) ?? "—"}
                {position.trigger_price != null && <span className="ml-1 text-[9px] text-bone-500">cap ${(position.trigger_price * 1.002).toFixed(2)}</span>}
              </div>
              <div className="col-span-1 tabular text-sm text-bone-300">{position.signal_target_price == null ? "—" : `$${position.signal_target_price.toFixed(2)}`}</div>
              <div className="col-span-3 truncate text-[11px] text-bone-400">{position.setup ?? "Heat watch"}</div>
              <div className="col-span-2 text-[9px] uppercase tracking-[0.15em] text-bone-300">{position.status === "pending_entry" ? "entry pending" : position.armed ? "armed" : "waiting rearm"}</div>
              <div className="col-span-2">
                <button disabled={busy} onClick={() => cancelHeat(position)} className="border border-crt-short/40 px-2 py-1 text-[9px] uppercase tracking-[0.16em] text-crt-short disabled:opacity-40">Cancel Watch</button>
              </div>
            </div>
          ))}
          {plans.length === 0 && heatWatches.length === 0 && <div className="px-6 py-10 text-center font-editorial italic text-bone-400">No watches yet.</div>}
        </div>
      </Section>
    </div>
  );
}

function Field({ label, value, onChange, placeholder, type = "text", span }: {
  label: string; value: string; onChange: (value: string) => void; placeholder: string; type?: string; span: string;
}) {
  return (
    <label className={span}>
      <span className="mb-1 block text-[9px] uppercase tracking-[0.18em] text-bone-500">{label}</span>
      <input type={type} min={type === "number" ? "0" : undefined} step={type === "number" ? "0.01" : undefined} value={value} onChange={(e) => onChange(e.target.value)} placeholder={placeholder} className="h-[38px] w-full border border-ink-500/60 bg-ink-900/50 px-3 text-sm text-bone-100 outline-none focus:border-crt-amber/60" />
    </label>
  );
}

// ---------------------------------------------------------------------------
// Active trades tab
// ---------------------------------------------------------------------------

type TradeOrigin = {
  label: string;
  author: string | null;
  channel: string | null;
  reference: string | null;
  tone: "heat" | "manual" | "discord";
};

function tradeOrigin(position: DayTradePosition, heatIdeas: HeatIdea[], signals: Signal[]): TradeOrigin {
  if (position.source === "heat") {
    const idea = heatIdeas.find((item) => item.id === position.heat_idea_id);
    return {
      label: "Heat",
      author: idea?.discord?.author_name ?? "heat2030",
      channel: idea?.discord?.channel_name ?? null,
      reference: position.heat_idea_id ? `idea ${position.heat_idea_id}` : null,
      tone: "heat",
    };
  }
  if (position.source === "manual") {
    return {
      label: "Manual",
      author: "Dashboard watch",
      channel: null,
      reference: position.manual_plan_id ? `plan ${position.manual_plan_id}` : null,
      tone: "manual",
    };
  }
  const signal = signals.find((item) => String(item.discord?.message_id ?? "") === String(position.plan_signal_id ?? ""));
  return {
    label: "Discord plan",
    author: signal?.discord?.author_name ?? null,
    channel: signal?.discord?.channel_name ?? null,
    reference: position.plan_signal_id ? `message ${position.plan_signal_id}` : null,
    tone: "discord",
  };
}

function SourceBadge({ origin }: { origin: TradeOrigin }) {
  return (
    <span className={clsx(
      "inline-flex border px-1.5 py-0.5 text-[9px] uppercase tracking-[0.16em]",
      origin.tone === "heat" && "border-crt-long/50 bg-crt-long/10 text-crt-long",
      origin.tone === "manual" && "border-crt-amber/50 bg-crt-amber/10 text-crt-amber",
      origin.tone === "discord" && "border-bone-500/50 bg-bone-500/10 text-bone-300",
    )}>
      {origin.label}
    </span>
  );
}

function formatTradeTime(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    timeZoneName: "short",
  }).format(date);
}

function triggerCondition(position: DayTradePosition): string {
  const operator = position.trigger_operator === "below" ? "<=" : ">=";
  const trigger = position.trigger_price == null ? "—" : `$${position.trigger_price.toFixed(2)}`;
  return `${position.ticker} ${operator} ${trigger}`;
}

function originDetail(origin: TradeOrigin): string {
  return [origin.author, origin.channel, origin.reference].filter(Boolean).join(" · ") || origin.label;
}

function TradeMetric({ label, value, valueClass }: { label: string; value: string; valueClass?: string }) {
  return (
    <div>
      <div className="text-[8px] uppercase tracking-[0.18em] text-bone-600">{label}</div>
      <div className={clsx("mt-0.5 whitespace-nowrap tabular text-sm text-bone-300", valueClass)}>{value}</div>
    </div>
  );
}

function TradeDetail({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <span className="uppercase tracking-[0.14em] text-bone-600">{label}</span>
      <div className="mt-0.5 tabular leading-relaxed text-bone-400">{value}</div>
    </div>
  );
}

function ActiveTab({ positions, heatIdeas, signals, serviceRunning, onChanged }: {
  positions: DayTradePosition[];
  heatIdeas: HeatIdea[];
  signals: Signal[];
  serviceRunning: boolean;
  onChanged: () => Promise<void>;
}) {
  const [busyId, setBusyId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function cancelWatch(position: DayTradePosition) {
    if (!position.manual_plan_id && !position.heat_idea_id) return;
    setBusyId(position.id);
    setError(null);
    try {
      if (position.source === "heat" && position.heat_idea_id) {
        await rejectHeatIdea(position.heat_idea_id);
      } else if (position.manual_plan_id) {
        await cancelManualDayPlan(position.manual_plan_id);
      }
      await onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusyId(null);
    }
  }

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
      {error && <div className="mb-4 border border-crt-short/50 bg-crt-short/10 px-4 py-3 text-sm text-crt-short">{error}</div>}
      {watching.length > 0 && (
        <Section title="Watching" subtitle="waiting for trigger price to be crossed">
          <div className="flex flex-col">
            <GridHeader cols={["ticker", "trigger", "setup", "plan age", "status", "action"]} spans={[2,2,3,2,1,2]} />
            {watching.map((p) => {
              const origin = tradeOrigin(p, heatIdeas, signals);
              return (
                <div key={p.id} className="grid grid-cols-12 items-center gap-3 border-b border-ink-500/20 px-4 py-3 hover:bg-ink-800/30">
                  <div className="col-span-2">
                    <div className="font-editorial text-xl italic text-bone-50">
                      {p.ticker}{p.execution_ticker && <span className="ml-1 text-sm text-crt-long">→ {p.execution_ticker}</span>}
                    </div>
                    <div className="mt-1"><SourceBadge origin={origin} /></div>
                  </div>
                  <div className="col-span-2 tabular text-sm text-crt-amber">
                    {triggerCondition(p)}
                    {p.status === "pending_entry" && p.entry_limit_price != null && (
                      <div className="mt-1 text-[9px] text-bone-500">entry cap ${p.entry_limit_price.toFixed(2)}</div>
                    )}
                  </div>
                  <div className="col-span-3 text-[11px] leading-relaxed text-bone-400">
                    <div>{p.setup ?? "—"}</div>
                    <div className="mt-1 text-[9px] text-bone-600">{originDetail(origin)}</div>
                  </div>
                  <div className="col-span-2 tabular text-[11px] text-bone-500" title={formatTradeTime(p.plan_received_at)}>{relativeTime(p.plan_received_at)}</div>
                  <div className="col-span-1">
                    <span className="inline-flex items-center gap-1 border border-crt-amber/50 bg-crt-amber/10 px-1.5 py-0.5 text-[9px] uppercase tracking-[0.18em] text-crt-amber">
                      <span className="h-1.5 w-1.5 animate-pulseDot rounded-full bg-crt-amber" />
                      {p.status === "pending_entry" ? "limit pending" : p.armed === false ? "waiting rearm" : "watching"}
                    </span>
                  </div>
                  <div className="col-span-2">
                    {(p.manual_plan_id || p.heat_idea_id) && (
                      <button disabled={busyId === p.id} onClick={() => cancelWatch(p)} className="border border-crt-short/40 px-2 py-1 text-[9px] uppercase tracking-[0.16em] text-crt-short disabled:opacity-40">
                        {busyId === p.id ? "Cancelling…" : "Cancel Watch"}
                      </button>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        </Section>
      )}

      {open.length > 0 && (
        <Section title="Open Positions" subtitle="in market — every position includes its source and entry thesis">
          <div className="flex flex-col">
            {open.map((p) => {
              const pnlEst = p.fill_price && p.current_price
                ? ((p.current_price - p.fill_price) / p.fill_price * 100)
                : null;
              const origin = tradeOrigin(p, heatIdeas, signals);
              const executionTicker = p.execution_ticker ?? p.ticker;
              return (
                <div key={p.id} className="border-b border-ink-500/30 px-4 py-4 hover:bg-ink-800/30">
                  <div className="flex flex-col justify-between gap-4 md:flex-row md:items-start">
                    <div className="min-w-0">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="font-editorial text-2xl italic text-bone-50">{executionTicker}</span>
                        {executionTicker !== p.ticker && <span className="font-mono text-[10px] text-bone-500">signal {p.ticker}</span>}
                        <SourceBadge origin={origin} />
                        <span className={clsx(
                          "border px-1.5 py-0.5 text-[9px] uppercase tracking-[0.16em]",
                          p.status === "pending_exit"
                            ? "border-crt-short/50 bg-crt-short/10 text-crt-short"
                            : "border-crt-long/50 bg-crt-long/10 text-crt-long",
                        )}>
                          {p.status === "pending_exit" ? "exiting" : "open"}
                        </span>
                      </div>
                      <div className="mt-1 text-[10px] text-bone-500">{originDetail(origin)}</div>
                    </div>
                    <div className="grid grid-cols-3 gap-x-6 gap-y-2 md:grid-cols-5">
                      <TradeMetric label="Fill" value={p.fill_price == null ? "—" : `$${p.fill_price.toFixed(2)}`} />
                      <TradeMetric label="Current" value={p.current_price == null ? "—" : `$${p.current_price.toFixed(2)}`} />
                      <TradeMetric label="P&L est." value={pnlEst == null ? "—" : `${pnlEst >= 0 ? "+" : ""}${pnlEst.toFixed(2)}%`} valueClass={pnlEst == null ? undefined : pnlEst >= 0 ? "text-crt-long" : "text-crt-short"} />
                      <TradeMetric label="Stop" value={p.status === "pending_exit" ? `selling · ${p.exit_reason ?? "exit"}` : p.stop_price == null ? "—" : `$${p.stop_price.toFixed(2)}`} valueClass="text-crt-short" />
                      <TradeMetric label="High" value={p.high_water_mark == null ? "—" : `$${p.high_water_mark.toFixed(2)}`} />
                    </div>
                  </div>

                  <div className="mt-4 border border-ink-500/35 bg-ink-900/35 px-3 py-3">
                    <div className="text-[9px] uppercase tracking-[0.22em] text-crt-amber">Why this trade</div>
                    <div className="mt-1 text-sm leading-relaxed text-bone-200">{p.setup ?? "No setup note was supplied."}</div>
                    <div className="mt-3 grid gap-x-6 gap-y-2 text-[10px] md:grid-cols-4">
                      <TradeDetail label="Trigger" value={triggerCondition(p)} />
                      <TradeDetail label="Plan received" value={formatTradeTime(p.plan_received_at)} />
                      <TradeDetail label="Entered" value={formatTradeTime(p.entered_at)} />
                      <TradeDetail
                        label="Execution"
                        value={`${p.ticker} signal → ${executionTicker}${p.execution_leverage ? ` (${p.execution_leverage.toFixed(0)}x)` : ""}${p.entry_limit_price != null ? ` · cap $${p.entry_limit_price.toFixed(2)}` : ""}`}
                      />
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
        </Section>
      )}

      {open.length === 0 && watching.length === 0 && (
        <Section title="Active Trades" subtitle="no active day trades">
          <div className="px-6 py-12 text-center font-editorial text-lg italic text-bone-400">
            No active day trades. Waiting for a Discord signal or manual watch.
          </div>
        </Section>
      )}

      {closed.length > 0 && (
        <Section title="Closed Today" subtitle="completed day trades — source and thesis retained for review">
          <div className="flex flex-col">
            <GridHeader cols={["ticker / source", "trade reason", "fill → exit", "p&l $", "p&l %", "exit"]} spans={[2,3,2,2,1,2]} />
            {closed.map((p) => {
              const origin = tradeOrigin(p, heatIdeas, signals);
              const executionTicker = p.execution_ticker ?? p.ticker;
              return (
                <div key={p.id} className="grid grid-cols-12 items-center gap-3 border-b border-ink-500/20 px-4 py-3 hover:bg-ink-800/30">
                  <div className="col-span-2">
                    <div className="font-editorial text-xl italic text-bone-50">{executionTicker}</div>
                    <div className="mt-1 flex flex-wrap items-center gap-1.5">
                      <SourceBadge origin={origin} />
                      {executionTicker !== p.ticker && <span className="text-[9px] text-bone-600">signal {p.ticker}</span>}
                    </div>
                  </div>
                  <div className="col-span-3 text-[11px] leading-relaxed text-bone-400">
                    <div>{p.setup ?? "—"}</div>
                    <div className="mt-1 text-[9px] text-bone-600">{triggerCondition(p)}</div>
                  </div>
                  <div className="col-span-2 tabular text-sm text-bone-300">
                    ${p.fill_price?.toFixed(2) ?? "—"} → ${p.exit_price?.toFixed(2) ?? "—"}
                  </div>
                  <div className="col-span-2">
                    <span className={clsx("tabular text-sm font-medium", (p.realized_pnl ?? 0) >= 0 ? "text-crt-long" : "text-crt-short")}>
                      {(p.realized_pnl ?? 0) >= 0 ? "+" : ""}${p.realized_pnl?.toFixed(2) ?? "0.00"}
                    </span>
                  </div>
                  <div className="col-span-1">
                    <span className={clsx("tabular text-sm", (p.realized_pnl_pct ?? 0) >= 0 ? "text-crt-long" : "text-crt-short")}>
                      {(p.realized_pnl_pct ?? 0) >= 0 ? "+" : ""}{p.realized_pnl_pct?.toFixed(2) ?? "0.00"}%
                    </span>
                  </div>
                  <div className="col-span-2">
                    <div className="tabular text-[10px] uppercase tracking-[0.18em] text-bone-500">{p.exit_reason ?? "—"}</div>
                    <div className="mt-1 text-[9px] text-bone-600">{formatTradeTime(p.closed_at)}</div>
                  </div>
                </div>
              );
            })}
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
