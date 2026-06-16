"""
Day-trade engine — fully decoupled from swing trade logic.

Strategy (all times US/Eastern):
  - Read PLAN signals from logs/signals.jsonl (kind == "PLAN").
  - Every 60 s poll each ticker we're watching; buy $20 market order when
    price crosses the trigger level.
  - Stop-loss management after entry:
      * Initial stop: fill_price × 0.98  (-2 %)
      * Breakeven upgrade: when price holds ≥ fill × 1.03 for 2 consecutive
        polls → move stop to fill × 1.005 (entry + buffer)
      * Stepped trailing: each subsequent +3 % milestone confirmed by 2 polls
        locks in the previous level (fill×1.005 → fill×1.03 → fill×1.06 …)
  - Will's target: if PLAN has a target price, place a limit sell at that level
    immediately after entry (in addition to the stop).
  - EOD tightening: after 3:30 pm ET, tighten trailing to current × 0.99.
  - Force close: at 3:50 pm ET cancel all open orders for day trades and
    market-sell remaining positions.
  - Plan expiry: PLAN signals older than 1 trading day are discarded.

State is persisted to logs/day_trade_positions.jsonl so the dashboard can
read it and the process can restart cleanly.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from zoneinfo import ZoneInfo

from bot.parser import Signal, SignalKind, parse_message
from bot.robinhood_mcp_client import (
    OrderResult,
    RobinhoodMCPError,
    _load_token,
    _MCPSession,
)

log = logging.getLogger("bot.day_trader")

ET = ZoneInfo("America/New_York")

# ------------------------------------------------------------------
# Constants
# ------------------------------------------------------------------
DAY_TRADE_BUDGET_USD = float(os.getenv("DAY_TRADE_BUDGET_USD", "20"))
POLL_INTERVAL_S = int(os.getenv("DAY_TRADE_POLL_INTERVAL_S", "60"))
SIGNALS_LOG = Path("logs/signals.jsonl")
POSITIONS_LOG = Path("logs/day_trade_positions.jsonl")

# Trailing-stop milestones: list of (threshold_pct, lock_in_pct) pairs.
# "When price holds +threshold% for CONFIRM_POLLS, lock stop at +lock_in%."
_TRAILING_MILESTONES = [
    (3.0, 0.5),   # price +3% → lock stop at entry+0.5%
    (6.0, 3.0),   # price +6% → lock stop at entry+3%
    (9.0, 6.0),   # price +9% → lock stop at entry+6%
    (12.0, 9.0),  # price +12% → ...
    (15.0, 12.0),
    (18.0, 15.0),
    (21.0, 18.0),
]
_CONFIRM_POLLS = 2       # number of consecutive polls needed to confirm milestone
_INITIAL_STOP_PCT = 2.0
_EOD_TIGHT_HOUR = 15     # 3 pm ET
_EOD_TIGHT_MINUTE = 30
_FORCE_CLOSE_HOUR = 15
_FORCE_CLOSE_MINUTE = 50


# ------------------------------------------------------------------
# Position dataclass
# ------------------------------------------------------------------

@dataclass
class DayPosition:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    ticker: str = ""
    status: str = "watching"          # watching | open | closed | expired

    trigger_price: float | None = None
    target_price: float | None = None
    setup: str | None = None
    plan_signal_id: str | None = None
    plan_received_at: str = ""        # ISO

    # Execution details
    buy_order_id: str | None = None
    fill_price: float | None = None
    fill_qty: float | None = None
    entered_at: str | None = None

    # Stop / limit order IDs (for cancellation)
    stop_order_id: str | None = None
    limit_order_id: str | None = None

    # Trailing stop state
    stop_price: float | None = None
    high_water_mark: float | None = None
    current_price: float | None = None
    milestone_idx: int = 0            # next milestone to watch for
    confirm_count: int = 0            # consecutive polls at/above next milestone

    # EOD flag
    eod_tightened: bool = False

    # Close details
    exit_price: float | None = None
    exit_reason: str | None = None    # stop | target | eod | manual
    realized_pnl: float | None = None
    realized_pnl_pct: float | None = None
    closed_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "DayPosition":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


# ------------------------------------------------------------------
# Persistence helpers
# ------------------------------------------------------------------

def _load_positions() -> list[DayPosition]:
    if not POSITIONS_LOG.exists():
        return []
    positions: dict[str, DayPosition] = {}
    for line in POSITIONS_LOG.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
            pos = DayPosition.from_dict(d)
            positions[pos.id] = pos   # last-write-wins per id
        except Exception:
            pass
    return list(positions.values())


def _append_position(pos: DayPosition) -> None:
    POSITIONS_LOG.parent.mkdir(parents=True, exist_ok=True)
    with POSITIONS_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(pos.to_dict()) + "\n")


def _flush_positions(positions: list[DayPosition]) -> None:
    """Rewrite the full positions log (compact — one entry per id)."""
    POSITIONS_LOG.parent.mkdir(parents=True, exist_ok=True)
    with POSITIONS_LOG.open("w", encoding="utf-8") as f:
        for pos in positions:
            f.write(json.dumps(pos.to_dict()) + "\n")


# ------------------------------------------------------------------
# Signal ingestion — pick up new PLAN signals
# ------------------------------------------------------------------

def _load_new_plans(seen_ids: set[str]) -> list[Signal]:
    """Read signals.jsonl and return PLAN signals not yet seen."""
    if not SIGNALS_LOG.exists():
        return []
    plans: list[Signal] = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=1)

    for line in SIGNALS_LOG.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue

        sig_id = (
            d.get("id")
            or d.get("message_id")
            or str(d.get("discord", {}).get("message_id", ""))
            or ""
        )
        if sig_id in seen_ids:
            continue

        kind_val = d.get("kind", "")
        if kind_val != "PLAN":
            continue

        # Parse ISO timestamp
        ts_str = d.get("received_at") or d.get("ts", "")
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except Exception:
            ts = datetime.now(timezone.utc)

        if ts < cutoff:
            log.debug("Skipping expired PLAN for %s (age > 1 day)", d.get("ticker", "?"))
            seen_ids.add(sig_id)
            continue

        ticker = d.get("ticker", "").upper()
        trigger = d.get("trigger")
        target = d.get("target")
        setup = d.get("setup")
        if not ticker or trigger is None:
            seen_ids.add(sig_id)
            continue

        from bot.parser import Signal as _Signal, SignalKind as _SK, Side
        sig = _Signal(
            kind=_SK.PLAN,
            ticker=ticker,
            trigger=float(trigger),
            target=float(target) if target is not None else None,
            setup=setup,
            received_at=ts,
        )
        sig.message_id = sig_id  # type: ignore[attr-defined]
        plans.append(sig)
        seen_ids.add(sig_id)

    return plans


# ------------------------------------------------------------------
# Robinhood helpers (decoupled from swing shadow_reviewer)
# ------------------------------------------------------------------

def _get_price(session: _MCPSession, ticker: str) -> float | None:
    """Return latest last-trade price for ticker, or None on error."""
    try:
        data = session.call("get_equity_quotes", symbols=[ticker])
        results = data.get("data", {}).get("results", [])
        if results:
            # Quote data is nested: results[0]["quote"][field]
            q = results[0].get("quote") or results[0]
            ltp = q.get("last_trade_price")
            if ltp is not None:
                return float(ltp)
            ask = q.get("ask_price")
            bid = q.get("bid_price")
            if ask is not None and bid is not None:
                return (float(ask) + float(bid)) / 2
            if ask is not None:
                return float(ask)
            if bid is not None:
                return float(bid)
    except Exception as exc:
        log.warning("Price fetch failed for %s: %s", ticker, exc)
    return None


def _get_agentic_account(session: _MCPSession) -> str:
    accounts_data = session.call("get_accounts")
    accounts = accounts_data.get("data", {}).get("accounts", [])
    agentic = [a for a in accounts if a.get("agentic_allowed")]
    if not agentic:
        raise RobinhoodMCPError("No Agentic account found")
    return agentic[0]["account_number"]


def _place_market_buy(session: _MCPSession, account_number: str, ticker: str, usd: float) -> OrderResult:
    """Place a $usd market buy for ticker. Returns OrderResult."""
    ref_id = str(uuid.uuid5(uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8"), f"daytrader:{ticker}:{time.time()}"))
    order_kwargs: dict[str, Any] = {
        "account_number": account_number,
        "symbol": ticker,
        "side": "buy",
        "type": "market",
        "time_in_force": "gfd",
        "dollar_amount": usd,
        "ref_id": ref_id,
    }
    resp = session.call("place_equity_order", **order_kwargs)
    order_id = resp.get("data", {}).get("order", {}).get("id") or resp.get("id", "")
    state = resp.get("data", {}).get("order", {}).get("state") or resp.get("state", "queued")
    if not order_id:
        raise RobinhoodMCPError(f"place_equity_order returned no order id: {resp}")

    # Poll for fill
    fill_price: float | None = None
    fill_qty: float | None = None
    for _ in range(10):
        time.sleep(2)
        try:
            od = session.call("get_equity_orders", account_number=account_number, symbol=ticker)
            orders = od.get("data", {}).get("orders", [])
            match = next((o for o in orders if o.get("id") == order_id), None)
            if match:
                state = match.get("state", state)
                avg = match.get("average_price")
                qty = match.get("cumulative_quantity")
                if avg is not None and qty is not None:
                    fill_price = float(avg)
                    fill_qty = float(qty)
                    break
        except Exception:
            pass

    return OrderResult(
        order_id=order_id,
        state=state,
        fill_price=fill_price,
        fill_qty=fill_qty,
        fill_usd=round(fill_price * fill_qty, 4) if fill_price and fill_qty else None,
    )


def _place_stop_order(session: _MCPSession, account_number: str, ticker: str, qty: float, stop_price: float) -> str | None:
    """Place a stop-market sell order. Returns order_id or None."""
    ref_id = str(uuid.uuid5(uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8"), f"daystop:{ticker}:{stop_price}"))
    try:
        resp = session.call(
            "place_equity_order",
            account_number=account_number,
            symbol=ticker,
            side="sell",
            type="stop",
            stop_price=round(stop_price, 2),
            quantity=round(qty, 6),
            time_in_force="gfd",
            ref_id=ref_id,
        )
        return resp.get("data", {}).get("order", {}).get("id") or resp.get("id")
    except Exception as exc:
        log.error("Failed to place stop order for %s at %.2f: %s", ticker, stop_price, exc)
        return None


def _place_limit_sell(session: _MCPSession, account_number: str, ticker: str, qty: float, limit_price: float) -> str | None:
    """Place a limit sell order (for Will's target). Returns order_id or None."""
    ref_id = str(uuid.uuid5(uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8"), f"daylimit:{ticker}:{limit_price}"))
    try:
        resp = session.call(
            "place_equity_order",
            account_number=account_number,
            symbol=ticker,
            side="sell",
            type="limit",
            limit_price=round(limit_price, 2),
            quantity=round(qty, 6),
            time_in_force="gfd",
            ref_id=ref_id,
        )
        return resp.get("data", {}).get("order", {}).get("id") or resp.get("id")
    except Exception as exc:
        log.error("Failed to place limit sell for %s at %.2f: %s", ticker, limit_price, exc)
        return None


def _cancel_order(session: _MCPSession, account_number: str, order_id: str) -> None:
    try:
        session.call("cancel_equity_order", account_number=account_number, order_id=order_id)
        log.info("Cancelled order %s", order_id)
    except Exception as exc:
        log.warning("Could not cancel order %s: %s", order_id, exc)


def _market_sell_all(session: _MCPSession, account_number: str, ticker: str, qty: float) -> None:
    ref_id = str(uuid.uuid5(uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8"), f"dayclose:{ticker}:{time.time()}"))
    try:
        session.call(
            "place_equity_order",
            account_number=account_number,
            symbol=ticker,
            side="sell",
            type="market",
            quantity=round(qty, 6),
            time_in_force="gfd",
            ref_id=ref_id,
        )
        log.info("EOD market-sell placed for %s qty=%.4f", ticker, qty)
    except Exception as exc:
        log.error("EOD market-sell failed for %s: %s", ticker, exc)


# ------------------------------------------------------------------
# Core loop
# ------------------------------------------------------------------

def _now_et() -> datetime:
    return datetime.now(ET)


def _is_eod_tighten(now: datetime) -> bool:
    return (now.hour, now.minute) >= (_EOD_TIGHT_HOUR, _EOD_TIGHT_MINUTE)


def _is_force_close(now: datetime) -> bool:
    return (now.hour, now.minute) >= (_FORCE_CLOSE_HOUR, _FORCE_CLOSE_MINUTE)


def _is_market_hours(now: datetime) -> bool:
    if now.weekday() >= 5:
        return False
    return (9, 30) <= (now.hour, now.minute) <= (16, 0)


def run_once(positions: list[DayPosition], seen_plan_ids: set[str]) -> list[DayPosition]:
    """
    Execute one iteration of the day-trade loop.
    Modifies `positions` in-place and returns it.
    Raises nothing — all exceptions are caught and logged.
    """
    now = datetime.now(ET)

    # 1. Ingest new PLAN signals.
    new_plans = _load_new_plans(seen_plan_ids)
    for sig in new_plans:
        existing = [p for p in positions if p.ticker == sig.ticker and p.status in ("watching", "open")]
        if existing:
            log.info("Already watching/open %s — skip new PLAN", sig.ticker)
            continue
        pos = DayPosition(
            ticker=sig.ticker,
            trigger_price=sig.trigger,
            target_price=sig.target,
            setup=sig.setup,
            plan_signal_id=getattr(sig, "message_id", None),
            plan_received_at=sig.received_at.isoformat(),
        )
        positions.append(pos)
        _append_position(pos)
        log.info("New day-trade plan: %s trigger=%.2f target=%s", sig.ticker, sig.trigger or 0, sig.target)

    if not _is_market_hours(now):
        return positions

    # 2. Open MCP session once per iteration for all tickers.
    try:
        token = _load_token()
        session = _MCPSession(token)
        account_number = _get_agentic_account(session)
    except Exception as exc:
        log.error("Cannot open MCP session: %s", exc)
        return positions

    force_close = _is_force_close(now)
    eod_tighten = _is_eod_tighten(now)
    changed = False

    for pos in positions:
        if pos.status not in ("watching", "open"):
            continue

        try:
            price = _get_price(session, pos.ticker)
        except Exception as exc:
            log.warning("Price error for %s: %s", pos.ticker, exc)
            continue

        if price is not None:
            pos.current_price = price

        # --- Force close all open day trades at 3:50 pm ET ---
        if force_close and pos.status == "open" and pos.fill_qty:
            log.info("Force-closing %s at EOD (price=%.4f)", pos.ticker, price or 0)
            if pos.stop_order_id:
                _cancel_order(session, account_number, pos.stop_order_id)
            if pos.limit_order_id:
                _cancel_order(session, account_number, pos.limit_order_id)
            _market_sell_all(session, account_number, pos.ticker, pos.fill_qty)
            pos.status = "closed"
            pos.exit_reason = "eod"
            pos.exit_price = price
            if pos.fill_price and price:
                pos.realized_pnl = round((price - pos.fill_price) * (pos.fill_qty or 0), 2)
                pos.realized_pnl_pct = round((price - pos.fill_price) / pos.fill_price * 100, 3)
            pos.closed_at = datetime.now(timezone.utc).isoformat()
            changed = True
            continue

        # --- Expire watching plans at EOD ---
        if force_close and pos.status == "watching":
            pos.status = "expired"
            changed = True
            continue

        # --- Entry: watching → open ---
        if pos.status == "watching" and price is not None and pos.trigger_price is not None:
            if price >= pos.trigger_price:
                log.info(
                    "TRIGGER: %s price=%.4f >= trigger=%.4f — buying $%.0f",
                    pos.ticker, price, pos.trigger_price, DAY_TRADE_BUDGET_USD
                )
                try:
                    result = _place_market_buy(session, account_number, pos.ticker, DAY_TRADE_BUDGET_USD)
                except RobinhoodMCPError as exc:
                    log.error("Buy failed for %s: %s", pos.ticker, exc)
                    continue

                pos.buy_order_id = result.order_id
                pos.fill_price = result.fill_price
                pos.fill_qty = result.fill_qty
                pos.entered_at = datetime.now(timezone.utc).isoformat()
                pos.status = "open"
                pos.high_water_mark = result.fill_price

                if result.fill_price:
                    pos.stop_price = round(result.fill_price * (1 - _INITIAL_STOP_PCT / 100), 4)
                    # Place stop order
                    if pos.fill_qty:
                        pos.stop_order_id = _place_stop_order(
                            session, account_number, pos.ticker, pos.fill_qty, pos.stop_price
                        )
                    # Place limit sell at Will's target if provided
                    if pos.target_price and pos.fill_qty:
                        pos.limit_order_id = _place_limit_sell(
                            session, account_number, pos.ticker, pos.fill_qty, pos.target_price
                        )

                changed = True
                log.info(
                    "Entered %s fill=%.4f stop=%.4f target=%s order=%s",
                    pos.ticker,
                    pos.fill_price or 0,
                    pos.stop_price or 0,
                    pos.target_price,
                    pos.buy_order_id,
                )
            continue

        # --- Open position management ---
        if pos.status == "open" and price is not None and pos.fill_price:
            # Update high-water mark
            if pos.high_water_mark is None or price > pos.high_water_mark:
                pos.high_water_mark = price

            # Check if stop was hit (for Robinhood-managed stop orders we
            # detect this by polling the order status, but as a safety net
            # we also check price directly).
            if pos.stop_price and price <= pos.stop_price:
                log.info("Stop triggered for %s price=%.4f stop=%.4f", pos.ticker, price, pos.stop_price)
                if pos.limit_order_id:
                    _cancel_order(session, account_number, pos.limit_order_id)
                pos.status = "closed"
                pos.exit_reason = "stop"
                pos.exit_price = price
                pos.realized_pnl = round((price - pos.fill_price) * (pos.fill_qty or 0), 2)
                pos.realized_pnl_pct = round((price - pos.fill_price) / pos.fill_price * 100, 3)
                pos.closed_at = datetime.now(timezone.utc).isoformat()
                changed = True
                continue

            # Check if target limit order filled (poll order state)
            if pos.limit_order_id:
                try:
                    od = session.call("get_equity_orders", account_number=account_number, symbol=pos.ticker)
                    orders = od.get("data", {}).get("orders", [])
                    tgt_order = next((o for o in orders if o.get("id") == pos.limit_order_id), None)
                    if tgt_order and tgt_order.get("state") == "filled":
                        fill_p = float(tgt_order.get("average_price") or price)
                        log.info("Target filled for %s at %.4f", pos.ticker, fill_p)
                        if pos.stop_order_id:
                            _cancel_order(session, account_number, pos.stop_order_id)
                        pos.status = "closed"
                        pos.exit_reason = "target"
                        pos.exit_price = fill_p
                        pos.realized_pnl = round((fill_p - pos.fill_price) * (pos.fill_qty or 0), 2)
                        pos.realized_pnl_pct = round((fill_p - pos.fill_price) / pos.fill_price * 100, 3)
                        pos.closed_at = datetime.now(timezone.utc).isoformat()
                        changed = True
                        continue
                except Exception as exc:
                    log.warning("Could not check target order for %s: %s", pos.ticker, exc)

            # EOD trailing tighten (3:30 pm)
            if eod_tighten and not pos.eod_tightened:
                new_stop = round(price * 0.99, 4)
                if new_stop > (pos.stop_price or 0):
                    log.info("EOD tighten %s: stop %.4f → %.4f", pos.ticker, pos.stop_price or 0, new_stop)
                    # Cancel old stop and place new tighter one
                    if pos.stop_order_id:
                        _cancel_order(session, account_number, pos.stop_order_id)
                    if pos.fill_qty:
                        pos.stop_order_id = _place_stop_order(session, account_number, pos.ticker, pos.fill_qty, new_stop)
                    pos.stop_price = new_stop
                    pos.eod_tightened = True
                    changed = True
                continue  # Don't do trailing stop after EOD tighten

            # Stepped trailing stop (normal hours)
            milestones = _TRAILING_MILESTONES
            while pos.milestone_idx < len(milestones):
                threshold_pct, lock_in_pct = milestones[pos.milestone_idx]
                threshold_price = pos.fill_price * (1 + threshold_pct / 100)
                if price >= threshold_price:
                    pos.confirm_count += 1
                    if pos.confirm_count >= _CONFIRM_POLLS:
                        new_stop = round(pos.fill_price * (1 + lock_in_pct / 100), 4)
                        if new_stop > (pos.stop_price or 0):
                            log.info(
                                "Trail stop upgrade %s: +%.0f%% confirmed → stop %.4f → %.4f",
                                pos.ticker, threshold_pct, pos.stop_price or 0, new_stop
                            )
                            if pos.stop_order_id:
                                _cancel_order(session, account_number, pos.stop_order_id)
                            if pos.fill_qty:
                                pos.stop_order_id = _place_stop_order(
                                    session, account_number, pos.ticker, pos.fill_qty, new_stop
                                )
                            pos.stop_price = new_stop
                            pos.confirm_count = 0
                            pos.milestone_idx += 1
                            changed = True
                else:
                    pos.confirm_count = 0  # reset confirmation on dip
                break  # only watch next milestone

    if changed:
        _flush_positions(positions)

    return positions


PID_FILE = Path("logs/day_trader.pid")


def main() -> None:
    import atexit

    # Write PID file so API can detect the service is running.
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()))
    atexit.register(lambda: PID_FILE.unlink(missing_ok=True))

    log.info(
        "Day trader started. budget=$%.0f poll=%ds positions_log=%s",
        DAY_TRADE_BUDGET_USD, POLL_INTERVAL_S, POSITIONS_LOG,
    )
    positions = _load_positions()
    seen_plan_ids: set[str] = {
        p.plan_signal_id for p in positions if p.plan_signal_id
    }

    while True:
        try:
            run_once(positions, seen_plan_ids)
        except Exception as exc:
            log.exception("run_once error: %s", exc)
        time.sleep(POLL_INTERVAL_S)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    main()
