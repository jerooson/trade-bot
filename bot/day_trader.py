"""
Day-trade engine — fully decoupled from swing trade logic.

Strategy (all times US/Eastern):
  - Read PLAN signals from logs/signals.jsonl (kind == "PLAN").
  - Poll adaptively (15 s normally, 5 s near a trigger/open position).
  - When price crosses the trigger, buy with a protected limit order capped
    slightly above the PLAN trigger; never chase a gap beyond that cap.  Entry
    orders use a stable idempotency ref and are cancelled if the breakout
    fails, the order times out, or only a partial fill is obtained.
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
FAR_POLL_INTERVAL_S = int(os.getenv("DAY_TRADE_FAR_POLL_INTERVAL_S", "15"))
NEAR_POLL_INTERVAL_S = int(os.getenv("DAY_TRADE_NEAR_POLL_INTERVAL_S", "5"))
NEAR_TRIGGER_PCT = float(os.getenv("DAY_TRADE_NEAR_TRIGGER_PCT", "0.5"))
ENTRY_LIMIT_OFFSET_PCT = float(os.getenv("DAY_TRADE_ENTRY_LIMIT_OFFSET_PCT", "0.2"))
ENTRY_ORDER_TTL_S = int(os.getenv("DAY_TRADE_ENTRY_ORDER_TTL_S", "30"))
SCHEDULER_TICK_S = float(os.getenv("DAY_TRADE_SCHEDULER_TICK_S", "1"))
SIGNALS_LOG = Path("logs/signals.jsonl")
POSITIONS_LOG = Path("logs/day_trade_positions.jsonl")
_RECONNECT_BACKOFF_S = (5, 10, 30)

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
    status: str = "watching"          # watching | pending_entry | open | pending_exit | closed | expired

    trigger_price: float | None = None
    target_price: float | None = None
    setup: str | None = None
    plan_signal_id: str | None = None
    plan_received_at: str = ""        # ISO

    # Execution details
    buy_order_id: str | None = None
    entry_limit_price: float | None = None
    entry_submitted_at: str | None = None
    entry_cancel_requested_at: str | None = None
    entry_cancel_reason: str | None = None
    entry_last_error: str | None = None
    entry_filled_qty: float = 0.0
    entry_filled_value: float = 0.0
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
    exit_order_id: str | None = None
    exit_requested_at: str | None = None
    exit_filled_qty: float = 0.0
    exit_filled_value: float = 0.0
    exit_last_error: str | None = None
    realized_pnl: float | None = None
    realized_pnl_pct: float | None = None
    closed_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "DayPosition":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class DayTraderRuntime:
    """Non-persistent connection and per-position scheduling state."""

    session: _MCPSession | None = field(default=None, repr=False)
    account_number: str | None = None
    next_due: dict[str, float] = field(default_factory=dict)
    consecutive_failures: int = 0
    retry_not_before: float = 0.0

    def connection(self) -> tuple[_MCPSession, str]:
        if self.session is None or self.account_number is None:
            self.session = _MCPSession(_load_token())
            self.account_number = _get_agentic_account(self.session)
        return self.session, self.account_number

    def due_positions(self, positions: list[DayPosition]) -> list[DayPosition]:
        now = time.monotonic()
        return [p for p in positions if now >= self.next_due.get(p.id, 0.0)]

    def schedule(self, pos: DayPosition) -> None:
        self.next_due[pos.id] = time.monotonic() + _position_poll_interval(pos)

    def can_request(self) -> bool:
        return time.monotonic() >= self.retry_not_before

    def record_success(self) -> None:
        self.consecutive_failures = 0
        self.retry_not_before = 0.0

    def record_failure(self) -> float:
        self.session = None
        self.account_number = None
        idx = min(self.consecutive_failures, len(_RECONNECT_BACKOFF_S) - 1)
        delay = float(_RECONNECT_BACKOFF_S[idx])
        self.consecutive_failures += 1
        self.retry_not_before = time.monotonic() + delay
        return delay


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

def _quote_price(item: dict[str, Any]) -> float | None:
    q = item.get("quote") or item
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
    return None


def _get_prices(session: _MCPSession, tickers: list[str]) -> dict[str, float]:
    """Fetch all due ticker quotes in one Robinhood request."""
    if not tickers:
        return {}
    symbols = list(dict.fromkeys(t.upper() for t in tickers))
    data = session.call("get_equity_quotes", symbols=symbols)
    results = data.get("data", {}).get("results", [])
    prices: dict[str, float] = {}
    for index, item in enumerate(results):
        q = item.get("quote") or item
        symbol = str(
            item.get("symbol")
            or q.get("symbol")
            or q.get("instrument_symbol")
            or ""
        ).upper()
        if not symbol and len(results) == len(symbols):
            # Robinhood preserves request order even when a response omits the
            # redundant symbol field.
            symbol = symbols[index]
        price = _quote_price(item)
        if symbol and price is not None:
            prices[symbol] = price
    return prices


def _get_agentic_account(session: _MCPSession) -> str:
    accounts_data = session.call("get_accounts")
    accounts = accounts_data.get("data", {}).get("accounts", [])
    agentic = [a for a in accounts if a.get("agentic_allowed")]
    if not agentic:
        raise RobinhoodMCPError("No Agentic account found")
    return agentic[0]["account_number"]


def _place_limit_buy(
    session: _MCPSession,
    account_number: str,
    ticker: str,
    usd: float,
    limit_price: float,
    ref_key: str,
) -> OrderResult:
    """Place a marketable dollar-based limit buy capped at ``limit_price``."""
    ref_id = str(uuid.uuid5(
        uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8"),
        f"dayentry:{ref_key}",
    ))
    order_kwargs: dict[str, Any] = {
        "account_number": account_number,
        "symbol": ticker,
        "side": "buy",
        "type": "limit",
        "time_in_force": "gfd",
        "dollar_amount": str(usd),   # API requires string, not number
        "limit_price": f"{limit_price:.2f}",
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
                if str(state).lower() in {
                    "filled", "partially_filled", "cancelled", "canceled",
                    "rejected", "failed", "expired",
                }:
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


def _entry_limit_price(trigger_price: float) -> float:
    """Maximum permitted entry price for a PLAN breakout."""
    return round(trigger_price * (1 + ENTRY_LIMIT_OFFSET_PCT / 100), 2)


def _poll_order(
    session: _MCPSession,
    account_number: str,
    ticker: str,
    order_id: str,
) -> OrderResult | None:
    """Return the latest state of a previously submitted equity order."""
    try:
        data = session.call("get_equity_orders", account_number=account_number, symbol=ticker)
        orders = data.get("data", {}).get("orders", [])
        order = next((item for item in orders if item.get("id") == order_id), None)
        if order is None:
            return None
        avg = order.get("average_price")
        qty = order.get("cumulative_quantity")
        fill_price = float(avg) if avg is not None else None
        fill_qty = float(qty) if qty is not None else None
        return OrderResult(
            order_id=order_id,
            state=order.get("state", "unknown"),
            fill_price=fill_price,
            fill_qty=fill_qty,
            fill_usd=round(fill_price * fill_qty, 4) if fill_price and fill_qty else None,
        )
    except Exception as exc:
        log.warning("Could not check order %s for %s: %s", order_id, ticker, exc)
        return None


def _place_stop_order(session: _MCPSession, account_number: str, ticker: str, qty: float, stop_price: float) -> str | None:
    """
    Attempt a stop_market sell order. Returns order_id or None.

    Robinhood does not support stop orders on fractional positions — in that
    case we return None and rely on the bot's fast price check to
    trigger a market-sell when the stop level is breached.
    """
    ref_id = str(uuid.uuid5(uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8"), f"daystop:{ticker}:{stop_price}"))
    try:
        resp = session.call(
            "place_equity_order",
            account_number=account_number,
            symbol=ticker,
            side="sell",
            type="stop_market",
            stop_price=str(round(stop_price, 2)),
            quantity=str(round(qty, 6)),
            time_in_force="gfd",
            ref_id=ref_id,
        )
        return resp.get("data", {}).get("order", {}).get("id") or resp.get("id")
    except RobinhoodMCPError as exc:
        msg = str(exc)
        if "fractional" in msg.lower() or "trigger" in msg.lower():
            log.info(
                "Broker stop not available for fractional %s — bot will manage stop internally at %.4f",
                ticker, stop_price,
            )
        else:
            log.error("Failed to place stop order for %s at %.2f: %s", ticker, stop_price, exc)
        return None
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
            limit_price=str(round(limit_price, 2)),
            quantity=str(round(qty, 6)),
            time_in_force="gfd",
            ref_id=ref_id,
        )
        return resp.get("data", {}).get("order", {}).get("id") or resp.get("id")
    except RobinhoodMCPError as exc:
        msg = str(exc)
        if "fractional" in msg.lower():
            log.info(
                "Broker limit sell not available for fractional %s — bot will manage target internally at %.4f",
                ticker, limit_price,
            )
        else:
            log.error("Failed to place limit sell for %s at %.2f: %s", ticker, limit_price, exc)
        return None
    except Exception as exc:
        log.error("Failed to place limit sell for %s at %.2f: %s", ticker, limit_price, exc)
        return None


def _cancel_order(session: _MCPSession, account_number: str, order_id: str) -> bool:
    try:
        session.call("cancel_equity_order", account_number=account_number, order_id=order_id)
        log.info("Cancelled order %s", order_id)
        return True
    except Exception as exc:
        log.warning("Could not cancel order %s: %s", order_id, exc)
        return False


def _market_sell_all(
    session: _MCPSession,
    account_number: str,
    ticker: str,
    qty: float,
    ref_key: str,
) -> OrderResult:
    """Submit a market sell and return its acknowledged broker state.

    ``ref_key`` is stable for the same logical exit attempt, so retrying after
    an ambiguous network failure remains idempotent at the broker boundary.
    """
    ref_id = str(uuid.uuid5(
        uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8"),
        f"dayclose:{ref_key}",
    ))
    response = session.call(
        "place_equity_order",
        account_number=account_number,
        symbol=ticker,
        side="sell",
        type="market",
        quantity=f"{qty:.6f}",
        time_in_force="gfd",
        ref_id=ref_id,
    )
    order = response.get("data", {}).get("order", {})
    order_id = order.get("id") or response.get("id", "")
    state = order.get("state") or response.get("state", "queued")
    if not order_id:
        raise RobinhoodMCPError(
            f"place_equity_order returned no exit order id for {ticker}"
        )
    average_price = order.get("average_price")
    cumulative_quantity = order.get("cumulative_quantity")
    fill_price = float(average_price) if average_price is not None else None
    fill_qty = (
        float(cumulative_quantity) if cumulative_quantity is not None else None
    )
    log.info(
        "Exit market-sell acknowledged for %s qty=%.6f order=%s state=%s",
        ticker,
        qty,
        order_id,
        state,
    )
    return OrderResult(
        order_id=order_id,
        state=state,
        fill_price=fill_price,
        fill_qty=fill_qty,
        fill_usd=(
            round(fill_price * fill_qty, 4)
            if fill_price is not None and fill_qty is not None
            else None
        ),
    )


_EXIT_TERMINAL_FAILURE_STATES = {
    "cancelled", "canceled", "rejected", "failed", "expired",
}
_QTY_EPSILON = 0.000001


def _remaining_exit_qty(pos: DayPosition) -> float:
    return max(0.0, float(pos.fill_qty or 0) - pos.exit_filled_qty)


def _finalize_exit(pos: DayPosition, fill_price: float, fill_qty: float) -> None:
    """Close a position using broker-confirmed cumulative sale proceeds."""
    total_qty = pos.exit_filled_qty + fill_qty
    total_value = pos.exit_filled_value + fill_price * fill_qty
    original_qty = float(pos.fill_qty or 0)
    if original_qty <= 0 or total_qty + _QTY_EPSILON < original_qty:
        raise ValueError(
            f"Cannot finalize {pos.ticker}: sold {total_qty} of {original_qty}"
        )
    pos.exit_filled_qty = total_qty
    pos.exit_filled_value = total_value
    pos.exit_price = total_value / total_qty
    pos.realized_pnl = round(
        total_value - float(pos.fill_price or 0) * total_qty,
        2,
    )
    pos.realized_pnl_pct = (
        round((pos.exit_price - pos.fill_price) / pos.fill_price * 100, 3)
        if pos.fill_price
        else None
    )
    pos.exit_order_id = None
    pos.exit_last_error = None
    pos.status = "closed"
    pos.closed_at = datetime.now(timezone.utc).isoformat()
    log.info(
        "Exit filled for %s reason=%s price=%.4f qty=%.6f pnl=%.2f",
        pos.ticker,
        pos.exit_reason,
        pos.exit_price,
        total_qty,
        pos.realized_pnl,
    )


def _apply_exit_order_result(pos: DayPosition, result: OrderResult) -> bool:
    """Apply an exit order update; return True when persisted state changed."""
    state = result.state.lower()
    if state == "filled" and result.fill_price is not None and result.fill_qty:
        _finalize_exit(pos, result.fill_price, result.fill_qty)
        return True

    if state in _EXIT_TERMINAL_FAILURE_STATES:
        if result.fill_price is not None and result.fill_qty:
            pos.exit_filled_qty += result.fill_qty
            pos.exit_filled_value += result.fill_price * result.fill_qty
        pos.exit_order_id = None
        remaining = _remaining_exit_qty(pos)
        if remaining <= _QTY_EPSILON and pos.exit_filled_qty > 0:
            _finalize_exit(pos, 0.0, 0.0)
        else:
            pos.exit_last_error = state
            log.error(
                "Exit order %s for %s ended state=%s; %.6f shares remain and will retry",
                result.order_id,
                pos.ticker,
                state,
                remaining,
            )
        return True

    # queued/new/confirmed/partially_filled remain broker-managed and are
    # checked again on the position's next five-second poll.
    return False


def _start_or_retry_exit(
    session: _MCPSession,
    account_number: str,
    pos: DayPosition,
    reason: str,
) -> bool:
    """Enter pending_exit and submit only the unsold remainder."""
    changed = False
    if pos.status != "pending_exit":
        pos.status = "pending_exit"
        pos.exit_reason = reason
        pos.exit_requested_at = datetime.now(timezone.utc).isoformat()
        pos.exit_last_error = None
        if pos.stop_order_id:
            _cancel_order(session, account_number, pos.stop_order_id)
            pos.stop_order_id = None
        if pos.limit_order_id:
            _cancel_order(session, account_number, pos.limit_order_id)
            pos.limit_order_id = None
        changed = True

    if pos.exit_order_id:
        return changed

    remaining = _remaining_exit_qty(pos)
    if remaining <= _QTY_EPSILON:
        if pos.exit_filled_qty > 0:
            _finalize_exit(pos, 0.0, 0.0)
        return True

    ref_key = f"{pos.id}:{pos.exit_filled_qty:.6f}"
    try:
        result = _market_sell_all(
            session,
            account_number,
            pos.ticker,
            remaining,
            ref_key,
        )
    except Exception as exc:
        pos.exit_last_error = str(exc)
        log.error(
            "Exit submission failed for %s; position remains pending_exit and will retry: %s",
            pos.ticker,
            exc,
        )
        return True

    pos.exit_order_id = result.order_id
    pos.exit_last_error = None
    _apply_exit_order_result(pos, result)
    return True


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


def _activate_filled_position(
    session: _MCPSession,
    account_number: str,
    pos: DayPosition,
    result: OrderResult,
) -> bool:
    """Move a final entry fill into managed open-position state."""
    if not result.fill_price or not result.fill_qty:
        return False

    pos.buy_order_id = result.order_id
    pos.fill_price = result.fill_price
    pos.fill_qty = result.fill_qty
    pos.entered_at = datetime.now(timezone.utc).isoformat()
    pos.status = "open"
    pos.high_water_mark = result.fill_price
    pos.stop_price = round(result.fill_price * (1 - _INITIAL_STOP_PCT / 100), 4)
    pos.stop_order_id = _place_stop_order(
        session, account_number, pos.ticker, result.fill_qty, pos.stop_price
    )
    if pos.target_price:
        pos.limit_order_id = _place_limit_sell(
            session, account_number, pos.ticker, result.fill_qty, pos.target_price
        )
    log.info(
        "Entered %s fill=%.4f stop=%.4f target=%s order=%s",
        pos.ticker,
        pos.fill_price,
        pos.stop_price,
        pos.target_price,
        pos.buy_order_id,
    )
    return True


_ENTRY_TERMINAL_FAILURE_STATES = {
    "cancelled", "canceled", "rejected", "failed", "expired",
}


def _record_entry_fill(pos: DayPosition, result: OrderResult) -> bool:
    """Persist the broker's cumulative entry fill without double counting."""
    if result.fill_price is None or not result.fill_qty:
        return False
    qty = float(result.fill_qty)
    value = float(result.fill_usd or result.fill_price * qty)
    if qty == pos.entry_filled_qty and value == pos.entry_filled_value:
        return False
    pos.entry_filled_qty = qty
    pos.entry_filled_value = value
    return True


