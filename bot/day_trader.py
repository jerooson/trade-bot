"""
Day-trade engine — fully decoupled from swing trade logic.

Strategy (all times US/Eastern):
  - Read PLAN signals from logs/signals.jsonl (kind == "PLAN").
  - Poll adaptively (15 s normally, 5 s near a trigger/open position).
  - When price crosses the trigger, immediately re-check the executable ask
    and spread before placing a $20 fractional market order.  The preflight
    must remain below the PLAN entry cap; never chase a gap beyond that cap.
    Entry orders use a stable idempotency ref and are cancelled if the
    breakout fails, the order times out, or only a partial fill is obtained.
  - Stop-loss management after entry:
      * Initial stop: fill_price × 0.98  (-2 %)
      * Early risk reduction: confirmed +1 % moves the stop to -0.5 %;
        confirmed +2 % moves it to +0.2 %; confirmed +3 % locks +0.5 %.
      * Larger moves keep the existing stepped trail (+6 % locks +3 %,
        +9 % locks +6 %, and so on).
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

from bot.parser import Side, Signal, SignalKind, parse_message
from bot.heat_ideas import (
    HEAT_DECISIONS_PATH,
    HEAT_IDEAS_PATH,
    HEAT_SETTINGS_PATH,
    load_heat_settings,
    load_materialized_heat_ideas,
)
from bot.manual_day_plans import DEFAULT_PATH as MANUAL_PLANS_PATH, load_plans
from bot.leveraged_etfs import execution_candidates, result_by_symbol
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
ENTRY_MAX_SPREAD_PCT = float(os.getenv("DAY_TRADE_ENTRY_MAX_SPREAD_PCT", "0.2"))
LEVERAGED_ETF_MAX_SPREAD_PCT = float(
    os.getenv("DAY_TRADE_LEVERAGED_ETF_MAX_SPREAD_PCT", "0.2")
)
LEVERAGED_ETF_MIN_PRICE = float(
    os.getenv("DAY_TRADE_LEVERAGED_ETF_MIN_PRICE", "5")
)
LEVERAGED_ETF_MIN_AVG_VOLUME = float(
    os.getenv("DAY_TRADE_LEVERAGED_ETF_MIN_AVG_VOLUME", "1000000")
)
ENTRY_ORDER_TTL_S = int(os.getenv("DAY_TRADE_ENTRY_ORDER_TTL_S", "30"))
SCHEDULER_TICK_S = float(os.getenv("DAY_TRADE_SCHEDULER_TICK_S", "1"))
SIGNALS_LOG = Path("logs/signals.jsonl")
POSITIONS_LOG = Path("logs/day_trade_positions.jsonl")
MAX_HEAT_PLANS_PER_DAY = int(os.getenv("DAY_TRADE_MAX_HEAT_PLANS_PER_DAY", "3"))
_RECONNECT_BACKOFF_S = (5, 10, 30)

# Trailing-stop milestones: list of (threshold_pct, lock_in_pct) pairs.
# "When price holds +threshold% for CONFIRM_POLLS, lock stop at +lock_in%."
_TRAILING_MILESTONES = [
    (1.0, -0.5),  # price +1% → reduce initial risk from -2% to -0.5%
    (2.0, 0.2),   # price +2% → protect entry plus a small fill buffer
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
_STOP_POLICY_VERSION = 2
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
    source: str = "discord"           # discord | manual | heat
    manual_plan_id: str | None = None
    heat_idea_id: str | None = None
    direction: str = "long"           # long | short (economic direction)
    trigger_operator: str = "above"   # above | below on the signal ticker
    good_til_cancelled: bool = False
    armed: bool = True
    manual_cancel_requested: bool = False
    entry_attempt_no: int = 0

    # Heat signals can observe one instrument and trade another.  ``ticker``
    # always remains the source/trigger ticker for backwards compatibility.
    execution_ticker: str | None = None
    execution_leverage: float | None = None
    execution_spread_pct: float | None = None
    execution_selected_at: str | None = None
    signal_current_price: float | None = None
    signal_target_price: float | None = None

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
    confirm_milestone_idx: int | None = None
    stop_policy_version: int = _STOP_POLICY_VERSION

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
        values = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        # Records written before P2 used a different milestone index layout.
        if "stop_policy_version" not in d:
            values["stop_policy_version"] = 1
        return cls(**values)


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
        interval = _position_poll_interval(pos)
        now = time.monotonic()
        # Align equal-frequency tickers to the same boundary so 5, 10, or 25
        # watches still share one batched Robinhood quote request.
        self.next_due[pos.id] = (now // interval + 1) * interval

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
        side_raw = str(d.get("side") or "").upper()
        trigger = d.get("trigger")
        target = d.get("target")
        setup = d.get("setup")
        if not ticker or trigger is None:
            seen_ids.add(sig_id)
            continue

        # This service buys the underlying for Discord plans; it must never
        # reinterpret a SHORT alert as a long breakout.  Fail closed when the
        # side is missing or unsupported instead of relying on dataclass
        # defaults later in the execution path.
        if side_raw != Side.LONG.value:
            log.warning(
                "Skipping unsupported Discord PLAN: %s side=%s trigger=%s",
                ticker,
                side_raw or "missing",
                trigger,
            )
            seen_ids.add(sig_id)
            continue

        sig = Signal(
            kind=SignalKind.PLAN,
            ticker=ticker,
            side=Side.LONG,
            trigger=float(trigger),
            target=float(target) if target is not None else None,
            setup=setup,
            received_at=ts,
        )
        sig.message_id = sig_id  # type: ignore[attr-defined]
        plans.append(sig)
        seen_ids.add(sig_id)

    return plans


def _sync_manual_plans(positions: list[DayPosition]) -> bool:
    """Create/cancel persistent manual watches from the API-owned registry."""
    plans = {str(item.get("id")): item for item in load_plans(MANUAL_PLANS_PATH)}
    changed = False

    # Apply user cancellation to watches that have not become a position yet.
    for pos in positions:
        if pos.source != "manual" or not pos.manual_plan_id:
            continue
        plan = plans.get(pos.manual_plan_id)
        if plan and plan.get("status") == "active":
            continue
        if pos.status == "watching":
            pos.status = "expired"
            pos.exit_reason = "manual_cancel"
            pos.manual_cancel_requested = True
            changed = True
        elif pos.status == "pending_entry" and not pos.manual_cancel_requested:
            pos.manual_cancel_requested = True
            changed = True

    for plan_id, plan in plans.items():
        if plan.get("status") != "active":
            continue
        related = [p for p in positions if p.manual_plan_id == plan_id]
        if any(
            p.status in ("watching", "pending_entry", "open", "pending_exit", "closed")
            or bool(p.fill_qty)
            for p in related
        ):
            continue

        ticker = str(plan.get("ticker", "")).upper()
        trigger = plan.get("trigger_price")
        if not ticker or trigger is None:
            continue
        # One active day-trade lifecycle per ticker, regardless of source.
        if any(
            p.ticker == ticker
            and p.status in ("watching", "pending_entry", "open", "pending_exit")
            for p in positions
        ):
            continue

        pos = DayPosition(
            ticker=ticker,
            trigger_price=float(trigger),
            target_price=(
                float(plan["target_price"])
                if plan.get("target_price") is not None
                else None
            ),
            setup=plan.get("setup") or "Manual breakout watch",
            plan_signal_id=f"manual:{plan_id}",
            plan_received_at=plan.get("created_at") or datetime.now(timezone.utc).isoformat(),
            source="manual",
            manual_plan_id=plan_id,
            good_til_cancelled=True,
            # A new manual watch must first observe price below the trigger.
            # This prevents adding a watch above its trigger from buying now.
            armed=False,
        )
        positions.append(pos)
        _append_position(pos)
        log.info(
            "New manual day watch: %s trigger=%.4f target=%s plan=%s",
            pos.ticker,
            pos.trigger_price,
            pos.target_price,
            plan_id,
        )
        changed = True
    return changed


def _sync_heat_ideas(
    positions: list[DayPosition],
    *,
    now: datetime | None = None,
) -> bool:
    """Create and maintain persistent watches from approved Heat ideas.

    The settings switch controls creation and unfilled entries only.  Turning
    it off never abandons an already-open position; normal stop/EOD management
    remains active.
    """
    now_et = now or datetime.now(ET)
    settings = load_heat_settings(HEAT_SETTINGS_PATH)
    enabled = bool(settings.get("auto_trading_enabled"))
    ideas = load_materialized_heat_ideas(HEAT_IDEAS_PATH, HEAT_DECISIONS_PATH)
    by_id = {str(item.get("id")): item for item in ideas}
    changed = False

    for pos in positions:
        if pos.source != "heat" or not pos.heat_idea_id:
            continue
        idea = by_id.get(pos.heat_idea_id)
        approved = bool(idea and idea.get("decision") == "approved")
        direction = str((idea or {}).get("direction") or "").lower()
        route_supported = bool(
            idea
            and direction in {"long", "short"}
            and execution_candidates(str(idea.get("ticker") or ""), direction)
        )
        persistent = bool(idea and idea.get("good_til_cancelled", True))
        if enabled and approved and route_supported:
            if pos.good_til_cancelled != persistent:
                pos.good_til_cancelled = persistent
                changed = True
            # Legacy Heat watches expired at EOD. Reactivate only clean,
            # unfilled watches; submitted/partial orders keep their lifecycle.
            if (
                persistent
                and pos.status == "expired"
                and not pos.buy_order_id
                and not pos.fill_qty
                and pos.entry_filled_qty <= 0
            ):
                pos.status = "watching"
                pos.exit_reason = None
                pos.manual_cancel_requested = False
                pos.entry_cancel_requested_at = None
                pos.entry_cancel_reason = None
                pos.armed = False
                changed = True
            continue
        if pos.status == "watching":
            pos.status = "expired"
            if not enabled:
                pos.exit_reason = "heat_disabled"
            elif not approved:
                pos.exit_reason = "heat_rejected"
            else:
                pos.exit_reason = "heat_unsupported_mapping"
            pos.manual_cancel_requested = True
            changed = True
        elif pos.status == "pending_entry" and not pos.manual_cancel_requested:
            pos.manual_cancel_requested = True
            changed = True

    if not enabled:
        return changed

    created_today = 0
    for pos in positions:
        if pos.source != "heat":
            continue
        try:
            received = datetime.fromisoformat(pos.plan_received_at.replace("Z", "+00:00"))
            if received.astimezone(ET).date() == now_et.date():
                created_today += 1
        except (ValueError, TypeError):
            continue

    for idea in ideas:
        if idea.get("decision") != "approved":
            continue
        idea_id = str(idea.get("id", ""))
        if not idea_id or any(p.heat_idea_id == idea_id for p in positions):
            continue
        try:
            created = datetime.fromisoformat(
                str(idea.get("created_at", "")).replace("Z", "+00:00")
            )
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if created.astimezone(ET).date() != now_et.date():
            continue
        if created_today >= MAX_HEAT_PLANS_PER_DAY:
            log.warning("Heat daily plan cap reached (%d)", MAX_HEAT_PLANS_PER_DAY)
            break

        ticker = str(idea.get("ticker", "")).upper()
        trigger = idea.get("trigger_price")
        direction = str(idea.get("direction") or "").lower()
        trigger_operator = str(idea.get("trigger_operator") or "above").lower()
        if (
            not ticker
            or trigger is None
            or direction not in {"long", "short"}
            or trigger_operator not in {"above", "below"}
        ):
            continue
        if not execution_candidates(ticker, direction):
            log.warning(
                "Heat idea %s cannot queue: no supported execution route for %s %s",
                idea_id,
                ticker,
                direction,
            )
            continue
        if any(
            p.ticker == ticker
            and p.status in ("watching", "pending_entry", "open", "pending_exit")
            for p in positions
        ):
            continue

        pos = DayPosition(
            ticker=ticker,
            trigger_price=float(trigger),
            # Heat's target belongs to the source ticker.  If supplied, it is
            # converted to an ETF-relative target only after the ETF fills.
            target_price=None,
            signal_target_price=(
                float(idea["target_price"])
                if idea.get("target_price") is not None
                else None
            ),
            setup=idea.get("setup") or "Heat breakout watch",
            plan_signal_id=f"heat:{idea_id}",
            plan_received_at=created.isoformat(),
            source="heat",
            heat_idea_id=idea_id,
            direction=direction,
            trigger_operator=trigger_operator,
            good_til_cancelled=bool(idea.get("good_til_cancelled", True)),
            # Observe price below the trigger before accepting a new breakout.
            armed=False,
        )
        positions.append(pos)
        _append_position(pos)
        created_today += 1
        changed = True
        log.info(
            "New Heat day watch: %s trigger=%.4f target=%s idea=%s",
            pos.ticker, pos.trigger_price, pos.target_price, idea_id,
        )
    return changed


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


class EntryPreflightRejected(Exception):
    """A fresh quote no longer meets the protected-entry requirements."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason


