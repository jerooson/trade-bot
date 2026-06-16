"""
Per-trade P&L ledger.

Appends one JSON record to logs/trade_pnl.jsonl for every order that gets
a PLACED status.  Tracks average cost basis per ticker so realized P&L can
be calculated on REDUCE and CLOSE trades.

Record schema
-------------
{
  "timestamp":          "2026-06-16T16:07:40Z",   # UTC fill time
  "ticker":             "SNOW",
  "kind":               "ENTRY",                  # ENTRY / ADD / REDUCE / CLOSE
  "action":             "BUY",                    # BUY / SELL
  "order_id":           "6a3174cc-...",
  "signal_price":       237.0,                    # price from Discord signal
  "fill_price":         238.62,                   # actual Robinhood avg fill price
  "fill_qty":           0.027952,                 # shares filled
  "fill_usd":           6.67,                     # fill_price × fill_qty
  "avg_cost_before":    null,                     # avg cost of position before trade
  "avg_cost_after":     238.62,                   # avg cost after trade (null for sells)
  "position_qty_after": 0.027952,                 # shares held after trade
  "realized_pnl":       null,                     # dollars gained/lost (sells only)
  "realized_pnl_pct":   null                      # % gain/loss vs avg cost (sells only)
}
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bot.robinhood_mcp_client import OrderResult

log = logging.getLogger("bot.pnl_tracker")

DEFAULT_PNL_PATH = Path("logs/trade_pnl.jsonl")


# ---------------------------------------------------------------------------
# Position book helpers
# ---------------------------------------------------------------------------

def _load_position(ticker: str, path: Path) -> tuple[float, float]:
    """Return (avg_cost, qty_held) for ticker from the P&L ledger.

    Replays all records in order to derive current avg cost and position size.
    Returns (0.0, 0.0) if no history found.
    """
    avg_cost = 0.0
    qty = 0.0

    if not path.exists():
        return avg_cost, qty

    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("ticker") != ticker:
                continue

            fq = rec.get("fill_qty") or 0.0
            fp = rec.get("fill_price") or 0.0
            action = rec.get("action", "")

            if action == "BUY" and fq > 0 and fp > 0:
                # Weighted average cost update.
                total_cost = avg_cost * qty + fp * fq
                qty = qty + fq
                avg_cost = total_cost / qty if qty > 0 else fp
            elif action == "SELL" and fq > 0:
                qty = max(0.0, qty - fq)
                if qty <= 0:
                    avg_cost = 0.0
                # avg_cost unchanged for remaining shares (FIFO-equivalent for avg-cost method)

    return avg_cost, qty


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def record_trade(
    ticker: str,
    kind: str,
    action: str,
    signal_price: float | None,
    result: OrderResult,
    pnl_path: Path = DEFAULT_PNL_PATH,
) -> dict[str, Any]:
    """Append a P&L record and return it."""

    avg_cost_before, qty_before = _load_position(ticker, pnl_path)

    fill_price = result.fill_price
    fill_qty = result.fill_qty
    fill_usd = result.fill_usd

    avg_cost_after: float | None = None
    position_qty_after: float | None = None
    realized_pnl: float | None = None
    realized_pnl_pct: float | None = None

    if action == "BUY" and fill_qty and fill_price:
        total_cost = avg_cost_before * qty_before + fill_price * fill_qty
        new_qty = qty_before + fill_qty
        avg_cost_after = total_cost / new_qty if new_qty > 0 else fill_price
        position_qty_after = new_qty

    elif action == "SELL" and fill_qty and fill_price:
        if avg_cost_before > 0:
            realized_pnl = round((fill_price - avg_cost_before) * fill_qty, 4)
            realized_pnl_pct = round((fill_price - avg_cost_before) / avg_cost_before * 100, 2)
        position_qty_after = max(0.0, qty_before - fill_qty)
        avg_cost_after = avg_cost_before if position_qty_after > 0 else None

    record: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "ticker": ticker,
        "kind": kind,
        "action": action,
        "order_id": result.order_id,
        "signal_price": signal_price,
        "fill_price": round(fill_price, 6) if fill_price else None,
        "fill_qty": round(fill_qty, 6) if fill_qty else None,
        "fill_usd": round(fill_usd, 4) if fill_usd else None,
        "avg_cost_before": round(avg_cost_before, 6) if avg_cost_before else None,
        "avg_cost_after": round(avg_cost_after, 6) if avg_cost_after else None,
        "position_qty_after": round(position_qty_after, 6) if position_qty_after is not None else None,
        "realized_pnl": realized_pnl,
        "realized_pnl_pct": realized_pnl_pct,
    }

    pnl_path.parent.mkdir(parents=True, exist_ok=True)
    with pnl_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    if realized_pnl is not None:
        sign = "+" if realized_pnl >= 0 else ""
        log.info(
            "P&L %s %s: fill=%.4f avg_cost=%.4f qty=%.6f → realized %s$%.4f (%s%.2f%%)",
            kind, ticker, fill_price or 0, avg_cost_before,
            fill_qty or 0, sign, realized_pnl, sign, realized_pnl_pct or 0,
        )
    else:
        log.info(
            "P&L %s %s: fill=%.4f qty=%.6f avg_cost_after=%.4f",
            kind, ticker, fill_price or 0, fill_qty or 0, avg_cost_after or 0,
        )

    return record