def _request_entry_cancel(
    session: _MCPSession,
    account_number: str,
    pos: DayPosition,
    reason: str,
) -> bool:
    """Request cancellation once, while keeping the order pending until final."""
    if not pos.buy_order_id or pos.entry_cancel_requested_at:
        return False
    if not _cancel_order(session, account_number, pos.buy_order_id):
        pos.entry_last_error = f"cancel_failed:{reason}"
        return True
    pos.entry_cancel_requested_at = datetime.now(timezone.utc).isoformat()
    pos.entry_cancel_reason = reason
    pos.entry_last_error = None
    log.info(
        "Entry cancellation requested for %s order=%s reason=%s",
        pos.ticker,
        pos.buy_order_id,
        reason,
    )
    return True


def _apply_entry_order_result(
    session: _MCPSession,
    account_number: str,
    pos: DayPosition,
    result: OrderResult,
) -> bool:
    """Apply an entry update without exposing an unknown residual buy quantity."""
    changed = _record_entry_fill(pos, result)
    state = result.state.lower()

    if state == "filled":
        pos.entry_last_error = None
        return _activate_filled_position(
            session, account_number, pos, result
        ) or changed

    if state in _ENTRY_TERMINAL_FAILURE_STATES:
        # A cancelled order can still contain a legitimate partial fill.  Only
        # after the order is terminal do we know the final quantity to protect.
        if pos.entry_filled_qty > 0:
            fill_price = (
                pos.entry_filled_value / pos.entry_filled_qty
                if pos.entry_filled_value > 0
                else result.fill_price
            )
            if fill_price:
                final_result = OrderResult(
                    result.order_id,
                    state,
                    fill_price,
                    pos.entry_filled_qty,
                    pos.entry_filled_value or fill_price * pos.entry_filled_qty,
                )
                return _activate_filled_position(
                    session, account_number, pos, final_result
                ) or changed
        pos.status = "expired"
        pos.exit_reason = (
            f"entry_{pos.entry_cancel_reason}"
            if pos.entry_cancel_reason
            else f"entry_{state}"
        )
        pos.entry_last_error = state
        return True

    if pos.entry_filled_qty > 0:
        # Do not install protection for a moving quantity.  Cancel the
        # remainder first, then activate the final partial fill above.
        return _request_entry_cancel(
            session, account_number, pos, "partial_fill"
        ) or changed
    return changed


