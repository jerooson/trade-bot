import clsx from "clsx";
import type { ActionKind, TradeAction } from "../lib/types";
import { fmtPrice, relativeTime } from "../lib/format";

interface Props {
  action: TradeAction;
}

const KIND_META: Record<
  ActionKind,
  { label: string; tone: "long" | "short" | "amber" | "info" | "violet" | "muted" }
> = {
  ENTRY:           { label: "ENTRY",         tone: "long" },
  ADD:             { label: "ADD",           tone: "info" },
  REDUCE:          { label: "REDUCE",        tone: "amber" },
  CLOSE:           { label: "CLOSE",         tone: "short" },
  STOP_TRIGGER:    { label: "STOP HIT",      tone: "short" },
  STOP_UPDATE:     { label: "STOP UPDATE",   tone: "violet" },
  POSITION_UPDATE: { label: "P/L UPDATE",    tone: "muted" },
};

const TONE_CLASSES: Record<string, { border: string; text: string; bg: string }> = {
  long:   { border: "border-crt-long/50",   text: "text-crt-long",   bg: "bg-crt-long/[0.06]"   },
  short:  { border: "border-crt-short/50",  text: "text-crt-short",  bg: "bg-crt-short/[0.06]"  },
  amber:  { border: "border-crt-amber/50",  text: "text-crt-amber",  bg: "bg-crt-amber/[0.06]"  },
  info:   { border: "border-crt-info/50",   text: "text-crt-info",   bg: "bg-crt-info/[0.06]"   },
  violet: { border: "border-crt-violet/50", text: "text-crt-violet", bg: "bg-crt-violet/[0.06]" },
  muted:  { border: "border-ink-500/40",    text: "text-bone-400",   bg: "bg-ink-900/30"        },
};

/**
 * A single trade action as a compact, scannable row.
 *
 * Layout (left to right):
 *   [time]  [KIND chip]  [TICKER]  [side]  [price/avg]  [size]  [stop]  [P/L]
 *
 * The kind chip carries the colour. Numeric fields are tabular so columns
 * stay readable when many rows stack.
 */
export function ActionRow({ action }: Props) {
  const meta = KIND_META[action.kind] ?? KIND_META.POSITION_UPDATE;
  const tone = TONE_CLASSES[meta.tone];
  const ts = action.discord?.created_at ?? action.received_at;

  return (
    <div
      className={clsx(
        "grid grid-cols-12 items-baseline gap-3 border-l-2 px-4 py-2.5 transition-colors hover:bg-ink-800/40",
        tone.border,
        tone.bg,
      )}
    >
      <span className="col-span-2 truncate text-[10px] uppercase tracking-[0.18em] text-bone-500">
        {relativeTime(ts)}
      </span>

      <span
        className={clsx(
          "col-span-2 inline-flex w-fit items-center border px-2 py-0.5 text-[10px] uppercase tracking-[0.32em]",
          tone.border,
          tone.text,
        )}
      >
        {meta.label}
      </span>

      <div className="col-span-2 flex items-baseline gap-2 min-w-0">
        <span className="font-editorial text-lg italic leading-none text-bone-50">
          {action.ticker}
        </span>
        {action.side && (
          <span
            className={clsx(
              "tabular text-[9px] uppercase tracking-[0.32em]",
              action.side === "LONG" ? "text-crt-long" : "text-crt-short",
            )}
          >
            {action.side}
          </span>
        )}
      </div>

      <div className="col-span-2 min-w-0">
        <FieldInline
          label={action.kind === "ADD" ? "fill / avg" : action.kind === "STOP_TRIGGER" ? "px / cost" : "price"}
          value={
            action.price != null && action.avg_cost != null && action.kind === "ADD"
              ? `$${fmtPrice(action.price)} → $${fmtPrice(action.avg_cost)}`
              : action.price != null
              ? `$${fmtPrice(action.price)}`
              : action.avg_cost != null
              ? `$${fmtPrice(action.avg_cost)}`
              : "—"
          }
        />
      </div>

      <div className="col-span-2 min-w-0">
        {/* For REDUCE rows the underlying message gives a DELTA (amount sold),
            not the new total -- show it as "−1/8" so it's visually clear it's
            a trim, not the new position size. ENTRY/ADD/POSITION_UPDATE rows
            use position_size directly. */}
        {action.position_size ? (
          <FieldInline label="size" value={action.position_size} accent />
        ) : action.delta_size ? (
          <FieldInline label="trim" value={`−${action.delta_size}`} tone="amber" />
        ) : (
          <FieldInline label="size" value="—" />
        )}
      </div>

      <div className="col-span-1 min-w-0">
        <FieldInline
          label="stop"
          value={
            action.stop_loss_label ??
            (action.stop_loss != null ? `$${fmtPrice(action.stop_loss)}` : "—")
          }
        />
      </div>

      <div className="col-span-1 text-right">
        {action.profit_pct != null && (
          <span
            className={clsx(
              "tabular text-sm font-semibold",
              action.profit_pct > 0 ? "text-crt-long" : action.profit_pct < 0 ? "text-crt-short" : "text-bone-300",
            )}
          >
            {action.profit_pct > 0 ? "+" : ""}
            {action.profit_pct.toFixed(1)}%
          </span>
        )}
      </div>
    </div>
  );
}

function FieldInline({
  label,
  value,
  accent,
  tone,
}: {
  label: string;
  value: string;
  accent?: boolean;
  tone?: "amber";
}) {
  return (
    <div className="min-w-0">
      <div className="text-[9px] uppercase tracking-[0.32em] text-bone-500">
        {label}
      </div>
      <div
        className={clsx(
          "tabular truncate text-[12px]",
          tone === "amber"
            ? "text-crt-amber"
            : accent
            ? "text-crt-amber"
            : "text-bone-200",
        )}
        title={value}
      >
        {value}
      </div>
    </div>
  );
}
