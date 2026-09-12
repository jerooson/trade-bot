"""
Reconciled performance report over the bot's own ledgers.

The dashboard used to show two numbers that could not be added together: the
day trader's "today only" realized total next to an all-history record list,
and a swing ledger whose sells were sized to the *account*, not the strategy.
This module produces one report with explicit scope, per-source and per-month
breakdowns, cent-precise P&L, open (last-quote) P&L, and a list of everything
that is missing or unreconciled so nobody reads the totals as account return.

Fill re-allocation
------------------
A swing SELL whose fill quantity exceeds the shares the swing strategy owned
at that moment sold somebody else's shares (2026-09-02 SPXL: 0.105993 sold,
0.035107 owned; the other 0.070886 were the day trader's).  When a day-trade
lifecycle in the same symbol lost exactly that many shares, the excess fill is
re-allocated to it here: swing P&L is recomputed on its own quantity and the
day trade gets the sale price against its own fill price.  Both sides are
labelled ``reallocated`` so the numbers can be traced back to the evidence.

Run
---
    python -m bot.performance            # text summary
    python -m bot.performance --json     # machine-readable
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from bot import position_ownership

ET = ZoneInfo("America/New_York")
QTY_EPSILON = 1e-6
_DAY_OPEN_STATUSES = {"open", "pending_exit"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read_jsonl(path: Path | None) -> list[dict[str, Any]]:
    if path is None or not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _parse_ts(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _month_of(raw: Any) -> str | None:
    dt = _parse_ts(raw)
    return dt.astimezone(ET).strftime("%Y-%m") if dt else None


def _round(value: float | None, places: int = 4) -> float | None:
    return None if value is None else round(float(value), places)


def summarize(pnls: Iterable[float]) -> dict[str, Any]:
    """Win/loss statistics for a list of realized P&L values."""
    values = [float(v) for v in pnls]
    wins = [v for v in values if v > 0]
    losses = [v for v in values if v < 0]
    gross_profit = sum(wins)
    gross_loss = -sum(losses)
    net = sum(values)
    return {
        "count": len(values),
        "wins": len(wins),
        "losses": len(losses),
        "flat": len(values) - len(wins) - len(losses),
        "gross_profit": _round(gross_profit),
        "gross_loss": _round(gross_loss),
        "net": _round(net),
        "avg": _round(net / len(values)) if values else None,
        "profit_factor": (
            _round(gross_profit / gross_loss, 3) if gross_loss > 0 else None
        ),
    }


def _group(rows: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    buckets: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        buckets[str(row.get(key) or "unknown")].append(float(row["realized_pnl"]))
    return {name: summarize(values) for name, values in sorted(buckets.items())}


# ---------------------------------------------------------------------------
# Swing ledger: replay fills, detect over-sales, re-allocate
# ---------------------------------------------------------------------------

def swing_ledger_rows(
    pnl_records: list[dict[str, Any]],
    day_positions: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (sell_rows, reallocations, omissions) from the swing fill ledger.

    Each sell row carries ``realized_pnl`` recomputed on the swing-owned
    quantity only.  Rows whose realized P&L cannot be derived (no fill, no
    cost basis) are listed in omissions rather than counted as zero.
    """
    ordered = sorted(pnl_records, key=lambda r: str(r.get("timestamp") or ""))
    owned: dict[str, float] = defaultdict(float)
    avg_cost: dict[str, float] = defaultdict(float)
    sells: list[dict[str, Any]] = []
    reallocations: list[dict[str, Any]] = []
    omissions: list[dict[str, Any]] = []
    day_claimed: set[str] = set()

    for rec in ordered:
        ticker = str(rec.get("ticker") or "").upper()
        qty = float(rec.get("fill_qty") or 0.0)
        price = float(rec.get("fill_price") or 0.0)
        action = rec.get("action")
        if action == "BUY":
            if qty <= 0 or price <= 0:
                omissions.append({
                    "kind": "swing_buy_without_fill",
                    "ticker": ticker,
                    "order_id": rec.get("order_id"),
                    "timestamp": rec.get("timestamp"),
                    "detail": "BUY recorded without fill quantity/price; cost basis unknown",
                })
                continue
            total_cost = avg_cost[ticker] * owned[ticker] + price * qty
            owned[ticker] += qty
            avg_cost[ticker] = total_cost / owned[ticker]
            continue
        if action != "SELL":
            continue
        if qty <= 0 or price <= 0:
            omissions.append({
                "kind": "swing_sell_without_fill",
                "ticker": ticker,
                "order_id": rec.get("order_id"),
                "timestamp": rec.get("timestamp"),
                "detail": "SELL recorded without a broker fill; realized P&L unknown",
            })
            continue
        own_qty = min(qty, owned[ticker])
        excess = round(qty - own_qty, 6)
        row: dict[str, Any] = {
            "timestamp": rec.get("timestamp"),
            "month": _month_of(rec.get("timestamp")),
            "ticker": ticker,
            "kind": rec.get("kind"),
            "order_id": rec.get("order_id"),
            "fill_price": price,
            "fill_qty": qty,
            "swing_qty": _round(own_qty, 6),
            "avg_cost": _round(avg_cost[ticker], 6) if own_qty > 0 else None,
            "recorded_realized_pnl": rec.get("realized_pnl"),
            "realized_pnl": None,
            "reallocated": False,
        }
        if own_qty > QTY_EPSILON and avg_cost[ticker] > 0:
            row["realized_pnl"] = _round((price - avg_cost[ticker]) * own_qty)
            row["realized_pnl_pct"] = _round(
                (price - avg_cost[ticker]) / avg_cost[ticker] * 100, 3
            )
        else:
            omissions.append({
                "kind": "swing_sell_without_cost_basis",
                "ticker": ticker,
                "order_id": rec.get("order_id"),
                "timestamp": rec.get("timestamp"),
                "detail": (
                    f"sold {qty:.6f} but the swing fill ledger holds no cost basis "
                    "(entry placed before P&L tracking, or not ours)"
                ),
            })
        owned[ticker] = max(0.0, owned[ticker] - own_qty)
        if owned[ticker] <= QTY_EPSILON:
            avg_cost[ticker] = 0.0

        if excess > QTY_EPSILON:
            allocation = _allocate_excess_to_day_trade(
                ticker, excess, price, rec, day_positions, day_claimed
            )
            if allocation:
                row["reallocated"] = True
                reallocations.append(allocation)
            else:
                omissions.append({
                    "kind": "swing_oversale_unallocated",
                    "ticker": ticker,
                    "order_id": rec.get("order_id"),
                    "timestamp": rec.get("timestamp"),
                    "detail": (
                        f"sold {excess:.6f} more than the swing strategy owned and no "
                        "day-trade lifecycle lost that quantity; needs broker records"
                    ),
                })
        if row["realized_pnl"] is not None or excess > QTY_EPSILON:
            sells.append(row)
    return sells, reallocations, omissions