def _entry_order_timed_out(pos: DayPosition, now: datetime) -> bool:
    if not pos.entry_submitted_at:
        return False
    try:
        submitted = datetime.fromisoformat(pos.entry_submitted_at)
    except ValueError:
        return True
    if submitted.tzinfo is None:
        submitted = submitted.replace(tzinfo=timezone.utc)
    return (now.astimezone(timezone.utc) - submitted).total_seconds() >= ENTRY_ORDER_TTL_S


def _submit_or_recover_entry(
    session: _MCPSession,
    account_number: str,
    pos: DayPosition,
) -> bool:
    """Submit an entry with a stable ref, or recover its acknowledgement."""
    if pos.buy_order_id or pos.entry_limit_price is None:
        return False
    if not pos.entry_submitted_at:
        pos.entry_submitted_at = datetime.now(timezone.utc).isoformat()
    pos.status = "pending_entry"
    # Persist the intent before crossing the broker boundary.  A restart can
    # safely repeat this call because ``pos.id`` generates the same ref_id.
    _append_position(pos)
    try:
        result = _place_limit_buy(
            session,
            account_number,
            pos.ticker,
            DAY_TRADE_BUDGET_USD,
            pos.entry_limit_price,
            pos.id,
        )
    except Exception as exc:
        pos.entry_last_error = f"submission_ambiguous:{exc}"
        log.error(
            "Entry acknowledgement missing for %s; stable-ref recovery will retry: %s",
            pos.ticker,
            exc,
        )
        return True

    pos.buy_order_id = result.order_id
    pos.entry_last_error = None
    _apply_entry_order_result(session, account_number, pos, result)
    if pos.status == "pending_entry":
        log.info(
            "Entry order pending for %s at limit %.4f order=%s state=%s",
            pos.ticker,
            pos.entry_limit_price,
            result.order_id,
            result.state,
        )
    return True