class EntryPreflightUnavailable(Exception):
    """A fresh executable quote is temporarily unavailable."""


def _validate_entry_preflight(
    session: _MCPSession,
    ticker: str,
    trigger_price: float,
    max_price: float,
    trigger_operator: str = "above",
) -> tuple[float, float, float, float]:
    """Return last/bid/ask/spread after enforcing the final entry guard."""
    data = session.call("get_equity_quotes", symbols=[ticker])
    results = data.get("data", {}).get("results", [])
    if not results:
        raise EntryPreflightUnavailable(f"No fresh quote for {ticker}")
    quote = results[0].get("quote") or results[0]
    try:
        last = float(quote["last_trade_price"])
        bid = float(quote["bid_price"])
        ask = float(quote["ask_price"])
    except (KeyError, TypeError, ValueError) as exc:
        raise EntryPreflightUnavailable(f"Incomplete fresh quote for {ticker}") from exc
    if last <= 0 or bid <= 0 or ask <= 0 or ask < bid:
        raise EntryPreflightUnavailable(
            f"Invalid fresh quote for {ticker}: last={last} bid={bid} ask={ask}",
        )
    midpoint = (ask + bid) / 2
    spread_pct = (ask - bid) / midpoint * 100
    if trigger_operator == "above" and last < trigger_price:
        raise EntryPreflightRejected(
            "lost_trigger",
            f"{ticker} last {last:.4f} fell below trigger {trigger_price:.4f}",
        )
    if trigger_operator == "below" and last > trigger_price:
        raise EntryPreflightRejected(
            "lost_trigger",
            f"{ticker} last {last:.4f} rose above trigger {trigger_price:.4f}",
        )
    if trigger_operator == "above" and ask > max_price:
        raise EntryPreflightRejected(
            "ask_above_cap",
            f"{ticker} ask {ask:.4f} exceeds entry cap {max_price:.4f}",
        )
    if trigger_operator == "below" and bid < max_price:
        raise EntryPreflightRejected(
            "bid_below_floor",
            f"{ticker} bid {bid:.4f} is below entry floor {max_price:.4f}",
        )
    if spread_pct > ENTRY_MAX_SPREAD_PCT:
        raise EntryPreflightRejected(
            "spread_too_wide",
            f"{ticker} spread {spread_pct:.3f}% exceeds {ENTRY_MAX_SPREAD_PCT:.3f}%",
        )
    return last, bid, ask, spread_pct