def _allocate_excess_to_day_trade(
    ticker: str,
    excess: float,
    sale_price: float,
    sale: dict[str, Any],
    day_positions: list[dict[str, Any]],
    claimed: set[str],
) -> dict[str, Any] | None:
    """Match an over-sold quantity to the day trade that lost it."""
    sale_ts = _parse_ts(sale.get("timestamp"))
    for pos in day_positions:
        if str(pos.get("id") or "") in claimed:
            continue
        if position_ownership.day_position_symbol(pos) != ticker:
            continue
        lost = float(pos.get("unreconciled_qty") or 0.0)
        if pos.get("status") == "pending_exit" and lost <= 0:
            lost = max(
                0.0,
                float(pos.get("fill_qty") or 0.0) - float(pos.get("exit_filled_qty") or 0.0),
            )
        if lost <= QTY_EPSILON or abs(lost - excess) > 1e-4:
            continue
        entered = _parse_ts(pos.get("entered_at"))
        if sale_ts and entered and entered > sale_ts:
            continue
        fill_price = float(pos.get("fill_price") or 0.0)
        if fill_price <= 0:
            continue
        claimed.add(str(pos.get("id")))
        return {
            "ticker": ticker,
            "sale_order_id": sale.get("order_id"),
            "sale_timestamp": sale.get("timestamp"),
            "sale_price": sale_price,
            "qty": _round(excess, 6),
            "day_position_id": pos.get("id"),
            "day_source": pos.get("source"),
            "day_fill_price": fill_price,
            "day_realized_pnl": _round((sale_price - fill_price) * excess),
            "day_realized_pnl_pct": _round((sale_price - fill_price) / fill_price * 100, 3),
            "basis": "swing sell fill exceeded swing-owned shares by exactly this quantity",
        }
    return None


