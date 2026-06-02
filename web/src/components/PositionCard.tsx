import clsx from "clsx";
import type { OpenPosition } from "../lib/types";
import { fmtPrice, relativeTime } from "../lib/format";

interface Props {
  pos: OpenPosition;
}

/**
 * A single open position from the trader's current book.
 *
 * Shows the most actionable bits: ticker, position size (e.g. "1/2"), avg
 * cost, last seen price, running P/L, and current stop. Tinted by P/L sign
 * so winners and losers are scannable at a glance.
 */
export function PositionCard({ pos }: Props) {
  const pnl = pos.last_pnl_pct;
  const isWinner = pnl != null && pnl > 0;
  const isLoser = pnl != null && pnl < 0;

  return (
    <article
      className={clsx(
        "relative border bg-ink-900/40 px-4 py-3 transition-colors",
        isWinner && "border-crt-long/40 bg-crt-long/[0.04]",
        isLoser && "border-crt-short/40 bg-crt-short/[0.04]",
        pnl == null && "border-ink-500/40",
      )}
    >
      <header className="flex items-baseline justify-between gap-2">
        <div className="flex items-baseline gap-2">
          <h4 className="font-editorial text-2xl italic leading-none text-bone-50">
            {pos.ticker}
          </h4>
          {pos.side && (
            <span
              className={clsx(
                "tabular text-[9px] uppercase tracking-[0.32em]",
                pos.side === "LONG" ? "text-crt-long" : "text-crt-short",
              )}
            >
              {pos.side}
            </span>
          )}
        </div>
        {pnl != null && (
          <span
            className={clsx(
              "tabular text-sm font-semibold",
              isWinner ? "text-crt-long" : isLoser ? "text-crt-short" : "text-bone-300",
            )}
          >
            {isWinner ? "+" : ""}
            {pnl.toFixed(1)}%
          </span>
        )}
      </header>

      <div className="mt-2 grid grid-cols-2 gap-x-3 gap-y-1.5 text-[11px]">
        <Field label="size" value={pos.position_size ?? "—"} highlight />
        <Field
          label="avg cost"
          value={pos.avg_cost != null ? `$${fmtPrice(pos.avg_cost)}` : "—"}
        />
        <Field
          label="last px"
          value={pos.last_price != null ? `$${fmtPrice(pos.last_price)}` : "—"}
        />
        <Field
          label="stop"
          value={pos.stop_loss_label ?? (pos.stop_loss != null ? `$${fmtPrice(pos.stop_loss)}` : "—")}
        />
      </div>

      <footer className="mt-2 flex items-baseline justify-between text-[9px] uppercase tracking-[0.18em] text-bone-500">
        <span>opened {relativeTime(pos.opened_at)}</span>
        <span className="tabular">last · {relativeTime(pos.last_action_at)}</span>
      </footer>
    </article>
  );
}

function Field({
  label,
  value,
  highlight,
}: {
  label: string;
  value: string;
  highlight?: boolean;
}) {
  return (
    <div className="min-w-0">
      <div className="text-[9px] uppercase tracking-[0.32em] text-bone-500">
        {label}
      </div>
      <div
        className={clsx(
          "tabular truncate",
          highlight ? "text-crt-amber" : "text-bone-200",
        )}
        title={value}
      >
        {value}
      </div>
    </div>
  );
}