@dataclass(frozen=True)
class LeveragedETFSelection:
    ticker: str
    leverage: float
    last: float
    bid: float
    ask: float
    spread_pct: float
    volume: float | None


def _float_or_none(value: object) -> float | None:
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _select_leveraged_etf(
    session: _MCPSession,
    account_number: str,
    source_ticker: str,
    direction: str,
) -> LeveragedETFSelection:
    """Choose a liquid ETF route, falling back to the underlying for longs."""
    candidates = execution_candidates(source_ticker, direction)
    if not candidates:
        raise EntryPreflightRejected(
            "unsupported_leveraged_mapping",
            f"No supported execution route for {source_ticker} {direction}",
        )
    symbols = [candidate.ticker for candidate in candidates]
    quote_data = session.call("get_equity_quotes", symbols=symbols)
    quote_rows = quote_data.get("data", {}).get("results", [])
    quotes = result_by_symbol(quote_rows, symbols)

    tradability_data = session.call(
        "get_equity_tradability",
        account_number=account_number,
        symbols=symbols,
    )
    tradability_rows = tradability_data.get("data", {}).get("results", [])
    tradability = result_by_symbol(tradability_rows, symbols)
    if not tradability_rows:
        raise EntryPreflightUnavailable(
            f"No tradability response for execution candidates {symbols}"
        )

    eligible: list[LeveragedETFSelection] = []
    rejected: list[str] = []
    for candidate in candidates:
        symbol = candidate.ticker
        item = quotes.get(symbol)
        trade = tradability.get(symbol)
        if item is None or trade is None:
            rejected.append(f"{symbol}:missing quote/tradability")
            continue
        quote = item.get("quote") or item
        try:
            last = float(quote["last_trade_price"])
            bid = float(quote["bid_price"])
            ask = float(quote["ask_price"])
        except (KeyError, TypeError, ValueError):
            rejected.append(f"{symbol}:incomplete quote")
            continue
        if last < LEVERAGED_ETF_MIN_PRICE or bid <= 0 or ask < bid:
            rejected.append(f"{symbol}:invalid/low price")
            continue
        midpoint = (ask + bid) / 2
        spread_pct = (ask - bid) / midpoint * 100
        if spread_pct > LEVERAGED_ETF_MAX_SPREAD_PCT:
            rejected.append(f"{symbol}:spread {spread_pct:.3f}%")
            continue
        trade_item = trade.get("tradability") or trade
        if not trade_item.get("tradeable", True):
            rejected.append(f"{symbol}:not tradeable")
            continue
        if trade_item.get("fractional_tradability", "tradable") == "untradable":
            rejected.append(f"{symbol}:not fractional")
            continue
        average_volume = _float_or_none(
            quote.get("average_volume_30_days")
            or quote.get("average_volume")
        )
        current_volume = _float_or_none(quote.get("volume"))
        observed_volume = average_volume or current_volume
        if (
            candidate.leverage > 1.0
            and (
                observed_volume is None
                or observed_volume < LEVERAGED_ETF_MIN_AVG_VOLUME
            )
        ):
            volume_label = f"{observed_volume:.0f}" if observed_volume is not None else "missing"
            rejected.append(f"{symbol}:volume {volume_label}")
            continue
        volume = observed_volume
        eligible.append(LeveragedETFSelection(
            symbol,
            candidate.leverage,
            last,
            bid,
            ask,
            spread_pct,
            volume,
        ))

    if not eligible:
        raise EntryPreflightRejected(
            "no_liquid_execution_route",
            f"No eligible execution route for {source_ticker} {direction}: {', '.join(rejected)}",
        )
    # Spread is the hard execution cost for a $20 order.  Volume is used as a
    # tie-breaker when Robinhood exposes it; P0 itself is a curated liquid list.
    # Prefer any eligible leveraged route.  The 1x source equity is a fallback,
    # not a cheaper-spread substitute for the requested leveraged exposure.
    eligible.sort(key=lambda row: (row.leverage <= 1.0, row.spread_pct, -(row.volume or 0)))
    return eligible[0]