# ---------------------------------------------------------------------------
# Day ledger
# ---------------------------------------------------------------------------

def day_trade_rows(
    positions: list[dict[str, Any]],
    reallocations: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (closed_rows, open_rows, omissions) for day-trade lifecycles."""
    realloc_by_pos = {str(r.get("day_position_id")): r for r in reallocations}
    closed: list[dict[str, Any]] = []
    open_rows: list[dict[str, Any]] = []
    omissions: list[dict[str, Any]] = []

    for pos in positions:
        status = str(pos.get("status") or "")
        pos_id = str(pos.get("id") or "")
        fill_price = float(pos.get("fill_price") or 0.0)
        fill_qty = float(pos.get("fill_qty") or 0.0)
        base = {
            "id": pos_id,
            "ticker": pos.get("ticker"),
            "execution_symbol": position_ownership.day_position_symbol(pos),
            "source": pos.get("source") or "discord",
            "leverage": float(pos.get("execution_leverage") or 1.0),
            "leveraged": float(pos.get("execution_leverage") or 1.0) > 1.0,
            "exit_reason": pos.get("exit_reason"),
            "status": status,
            "cost_basis": _round(fill_price * fill_qty) if fill_price and fill_qty else None,
        }
        realloc = realloc_by_pos.get(pos_id)

        if status == "closed" and pos.get("realized_pnl") is not None:
            sold_qty = float(pos.get("exit_filled_qty") or 0.0)
            sold_value = float(pos.get("exit_filled_value") or 0.0)
            if sold_qty > 0 and sold_value > 0 and fill_price > 0:
                pnl = sold_value - fill_price * sold_qty
            else:
                pnl = float(pos.get("realized_pnl"))
            row = {
                **base,
                "closed_at": pos.get("closed_at"),
                "month": _month_of(pos.get("closed_at")),
                "realized_pnl": _round(pnl),
                "realized_pnl_pct": (
                    _round(pnl / (fill_price * sold_qty) * 100, 3)
                    if fill_price > 0 and sold_qty > 0
                    else pos.get("realized_pnl_pct")
                ),
                "reallocated": False,
                "partial": float(pos.get("unreconciled_qty") or 0.0) > QTY_EPSILON,
            }
            closed.append(row)
            if row["partial"]:
                omissions.append({
                    "kind": "day_partial_unreconciled",
                    "ticker": pos.get("ticker"),
                    "position_id": pos_id,
                    "detail": pos.get("reconciliation_note") or "part of the position was never sold by this lifecycle",
                })
            continue

        if status in _DAY_OPEN_STATUSES or status == "unreconciled":
            if realloc is not None:
                closed.append({
                    **base,
                    "closed_at": realloc.get("sale_timestamp"),
                    "month": _month_of(realloc.get("sale_timestamp")),
                    "realized_pnl": realloc["day_realized_pnl"],
                    "realized_pnl_pct": realloc["day_realized_pnl_pct"],
                    "reallocated": True,
                    "partial": False,
                    "exit_reason": (pos.get("exit_reason") or "unknown") + " (sold by swing strategy)",
                })
                continue
            if status == "unreconciled" or (
                status == "pending_exit" and pos.get("exit_last_error")
            ):
                omissions.append({
                    "kind": "day_unreconciled_exit",
                    "ticker": pos.get("ticker"),
                    "position_id": pos_id,
                    "detail": (
                        pos.get("reconciliation_note")
                        or f"exit blocked: {pos.get('exit_last_error')}"
                    ),
                })
                continue
            owned_qty = position_ownership.day_position_owned_qty(pos)
            current = pos.get("current_price")
            unrealized = (
                _round((float(current) - fill_price) * owned_qty)
                if current is not None and fill_price > 0 and owned_qty > 0
                else None
            )
            open_rows.append({
                **base,
                "qty": _round(owned_qty, 6),
                "fill_price": fill_price or None,
                "last_price": current,
                "unrealized_pnl": unrealized,
                "basis": "last polled execution-symbol quote; not a live mark",
            })
            continue

        if status == "closed" and pos.get("realized_pnl") is None:
            omissions.append({
                "kind": "day_closed_without_pnl",
                "ticker": pos.get("ticker"),
                "position_id": pos_id,
                "detail": "closed lifecycle carries no realized P&L",
            })
    return closed, open_rows, omissions


# ---------------------------------------------------------------------------
# Review ledger
# ---------------------------------------------------------------------------

def review_ledger_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = str(row.get("dedupe_key") or "")
        if key:
            latest[key] = row
    counts: dict[str, int] = defaultdict(int)
    unresolved: list[dict[str, Any]] = []
    for row in latest.values():
        status = str(row.get("status") or "").upper()
        counts[status] += 1
        if status in {"PENDING", "UNVERIFIED"}:
            unresolved.append({
                "status": status,
                "ticker": row.get("ticker"),
                "signal_kind": row.get("signal_kind"),
                "reviewed_at": row.get("reviewed_at"),
                "rationale": str(row.get("rationale") or "")[:200],
            })
    return {"latest_status_counts": dict(sorted(counts.items())), "unresolved": unresolved}


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def build_report(
    *,
    positions_path: Path | None = None,
    pnl_path: Path | None = None,
    reviews_path: Path | None = None,
    positions: list[dict[str, Any]] | None = None,
    pnl_records: list[dict[str, Any]] | None = None,
    review_rows: list[dict[str, Any]] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Assemble the reconciled report from ledgers (or pre-loaded rows)."""
    now = now or datetime.now(timezone.utc)
    if positions is None:
        positions = position_ownership.read_latest_day_positions(
            positions_path or position_ownership.DAY_POSITIONS_PATH
        )
    if pnl_records is None:
        pnl_records = _read_jsonl(pnl_path or position_ownership.SWING_PNL_PATH)
    if review_rows is None:
        review_rows = _read_jsonl(reviews_path)

    swing_sells, reallocations, swing_omissions = swing_ledger_rows(pnl_records, positions)
    day_closed, day_open, day_omissions = day_trade_rows(positions, reallocations)

    day_realized = [r for r in day_closed if r.get("realized_pnl") is not None]
    swing_realized = [r for r in swing_sells if r.get("realized_pnl") is not None]

    today_et = now.astimezone(ET).date().isoformat()
    day_today = [
        r for r in day_realized
        if (_parse_ts(r.get("closed_at")) or now).astimezone(ET).date().isoformat() == today_et
    ]

    heat_rows = [r for r in day_realized if r.get("source") == "heat"]
    day_section = {
        "scope": "all closed day-trade lifecycles (last record per position id)",
        "all_time": summarize(r["realized_pnl"] for r in day_realized),
        "today": summarize(r["realized_pnl"] for r in day_today),
        "by_source": _group(day_realized, "source"),
        "by_month": _group(day_realized, "month"),
        "by_exit_reason": _group(day_realized, "exit_reason"),
        "heat_by_leverage": {
            "leveraged": summarize(r["realized_pnl"] for r in heat_rows if r["leveraged"]),
            "unleveraged": summarize(r["realized_pnl"] for r in heat_rows if not r["leveraged"]),
            "note": "not a controlled comparison: symbols, dates and risk differ",
        },
        "open": day_open,
        "open_unrealized_pnl": _round(
            sum(float(r["unrealized_pnl"]) for r in day_open if r.get("unrealized_pnl") is not None)
        ) if day_open else None,
        "closed": sorted(day_realized, key=lambda r: str(r.get("closed_at") or ""), reverse=True),
    }
    swing_section = {
        "scope": (
            "broker-confirmed swing fills; each sell valued on the swing-owned "
            "quantity only (excess fills re-allocated to day trades)"
        ),
        "all_time": summarize(r["realized_pnl"] for r in swing_realized),
        "recorded_all_time": _round(
            sum(float(r.get("realized_pnl") or 0.0) for r in pnl_records if r.get("action") == "SELL")
        ),
        "by_month": _group(swing_realized, "month"),
        "by_ticker": _group(swing_realized, "ticker"),
        "sells": sorted(swing_realized, key=lambda r: str(r.get("timestamp") or ""), reverse=True),
        "reviews": review_ledger_summary(review_rows),
    }
    omissions = swing_omissions + day_omissions
    for item in swing_section["reviews"]["unresolved"]:
        omissions.append({"kind": f"review_{item['status'].lower()}", **item})

    day_net = day_section["all_time"]["net"] or 0.0
    swing_net = swing_section["all_time"]["net"] or 0.0
    return {
        "generated_at": now.isoformat(),
        "caveats": [
            "Bot ledgers only: not broker statements, no fees, no server/model costs, no cash-flow accounting.",
            "Realized figures include only sells with a known swing cost basis; see omissions.",
            "Open P&L uses the last polled quote for each position, not a live mark.",
            "Exit-reason buckets are outcome-conditioned and do not show whether a different rule would have done better.",
        ],
        "day": day_section,
        "swing": swing_section,
        "reallocations": reallocations,
        "omissions": omissions,
        "totals": {
            "day_realized": _round(day_net),
            "swing_realized_adjusted": _round(swing_net),
            "combined_realized": _round(day_net + swing_net),
            "day_open_unrealized": day_section["open_unrealized_pnl"],
            "omission_count": len(omissions),
        },
    }


# ---------------------------------------------------------------------------
# Text rendering / CLI
# ---------------------------------------------------------------------------

def _fmt_summary(name: str, s: dict[str, Any]) -> str:
    pf = "n/a" if s.get("profit_factor") is None else f"{s['profit_factor']:.2f}"
    net = 0.0 if s.get("net") is None else s["net"]
    avg = "n/a" if s.get("avg") is None else f"{s['avg']:+.4f}"
    return (
        f"  {name:<14} n={s['count']:<3} W/L={s['wins']}/{s['losses']:<3} "
        f"net={net:+.4f} avg={avg} PF={pf}"
    )


def format_report(report: dict[str, Any]) -> str:
    lines: list[str] = []
    day = report["day"]
    swing = report["swing"]
    totals = report["totals"]
    lines.append(f"Performance report  generated {report['generated_at']}")
    lines.append("")
    lines.append("DAY TRADES  (" + day["scope"] + ")")
    lines.append(_fmt_summary("all time", day["all_time"]))
    lines.append(_fmt_summary("today", day["today"]))
    for name, s in day["by_source"].items():
        lines.append(_fmt_summary(f"src:{name}", s))
    for name, s in day["by_month"].items():
        lines.append(_fmt_summary(name, s))
    for name, s in day["by_exit_reason"].items():
        lines.append(_fmt_summary(f"exit:{name}", s))
    if day["open"]:
        lines.append(f"  open positions: {len(day['open'])}  unrealized (last quote)="
                     f"{(day['open_unrealized_pnl'] or 0.0):+.4f}")
    lines.append("")
    lines.append("SWING  (" + swing["scope"] + ")")
    lines.append(_fmt_summary("adjusted", swing["all_time"]))
    lines.append(f"  recorded ledger total (unadjusted): {swing['recorded_all_time']:+.4f}")
    for name, s in swing["by_month"].items():
        lines.append(_fmt_summary(name, s))
    lines.append(f"  review ledger: {swing['reviews']['latest_status_counts']}")
    lines.append("")
    if report["reallocations"]:
        lines.append("FILL RE-ALLOCATIONS")
        for r in report["reallocations"]:
            lines.append(
                f"  {r['ticker']} {r['sale_timestamp']}: {r['qty']:.6f} sh sold by swing at "
                f"{r['sale_price']:.4f} belonged to day position {r['day_position_id']} "
                f"(fill {r['day_fill_price']:.4f}) -> day P&L {r['day_realized_pnl']:+.4f}"
            )
        lines.append("")
    lines.append(f"OMISSIONS ({len(report['omissions'])})")
    for item in report["omissions"]:
        lines.append(f"  [{item['kind']}] {item.get('ticker')}: {item.get('detail') or item.get('rationale')}")
    lines.append("")
    lines.append(
        f"TOTALS  day={totals['day_realized']:+.4f}  swing(adj)={totals['swing_realized_adjusted']:+.4f}  "
        f"combined={totals['combined_realized']:+.4f}"
    )
    lines.append("")
    for caveat in report["caveats"]:
        lines.append(f"* {caveat}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Reconciled trade-bot performance report")
    parser.add_argument("--positions", type=Path, default=Path("logs/day_trade_positions.jsonl"))
    parser.add_argument("--pnl", type=Path, default=Path("logs/trade_pnl.jsonl"))
    parser.add_argument("--reviews", type=Path, default=Path("logs/robinhood_shadow_reviews.jsonl"))
    parser.add_argument("--json", action="store_true", help="print the full JSON report")
    args = parser.parse_args(argv)
    report = build_report(
        positions_path=args.positions, pnl_path=args.pnl, reviews_path=args.reviews
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(format_report(report))


if __name__ == "__main__":
    main()