def _position_poll_interval(pos: DayPosition) -> int:
    """Return this ticker's own polling interval without affecting others."""
    if pos.status in ("pending_entry", "open", "pending_exit"):
        return NEAR_POLL_INTERVAL_S
    if (
        pos.status == "watching"
        and pos.trigger_price
        and pos.current_price is not None
        and abs(pos.current_price / pos.trigger_price - 1) * 100 <= NEAR_TRIGGER_PCT
    ):
        return NEAR_POLL_INTERVAL_S
    return FAR_POLL_INTERVAL_S


def run_once(
    positions: list[DayPosition],
    seen_plan_ids: set[str],
    runtime: DayTraderRuntime | None = None,
) -> list[DayPosition]:
    """
    Execute one iteration of the day-trade loop.
    Modifies `positions` in-place and returns it.
    Raises nothing — all exceptions are caught and logged.
    """
    now = datetime.now(ET)

    # 1. Ingest new PLAN signals.
    new_plans = _load_new_plans(seen_plan_ids)
    for sig in new_plans:
        existing = [
            p for p in positions
            if p.ticker == sig.ticker and p.status in ("watching", "pending_entry", "open", "pending_exit")
        ]
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

    active = [
        pos for pos in positions
        if pos.status in ("watching", "pending_entry", "open", "pending_exit")
    ]
    due = runtime.due_positions(active) if runtime else active
    if not due or (runtime and not runtime.can_request()):
        return positions

    # 2. Reuse one MCP session/account and batch all due quote requests.
    try:
        if runtime:
            session, account_number = runtime.connection()
        else:
            session = _MCPSession(_load_token())
            account_number = _get_agentic_account(session)
        prices = _get_prices(session, [pos.ticker for pos in due])
        if runtime:
            runtime.record_success()
    except Exception as exc:
        if runtime:
            delay = runtime.record_failure()
            log.error("Robinhood batch quote failed; retrying in %.0fs: %s", delay, exc)
        else:
            log.error("Cannot open MCP session or fetch quotes: %s", exc)
        return positions

    force_close = _is_force_close(now)
    eod_tighten = _is_eod_tighten(now)
    changed = False

    for pos in due:
        price = prices.get(pos.ticker.upper())

        if price is not None:
            if pos.current_price != price:
                pos.current_price = price
                changed = True
            if pos.status == "watching":
                log.info(
                    "Poll %s price=%.4f trigger=%.4f gap=%.2f%%",
                    pos.ticker, price, pos.trigger_price or 0,
                    (price / pos.trigger_price - 1) * 100 if pos.trigger_price else 0,
                )
        else:
            log.warning("Batch quote missing %s; no new order decision", pos.ticker)

        if runtime:
            runtime.schedule(pos)

        # --- Resolve a limit entry that remained pending after submission. ---
        if pos.status == "pending_entry" and not pos.buy_order_id:
            if _submit_or_recover_entry(session, account_number, pos):
                changed = True

        if pos.status == "pending_entry" and pos.buy_order_id:
            result = _poll_order(session, account_number, pos.ticker, pos.buy_order_id)
            if result and _apply_entry_order_result(
                session, account_number, pos, result
            ):
                changed = True

            if pos.status == "pending_entry" and not pos.entry_cancel_requested_at:
                cancel_reason: str | None = None
                if force_close:
                    cancel_reason = "eod"
                elif price is not None and pos.trigger_price and price < pos.trigger_price:
                    cancel_reason = "lost_trigger"
                elif _entry_order_timed_out(pos, now):
                    cancel_reason = "timeout"
                if cancel_reason and _request_entry_cancel(
                    session, account_number, pos, cancel_reason
                ):
                    changed = True

        # --- Resolve or retry a broker-confirmed exit. ---
        if pos.status == "pending_exit":
            if pos.exit_order_id:
                result = _poll_order(
                    session, account_number, pos.ticker, pos.exit_order_id
                )
                if result is not None and _apply_exit_order_result(pos, result):
                    changed = True
            if pos.status == "pending_exit" and not pos.exit_order_id:
                if _start_or_retry_exit(
                    session,
                    account_number,
                    pos,
                    pos.exit_reason or "unknown",
                ):
                    changed = True
            continue

        # --- Force close all open day trades at 3:50 pm ET ---
        if force_close and pos.status == "open" and pos.fill_qty:
            log.info("Force-closing %s at EOD (price=%.4f)", pos.ticker, price or 0)
            if _start_or_retry_exit(session, account_number, pos, "eod"):
                changed = True
            continue

        # --- Expire watching plans at EOD ---
        if force_close and pos.status == "watching":
            pos.status = "expired"
            changed = True
            continue

        if force_close and pos.status == "pending_entry":
            # Cancellation has been requested above.  Do not mark this order
            # expired until Robinhood reports a terminal state; it may contain
            # a partial fill that must be sold at EOD.
            continue

        # --- Entry: watching -> protected limit order -> open ---
        if pos.status == "watching" and price is not None and pos.trigger_price is not None:
            if price >= pos.trigger_price:
                limit_price = _entry_limit_price(pos.trigger_price)
                pos.entry_limit_price = limit_price
                if price > limit_price:
                    log.warning(
                        "SKIP GAP: %s price=%.4f exceeds max entry %.4f (trigger=%.4f)",
                        pos.ticker, price, limit_price, pos.trigger_price,
                    )
                    pos.status = "expired"
                    pos.exit_reason = "entry_gap_above_limit"
                    changed = True
                    continue

                log.info(
                    "TRIGGER: %s price=%.4f >= trigger=%.4f — limit buying $%.0f at max %.4f",
                    pos.ticker, price, pos.trigger_price, DAY_TRADE_BUDGET_USD, limit_price,
                )
                if _submit_or_recover_entry(session, account_number, pos):
                    changed = True
            continue

        # --- Open position management ---
        if pos.status == "open" and price is not None and pos.fill_price:
            # Update high-water mark
            if pos.high_water_mark is None or price > pos.high_water_mark:
                pos.high_water_mark = price

            # A broker-managed stop owns the exit while it remains active.
            # Never place a second market sell simply because the quote crossed
            # the stop before the broker order status update reached us.
            if pos.stop_order_id:
                stop_result = _poll_order(
                    session, account_number, pos.ticker, pos.stop_order_id
                )
                if (
                    stop_result
                    and stop_result.state.lower() == "filled"
                    and stop_result.fill_price is not None
                    and stop_result.fill_qty
                ):
                    pos.exit_reason = "stop"
                    pos.exit_order_id = pos.stop_order_id
                    pos.stop_order_id = None
                    _finalize_exit(
                        pos, stop_result.fill_price, stop_result.fill_qty
                    )
                    changed = True
                    continue
                if (
                    stop_result
                    and stop_result.state.lower() in _EXIT_TERMINAL_FAILURE_STATES
                ):
                    log.warning(
                        "Broker stop %s for %s ended state=%s; reverting to bot-managed stop",
                        pos.stop_order_id,
                        pos.ticker,
                        stop_result.state,
                    )
                    pos.stop_order_id = None
                    changed = True
                elif pos.stop_price and price <= pos.stop_price:
                    pos.status = "pending_exit"
                    pos.exit_reason = "stop"
                    pos.exit_requested_at = datetime.now(timezone.utc).isoformat()
                    pos.exit_order_id = pos.stop_order_id
                    pos.stop_order_id = None
                    changed = True
                    continue

            # Check if stop was hit (for Robinhood-managed stop orders we
            # detect this by polling the order status, but as a safety net
            # we also check price directly).
            if pos.stop_price and price <= pos.stop_price:
                log.info("Stop triggered for %s price=%.4f stop=%.4f — market selling", pos.ticker, price, pos.stop_price)
                if _start_or_retry_exit(session, account_number, pos, "stop"):
                    changed = True
                continue

            # Bot-managed target check: sell when price hits Will's target.
            # (Robinhood rejects limit orders on fractional qty, so we monitor
            # the target via price polling and market-sell when hit.)
            if (
                pos.target_price
                and price >= pos.target_price
                and not pos.limit_order_id
            ):
                log.info(
                    "Target hit for %s: price=%.4f >= target=%.4f — market selling",
                    pos.ticker, price, pos.target_price,
                )
                if _start_or_retry_exit(session, account_number, pos, "target"):
                    changed = True
                continue

            # Also poll broker limit order if one was placed (for whole-share positions)
            if pos.limit_order_id:
                try:
                    od = session.call("get_equity_orders", account_number=account_number, symbol=pos.ticker)
                    orders = od.get("data", {}).get("orders", [])
                    tgt_order = next((o for o in orders if o.get("id") == pos.limit_order_id), None)
                    if tgt_order and tgt_order.get("state") == "filled":
                        fill_p = float(tgt_order.get("average_price") or price)
                        fill_q = float(
                            tgt_order.get("cumulative_quantity")
                            or pos.fill_qty
                            or 0
                        )
                        log.info("Limit order filled for %s at %.4f", pos.ticker, fill_p)
                        if pos.stop_order_id:
                            _cancel_order(session, account_number, pos.stop_order_id)
                        pos.exit_reason = "target"
                        pos.exit_order_id = pos.limit_order_id
                        pos.limit_order_id = None
                        pos.stop_order_id = None
                        _finalize_exit(pos, fill_p, fill_q)
                        changed = True
                        continue
                    if (
                        tgt_order
                        and str(tgt_order.get("state", "")).lower()
                        in _EXIT_TERMINAL_FAILURE_STATES
                    ):
                        log.warning(
                            "Broker target %s for %s ended state=%s; reverting to bot-managed target",
                            pos.limit_order_id,
                            pos.ticker,
                            tgt_order.get("state"),
                        )
                        pos.limit_order_id = None
                        changed = True
                    elif pos.target_price and price >= pos.target_price:
                        pos.status = "pending_exit"
                        pos.exit_reason = "target"
                        pos.exit_requested_at = datetime.now(timezone.utc).isoformat()
                        pos.exit_order_id = pos.limit_order_id
                        pos.limit_order_id = None
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
HEARTBEAT_FILE = Path("logs/day_trader.heartbeat")


def main() -> None:
    import atexit

    # Write PID file so API can detect the service is running.
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()))
    atexit.register(lambda: PID_FILE.unlink(missing_ok=True))

    log.info(
        "Day trader started. budget=$%.0f poll=%ds/%ds tick=%.1fs near=%.2f%% entry_cap=%.2f%% positions_log=%s",
        DAY_TRADE_BUDGET_USD,
        FAR_POLL_INTERVAL_S,
        NEAR_POLL_INTERVAL_S,
        SCHEDULER_TICK_S,
        NEAR_TRIGGER_PCT,
        ENTRY_LIMIT_OFFSET_PCT,
        POSITIONS_LOG,
    )
    positions = _load_positions()
    runtime = DayTraderRuntime()
    seen_plan_ids: set[str] = {
        p.plan_signal_id for p in positions if p.plan_signal_id
    }

    while True:
        try:
            run_once(positions, seen_plan_ids, runtime)
        except Exception as exc:
            log.exception("run_once error: %s", exc)
        # Heartbeat: touch file so the API can detect we're alive
        try:
            HEARTBEAT_FILE.touch()
        except Exception:
            pass
        time.sleep(SCHEDULER_TICK_S)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    main()