def _place_fractional_market_buy(
    session: _MCPSession,
    account_number: str,
    ticker: str,
    usd: float,
    max_price: float,
    ref_key: str,
) -> OrderResult:
    """Place a regular-hours fractional market buy after quote preflight."""
    ref_id = str(uuid.uuid5(
        uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8"),
        f"dayentry:{ref_key}",
    ))
    order_kwargs: dict[str, Any] = {
        "account_number": account_number,
        "symbol": ticker,
        "side": "buy",
        "type": "market",
        "time_in_force": "gfd",
        "market_hours": "regular_hours",
        "dollar_amount": f"{usd:.2f}",
        "ref_id": ref_id,
    }
    log.info(
        "Submitting protected fractional buy for %s: $%.2f market, preflight cap %.2f",
        ticker,
        usd,
        max_price,
    )
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


def _entry_guard_price(pos: DayPosition) -> float:
    multiplier = (
        1 - ENTRY_LIMIT_OFFSET_PCT / 100
        if pos.trigger_operator == "below"
        else 1 + ENTRY_LIMIT_OFFSET_PCT / 100
    )
    return round(float(pos.trigger_price or 0) * multiplier, 2)


def _trigger_crossed(pos: DayPosition, price: float) -> bool:
    if pos.trigger_price is None:
        return False
    return (
        price <= pos.trigger_price
        if pos.trigger_operator == "below"
        else price >= pos.trigger_price
    )


def _trigger_is_armed(pos: DayPosition, price: float) -> bool:
    if pos.trigger_price is None:
        return False
    return (
        price > pos.trigger_price
        if pos.trigger_operator == "below"
        else price < pos.trigger_price
    )


def _trigger_gapped_past_guard(pos: DayPosition, price: float) -> bool:
    if pos.entry_limit_price is None:
        return False
    return (
        price < pos.entry_limit_price
        if pos.trigger_operator == "below"
        else price > pos.entry_limit_price
    )


def _execution_symbol(pos: DayPosition) -> str:
    return (pos.execution_ticker or pos.ticker).upper()


def _quote_symbols_for_position(pos: DayPosition) -> list[str]:
    symbols = [pos.ticker.upper()]
    execution = _execution_symbol(pos)
    if execution not in symbols:
        symbols.append(execution)
    return symbols


def _converted_heat_target(pos: DayPosition, fill_price: float) -> float | None:
    """Convert an underlying target move into an ETF target from its fill."""
    if (
        pos.source != "heat"
        or pos.signal_target_price is None
        or pos.trigger_price is None
        or not pos.execution_leverage
    ):
        return pos.target_price
    if pos.direction == "short":
        source_move = (pos.trigger_price - pos.signal_target_price) / pos.trigger_price
    else:
        source_move = (pos.signal_target_price - pos.trigger_price) / pos.trigger_price
    if source_move <= 0:
        log.warning(
            "Ignoring Heat target %.4f for %s %s trigger %.4f: wrong direction",
            pos.signal_target_price,
            pos.direction,
            pos.ticker,
            pos.trigger_price,
        )
        return None
    return round(fill_price * (1 + source_move * pos.execution_leverage), 4)


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
            _execution_symbol(pos),
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
    pos.target_price = _converted_heat_target(pos, result.fill_price)
    execution = _execution_symbol(pos)
    pos.stop_order_id = _place_stop_order(
        session, account_number, execution, result.fill_qty, pos.stop_price
    )
    if pos.target_price:
        pos.limit_order_id = _place_limit_sell(
            session, account_number, execution, result.fill_qty, pos.target_price
        )
    log.info(
        "Entered %s via %s fill=%.4f stop=%.4f target=%s order=%s",
        pos.ticker,
        execution,
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


def _reset_manual_watch_after_unfilled_entry(pos: DayPosition) -> None:
    """Re-arm a GTC watch with a fresh idempotency attempt after no fill."""
    pos.status = "watching"
    pos.armed = False
    pos.buy_order_id = None
    pos.entry_submitted_at = None
    pos.entry_cancel_requested_at = None
    pos.entry_cancel_reason = None
    pos.entry_last_error = None
    pos.entry_filled_qty = 0.0
    pos.entry_filled_value = 0.0
    pos.entry_limit_price = None
    pos.execution_ticker = None
    pos.execution_leverage = None
    pos.execution_spread_pct = None
    pos.execution_selected_at = None
    pos.exit_reason = None
    pos.entry_attempt_no += 1
    log.info(
        "Manual watch %s returned to waiting-rearm after unfilled attempt %d",
        pos.ticker,
        pos.entry_attempt_no,
    )


def _end_unsubmitted_entry(pos: DayPosition, reason: str, detail: object) -> None:
    """End an entry attempt known not to have reached the broker."""
    if pos.good_til_cancelled and not pos.manual_cancel_requested:
        _reset_manual_watch_after_unfilled_entry(pos)
        pos.exit_reason = f"waiting_rearm_after_{reason}"
    else:
        pos.status = "expired"
        pos.exit_reason = f"entry_{reason}"
        pos.entry_last_error = f"submission_rejected:{detail}"
    log.warning("Entry not submitted for %s (%s): %s", pos.ticker, reason, detail)


def _is_definitive_entry_rejection(error: object) -> bool:
    """Return whether MCP explicitly rejected the order before acknowledgement."""
    return "'place_equity_order' returned isError:" in str(error)


def _recover_definitive_entry_rejections(positions: list[DayPosition]) -> bool:
    """Repair legacy rejected entries without waiting for market hours."""
    changed = False
    for pos in positions:
        if (
            pos.status == "pending_entry"
            and not pos.buy_order_id
            and pos.entry_last_error
            and _is_definitive_entry_rejection(pos.entry_last_error)
        ):
            rejection = pos.entry_last_error
            _end_unsubmitted_entry(pos, "broker_rejected", rejection)
            changed = True
    return changed


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
        if pos.good_til_cancelled and not pos.manual_cancel_requested:
            _reset_manual_watch_after_unfilled_entry(pos)
        else:
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
    if pos.entry_last_error and _is_definitive_entry_rejection(pos.entry_last_error):
        rejection = pos.entry_last_error
        _end_unsubmitted_entry(pos, "broker_rejected", rejection)
        return True
    if not pos.entry_submitted_at:
        try:
            last, bid, ask, spread_pct = _validate_entry_preflight(
                session,
                pos.ticker,
                float(pos.trigger_price or 0),
                pos.entry_limit_price,
                pos.trigger_operator,
            )
        except EntryPreflightRejected as exc:
            _end_unsubmitted_entry(pos, exc.reason, exc)
            return True
        except Exception as exc:
            # A quote outage happens before the broker boundary.  Keep the
            # watch armed and try a fresh preflight later; do not turn it into
            # an ambiguous pending order.
            pos.entry_last_error = f"preflight_unavailable:{exc}"
            log.warning("Entry preflight unavailable for %s: %s", pos.ticker, exc)
            return True
        log.info(
            "SIGNAL PREFLIGHT: %s last=%.4f bid=%.4f ask=%.4f spread=%.3f%% guard=%.4f",
            pos.ticker,
            last,
            bid,
            ask,
            spread_pct,
            pos.entry_limit_price,
        )
        if pos.source == "heat":
            try:
                selection = _select_leveraged_etf(
                    session,
                    account_number,
                    pos.ticker,
                    pos.direction,
                )
            except EntryPreflightRejected as exc:
                _end_unsubmitted_entry(pos, exc.reason, exc)
                return True
            except Exception as exc:
                pos.entry_last_error = f"leveraged_preflight_unavailable:{exc}"
                log.warning(
                    "Leveraged ETF preflight unavailable for %s: %s",
                    pos.ticker,
                    exc,
                )
                return True
            pos.execution_ticker = selection.ticker
            pos.execution_leverage = selection.leverage
            pos.execution_spread_pct = selection.spread_pct
            pos.execution_selected_at = datetime.now(timezone.utc).isoformat()
            log.info(
                "ETF PREFLIGHT: %s %s -> %s last=%.4f bid=%.4f ask=%.4f spread=%.3f%%",
                pos.ticker,
                pos.direction,
                selection.ticker,
                selection.last,
                selection.bid,
                selection.ask,
                selection.spread_pct,
            )
        pos.entry_submitted_at = datetime.now(timezone.utc).isoformat()
        pos.entry_last_error = None
    pos.status = "pending_entry"
    # Persist the intent before crossing the broker boundary.  A restart can
    # safely repeat this call because ``pos.id`` generates the same ref_id.
    _append_position(pos)
    try:
        result = _place_fractional_market_buy(
            session,
            account_number,
            _execution_symbol(pos),
            DAY_TRADE_BUDGET_USD,
            pos.entry_limit_price,
            f"{pos.id}:{pos.entry_attempt_no}",
        )
    except RobinhoodMCPError as exc:
        if _is_definitive_entry_rejection(exc):
            _end_unsubmitted_entry(pos, "broker_rejected", exc)
            return True
        pos.entry_last_error = f"submission_ambiguous:{exc}"
        log.error(
            "Entry acknowledgement missing for %s; stable-ref recovery will retry: %s",
            _execution_symbol(pos),
            exc,
        )
        return True
    except Exception as exc:
        pos.entry_last_error = f"submission_ambiguous:{exc}"
        log.error(
            "Entry acknowledgement missing for %s; stable-ref recovery will retry: %s",
            _execution_symbol(pos),
            exc,
        )
        return True

    pos.buy_order_id = result.order_id
    pos.entry_last_error = None
    _apply_entry_order_result(session, account_number, pos, result)
    if pos.status == "pending_entry":
        log.info(
            "Entry order pending for %s with preflight cap %.4f order=%s state=%s",
            _execution_symbol(pos),
            pos.entry_limit_price,
            result.order_id,
            result.state,
        )
    return True


def _migrate_stop_policy(pos: DayPosition) -> bool:
    """Map an older position to the first new milestone that raises its stop."""
    if pos.stop_policy_version >= _STOP_POLICY_VERSION:
        return False
    current_stop = float(pos.stop_price or 0)
    fill_price = float(pos.fill_price or 0)
    if fill_price > 0:
        pos.milestone_idx = len(_TRAILING_MILESTONES)
        for idx, (_, lock_in_pct) in enumerate(_TRAILING_MILESTONES):
            candidate = round(fill_price * (1 + lock_in_pct / 100), 4)
            if candidate > current_stop:
                pos.milestone_idx = idx
                break
    pos.confirm_count = 0
    pos.confirm_milestone_idx = None
    pos.stop_policy_version = _STOP_POLICY_VERSION
    log.info(
        "Migrated stop policy for %s; current stop=%.4f next milestone=%d",
        pos.ticker,
        current_stop,
        pos.milestone_idx,
    )
    return True


def _position_poll_interval(pos: DayPosition) -> int:
    """Return this ticker's own polling interval without affecting others."""
    if pos.status in ("pending_entry", "open", "pending_exit"):
        return NEAR_POLL_INTERVAL_S
    watch_price = (
        pos.signal_current_price
        if pos.signal_current_price is not None
        else pos.current_price
    )
    if (
        pos.status == "watching"
        and pos.trigger_price
        and watch_price is not None
        and abs(watch_price / pos.trigger_price - 1) * 100 <= NEAR_TRIGGER_PCT
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
        # Defence in depth: callers/tests can supply Signal objects without
        # going through _load_new_plans().  Only explicit LONG Discord plans
        # are allowed to create an executable DayPosition.
        if sig.side is not Side.LONG:
            log.warning(
                "Ignoring unsupported day-trade PLAN: %s side=%s",
                sig.ticker,
                sig.side.value if sig.side else "missing",
            )
            continue
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
            direction="long",
            trigger_operator="above",
        )
        positions.append(pos)
        _append_position(pos)
        log.info("New day-trade plan: %s trigger=%.2f target=%s", sig.ticker, sig.trigger or 0, sig.target)

    if _sync_manual_plans(positions):
        _flush_positions(positions)
    if _sync_heat_ideas(positions, now=now):
        _flush_positions(positions)
    if _recover_definitive_entry_rejections(positions):
        _flush_positions(positions)

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
        quote_symbols = [
            symbol
            for pos in due
            for symbol in _quote_symbols_for_position(pos)
        ]
        prices = _get_prices(session, quote_symbols)
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
        signal_price = prices.get(pos.ticker.upper())
        execution = _execution_symbol(pos)
        price = (
            prices.get(execution)
            if pos.status in ("open", "pending_exit")
            else signal_price
        )

        if signal_price is not None and pos.signal_current_price != signal_price:
            pos.signal_current_price = signal_price
            changed = True
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
            result = _poll_order(
                session, account_number, _execution_symbol(pos), pos.buy_order_id
            )
            if result and _apply_entry_order_result(
                session, account_number, pos, result
            ):
                changed = True

            if pos.status == "pending_entry" and not pos.entry_cancel_requested_at:
                cancel_reason: str | None = None
                if pos.manual_cancel_requested:
                    cancel_reason = "manual_cancel"
                elif force_close:
                    cancel_reason = "eod"
                elif signal_price is not None and pos.trigger_price and not _trigger_crossed(
                    pos, signal_price
                ):
                    cancel_reason = "lost_trigger"
                elif _entry_order_timed_out(pos, now):
                    cancel_reason = "timeout"
                if cancel_reason and _request_entry_cancel(
                    session, account_number, pos, cancel_reason
                ):
                    changed = True

        # Polling a pending entry can transition it to open in this same loop.
        # From that point onward, every risk decision must use the ETF quote,
        # never the source ticker quote captured at the top of the iteration.
        if pos.status in ("open", "pending_exit"):
            execution_price = prices.get(_execution_symbol(pos))
            price = execution_price
            if execution_price is not None and pos.current_price != execution_price:
                pos.current_price = execution_price
                changed = True

        if (
            pos.status == "open"
            and pos.source in ("manual", "heat")
            and pos.manual_cancel_requested
            and pos.fill_qty
        ):
            if _start_or_retry_exit(session, account_number, pos, "manual"):
                changed = True
            continue

        # --- Resolve or retry a broker-confirmed exit. ---
        if pos.status == "pending_exit":
            if pos.exit_order_id:
                result = _poll_order(
                    session, account_number, _execution_symbol(pos), pos.exit_order_id
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
            if not pos.good_til_cancelled:
                pos.status = "expired"
                changed = True
            continue

        if force_close and pos.status == "pending_entry":
            # Cancellation has been requested above.  Do not mark this order
            # expired until Robinhood reports a terminal state; it may contain
            # a partial fill that must be sold at EOD.
            continue

        # --- Entry: watching -> quote-gated fractional market order -> open ---
        if (
            pos.status == "watching"
            and signal_price is not None
            and pos.trigger_price is not None
        ):
            if not pos.armed:
                if _trigger_is_armed(pos, signal_price):
                    pos.armed = True
                    pos.exit_reason = None
                    changed = True
                    log.info(
                        "%s watch armed: %s price=%.4f below trigger=%.4f",
                        pos.source.capitalize(),
                        pos.ticker,
                        signal_price,
                        pos.trigger_price,
                    )
                continue
            if _trigger_crossed(pos, signal_price):
                limit_price = _entry_guard_price(pos)
                pos.entry_limit_price = limit_price
                if _trigger_gapped_past_guard(pos, signal_price):
                    log.warning(
                        "SKIP GAP: %s price=%.4f passed entry guard %.4f (trigger=%.4f operator=%s)",
                        pos.ticker,
                        signal_price,
                        limit_price,
                        pos.trigger_price,
                        pos.trigger_operator,
                    )
                    if pos.good_til_cancelled:
                        pos.armed = False
                        pos.exit_reason = "waiting_rearm_after_gap"
                    else:
                        pos.status = "expired"
                        pos.exit_reason = "entry_gap_above_limit"
                    changed = True
                    continue

                log.info(
                    "TRIGGER: %s price=%.4f >= trigger=%.4f — preparing protected $%.0f fractional buy with cap %.4f",
                    pos.ticker,
                    signal_price,
                    pos.trigger_price,
                    DAY_TRADE_BUDGET_USD,
                    limit_price,
                )
                if _submit_or_recover_entry(session, account_number, pos):
                    changed = True
            continue

        # --- Open position management ---
        if pos.status == "open" and price is not None and pos.fill_price:
            if _migrate_stop_policy(pos):
                changed = True

            # Update high-water mark
            if pos.high_water_mark is None or price > pos.high_water_mark:
                pos.high_water_mark = price

            # A broker-managed stop owns the exit while it remains active.
            # Never place a second market sell simply because the quote crossed
            # the stop before the broker order status update reached us.
            if pos.stop_order_id:
                stop_result = _poll_order(
                    session, account_number, _execution_symbol(pos), pos.stop_order_id
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
                    od = session.call(
                        "get_equity_orders",
                        account_number=account_number,
                        symbol=_execution_symbol(pos),
                    )
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
                        pos.stop_order_id = _place_stop_order(
                            session,
                            account_number,
                            _execution_symbol(pos),
                            pos.fill_qty,
                            new_stop,
                        )
                    pos.stop_price = new_stop
                    pos.eod_tightened = True
                    changed = True
                continue  # Don't do trailing stop after EOD tighten

            # Stepped trailing stop (normal hours)
            milestones = _TRAILING_MILESTONES
            eligible_idx: int | None = None
            for idx in range(pos.milestone_idx, len(milestones)):
                threshold_pct, _ = milestones[idx]
                if price >= pos.fill_price * (1 + threshold_pct / 100):
                    eligible_idx = idx
                else:
                    break

            if eligible_idx is None:
                pos.confirm_count = 0
                pos.confirm_milestone_idx = None
            else:
                if pos.confirm_milestone_idx == eligible_idx:
                    pos.confirm_count += 1
                else:
                    pos.confirm_milestone_idx = eligible_idx
                    pos.confirm_count = 1

                if pos.confirm_count >= _CONFIRM_POLLS:
                    threshold_pct, lock_in_pct = milestones[eligible_idx]
                    new_stop = round(
                        pos.fill_price * (1 + lock_in_pct / 100), 4
                    )
                    if new_stop > (pos.stop_price or 0):
                        log.info(
                            "Trail stop upgrade %s: +%.0f%% confirmed → stop %.4f → %.4f",
                            pos.ticker, threshold_pct, pos.stop_price or 0, new_stop
                        )
                        if pos.stop_order_id:
                            _cancel_order(session, account_number, pos.stop_order_id)
                        if pos.fill_qty:
                            pos.stop_order_id = _place_stop_order(
                                session,
                                account_number,
                                _execution_symbol(pos),
                                pos.fill_qty,
                                new_stop,
                            )
                        pos.stop_price = new_stop
                    pos.confirm_count = 0
                    pos.confirm_milestone_idx = None
                    pos.milestone_idx = eligible_idx + 1
                    changed = True

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
        "Day trader started. budget=$%.0f poll=%ds/%ds tick=%.1fs near=%.2f%% entry_cap=%.2f%% max_spread=%.2f%% etf_spread=%.2f%% etf_min_avg_volume=%.0f positions_log=%s",
        DAY_TRADE_BUDGET_USD,
        FAR_POLL_INTERVAL_S,
        NEAR_POLL_INTERVAL_S,
        SCHEDULER_TICK_S,
        NEAR_TRIGGER_PCT,
        ENTRY_LIMIT_OFFSET_PCT,
        ENTRY_MAX_SPREAD_PCT,
        LEVERAGED_ETF_MAX_SPREAD_PCT,
        LEVERAGED_ETF_MIN_AVG_VOLUME,
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
