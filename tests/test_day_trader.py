"""
Unit tests for bot/day_trader.py — dry-run with mocked MCP calls.

Every test patches the network/IO layer so no real orders are placed.
The primary contract verified in each test:

  "If condition X is true, _market_sell_all / _place_fractional_market_buy /
   _cancel_order MUST be called with correct arguments and the position
   state MUST transition correctly."
"""

from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, call, patch

# ---------------------------------------------------------------------------
# Make bot package importable from the project root without installing.
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.day_trader import (
    DAY_TRADE_BUDGET_USD,
    _CONFIRM_POLLS,
    _FIRST_MILESTONE_CONFIRM_POLLS,
    _INITIAL_STOP_PCT,
    _STOP_POLICY_VERSION,
    _TRAILING_MILESTONES,
    ENTRY_ORDER_TTL_S,
    ENTRY_LIMIT_OFFSET_PCT,
    ENTRY_MAX_SPREAD_PCT,
    FAR_POLL_INTERVAL_S,
    NEAR_POLL_INTERVAL_S,
    DayTraderRuntime,
    DayPosition,
    EntryPreflightRejected,
    EntryPreflightUnavailable,
    LeveragedETFSelection,
    _apply_entry_order_result,
    _apply_exit_order_result,
    _get_prices,
    _load_new_plans,
    _recover_legacy_discord_carryovers,
    _migrate_stop_policy,
    _position_poll_interval,
    _select_leveraged_etf,
    _place_fractional_market_buy,
    _start_or_retry_exit,
    _submit_or_recover_entry,
    _sync_manual_plans,
    _validate_entry_preflight,
    run_once,
)
from bot.leveraged_etfs import LeveragedETF
from bot.robinhood_mcp_client import OrderResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ET_MARKET_OPEN = datetime(2026, 6, 16, 10, 0, tzinfo=__import__("zoneinfo").ZoneInfo("America/New_York"))
ET_EOD_TIGHTEN = datetime(2026, 6, 16, 15, 35, tzinfo=__import__("zoneinfo").ZoneInfo("America/New_York"))
ET_FORCE_CLOSE = datetime(2026, 6, 16, 15, 51, tzinfo=__import__("zoneinfo").ZoneInfo("America/New_York"))
ET_AFTER_HOURS = datetime(2026, 6, 16, 17, 0, tzinfo=__import__("zoneinfo").ZoneInfo("America/New_York"))
ET_WEEKEND = datetime(2026, 6, 14, 10, 0, tzinfo=__import__("zoneinfo").ZoneInfo("America/New_York"))  # Saturday


def _open_pos(fill=10.0, stop=9.8, qty=2.0, target=None, stop_order_id=None, limit_order_id=None,
              milestone_idx=0, confirm_count=0, confirm_milestone_idx=None,
              eod_tightened=False) -> DayPosition:
    """Return a fully-entered open DayPosition."""
    return DayPosition(
        ticker="TEST",
        status="open",
        trigger_price=9.5,
        target_price=target,
        fill_price=fill,
        fill_qty=qty,
        stop_price=stop,
        high_water_mark=fill,
        entered_at=datetime.now(timezone.utc).isoformat(),
        stop_order_id=stop_order_id,
        limit_order_id=limit_order_id,
        milestone_idx=milestone_idx,
        confirm_count=confirm_count,
        confirm_milestone_idx=confirm_milestone_idx,
        eod_tightened=eod_tightened,
        plan_received_at=datetime.now(timezone.utc).isoformat(),
    )


def _watching_pos(trigger=10.0, target=None) -> DayPosition:
    return DayPosition(
        ticker="TEST",
        status="watching",
        trigger_price=trigger,
        target_price=target,
        plan_received_at=datetime.now(timezone.utc).isoformat(),
        plan_signal_id="sig-001",
    )


def _patch_env(now=ET_MARKET_OPEN, price=10.0, buy_result: OrderResult | None = None):
    """Return a context-manager stack that patches all I/O for run_once."""
    if buy_result is None:
        buy_result = OrderResult(
            order_id="buy-001", state="filled", fill_price=price, fill_qty=2.0, fill_usd=price * 2
        )

    patches = [
        patch("bot.day_trader.datetime", wraps=__import__("datetime").datetime),
        patch("bot.day_trader._load_new_plans", return_value=[]),
        patch("bot.day_trader._load_token", return_value="fake-token"),
        patch("bot.day_trader._MCPSession", return_value=MagicMock()),
        patch("bot.day_trader._get_agentic_account", return_value="acct-001"),
        patch("bot.day_trader._get_prices", return_value={"TEST": price}),
        patch("bot.day_trader._validate_entry_preflight", return_value=(price, price, price, 0.0)),
        patch("bot.day_trader._place_fractional_market_buy", return_value=buy_result),
        patch("bot.day_trader._place_stop_order", return_value="stop-001"),
        patch("bot.day_trader._place_limit_sell", return_value="limit-001"),
        patch("bot.day_trader._cancel_order"),
        patch("bot.day_trader._market_sell_all"),
        patch("bot.day_trader._append_position"),
        patch("bot.day_trader._flush_positions"),
        patch("bot.day_trader.datetime") ,
    ]
    return patches


class _Base(unittest.TestCase):
    """Base with convenience patching."""

    def _run(
        self,
        positions,
        now=ET_MARKET_OPEN,
        price=10.0,
        buy_result=None,
        new_plans=None,
        manual_plans=None,
    ):
        """Run run_once with all I/O mocked. Returns (positions, mock_namespace)."""
        if buy_result is None:
            buy_result = OrderResult(
                order_id="buy-001", state="filled",
                fill_price=price, fill_qty=2.0, fill_usd=price * 2,
            )
        if manual_plans is None:
            manual_plans = [
                {
                    "id": pos.manual_plan_id,
                    "ticker": pos.ticker,
                    "trigger_price": pos.trigger_price,
                    "target_price": pos.target_price,
                    "setup": pos.setup,
                    "status": "active",
                    "created_at": pos.plan_received_at,
                }
                for pos in positions
                if pos.source == "manual" and pos.manual_plan_id
            ]
        mocks = {}
        with patch("bot.day_trader._load_new_plans", return_value=new_plans or []) as m_plans, \
             patch("bot.day_trader.load_plans", return_value=manual_plans), \
             patch("bot.day_trader._load_token", return_value="tok") as m_tok, \
             patch("bot.day_trader._MCPSession", return_value=MagicMock()) as m_sess, \
             patch("bot.day_trader._get_agentic_account", return_value="acct") as m_acct, \
             patch("bot.day_trader._get_prices", return_value={"TEST": price}) as m_price, \
             patch("bot.day_trader._validate_entry_preflight", return_value=(price, price, price, 0.0)) as m_preflight, \
             patch("bot.day_trader._place_fractional_market_buy", return_value=buy_result) as m_buy, \
             patch("bot.day_trader._place_stop_order", return_value="stop-001") as m_stop, \
             patch("bot.day_trader._place_limit_sell", return_value="limit-001") as m_lim, \
             patch("bot.day_trader._cancel_order") as m_cancel, \
             patch("bot.day_trader._market_sell_all") as m_sell, \
             patch("bot.day_trader._append_position") as m_append, \
             patch("bot.day_trader._flush_positions") as m_flush, \
             patch("bot.day_trader.datetime") as m_dt:

            # Make datetime.now(ET) return `now` so time checks work
            m_dt.now.return_value = now
            m_dt.fromisoformat.side_effect = datetime.fromisoformat
            # Keep datetime constructor and other methods working
            m_dt.side_effect = lambda *a, **k: __import__("datetime").datetime(*a, **k)
            m_sell.side_effect = lambda _s, _a, _t, qty, _ref: OrderResult(
                order_id="sell-001",
                state="filled",
                fill_price=price,
                fill_qty=qty,
                fill_usd=price * qty,
            )

            run_once(positions, set())

            mocks = {
                "buy": m_buy, "preflight": m_preflight, "stop": m_stop, "lim": m_lim,
                "cancel": m_cancel, "sell": m_sell,
                "append": m_append, "flush": m_flush,
            }
        return positions, mocks


# ===========================================================================
# 1. Entry (watching → open)
# ===========================================================================

class TestEntry(_Base):

    def test_no_buy_when_price_below_trigger(self):
        """Price below trigger: no buy placed, position stays watching."""
        pos = _watching_pos(trigger=10.0)
        positions, m = self._run([pos], price=9.99)
        self.assertEqual(pos.status, "watching")
        m["buy"].assert_not_called()

    def test_buy_placed_when_price_at_trigger(self):
        """Price exactly at trigger: buy IS placed."""
        pos = _watching_pos(trigger=10.0)
        positions, m = self._run([pos], price=10.0)
        self.assertEqual(pos.status, "open")
        m["buy"].assert_called_once()

    def test_buy_placed_when_price_above_trigger_but_within_cap(self):
        """A small breakout within the configured cap places a protected buy."""
        pos = _watching_pos(trigger=10.0)
        positions, m = self._run([pos], price=10.01)
        self.assertEqual(pos.status, "open")
        m["buy"].assert_called_once()

    def test_gap_above_entry_cap_is_skipped(self):
        """A breakout already above the cap expires instead of chasing."""
        pos = _watching_pos(trigger=10.0)
        positions, m = self._run([pos], price=10.5)
        self.assertEqual(pos.status, "expired")
        self.assertEqual(pos.exit_reason, "entry_gap_above_limit")
        self.assertEqual(pos.entry_limit_price, 10.02)
        m["buy"].assert_not_called()

    def test_protected_buy_uses_trigger_based_cap(self):
        pos = _watching_pos(trigger=10.0)
        positions, m = self._run([pos], price=10.0)
        args = m["buy"].call_args[0]
        self.assertEqual(args[4], round(10.0 * (1 + ENTRY_LIMIT_OFFSET_PCT / 100), 2))

    def test_unfilled_market_order_becomes_pending(self):
        pos = _watching_pos(trigger=10.0)
        result = OrderResult("b1", "queued", None, None, None)
        positions, m = self._run([pos], price=10.0, buy_result=result)
        self.assertEqual(pos.status, "pending_entry")
        self.assertEqual(pos.buy_order_id, "b1")
        m["stop"].assert_not_called()

    def test_initial_stop_set_at_minus_2pct(self):
        """After buy, stop is fill_price × 0.98."""
        fill = 10.0
        pos = _watching_pos(trigger=10.0)
        positions, m = self._run([pos], price=10.0,
                                 buy_result=OrderResult("b1", "filled", fill, 2.0, 20.0))
        self.assertAlmostEqual(pos.stop_price, fill * (1 - _INITIAL_STOP_PCT / 100), places=4)

    def test_stop_order_attempted_after_buy(self):
        """_place_stop_order is called once with correct ticker after entry."""
        pos = _watching_pos(trigger=10.0)
        positions, m = self._run([pos], price=10.0)
        m["stop"].assert_called_once()
        args = m["stop"].call_args[0]
        self.assertEqual(args[2], "TEST")  # ticker

    def test_limit_sell_attempted_when_target_given(self):
        """If PLAN has a target, _place_limit_sell is called after entry."""
        pos = _watching_pos(trigger=10.0, target=12.0)
        positions, m = self._run([pos], price=10.0)
        m["lim"].assert_called_once()
        args = m["lim"].call_args[0]
        self.assertEqual(args[4], 12.0)  # limit_price

    def test_no_limit_sell_when_no_target(self):
        """No target in plan → _place_limit_sell not called."""
        pos = _watching_pos(trigger=10.0, target=None)
        positions, m = self._run([pos], price=10.0)
        m["lim"].assert_not_called()

    def test_position_fill_price_recorded(self):
        """fill_price and fill_qty from OrderResult are stored on position."""
        pos = _watching_pos(trigger=10.0)
        br = OrderResult("b1", "filled", 10.01, 1.95, 19.5195)
        positions, m = self._run([pos], price=10.01, buy_result=br)
        self.assertEqual(pos.fill_price, 10.01)
        self.assertEqual(pos.fill_qty, 1.95)

    def test_duplicate_plan_ignored(self):
        """A second PLAN for a ticker already watching is silently skipped."""
        pos = _watching_pos(trigger=10.0)  # already watching
        pos.plan_signal_id = "sig-001"
        # Provide a second plan for same ticker via new_plans
        from bot.parser import Side, Signal, SignalKind
        from datetime import timezone
        sig2 = Signal(kind=SignalKind.PLAN, ticker="TEST", trigger=10.0,
                      side=Side.LONG,
                      received_at=datetime.now(timezone.utc))
        sig2.message_id = "sig-002"  # type: ignore
        positions, m = self._run([pos], price=9.0, new_plans=[sig2])
        # Still only one position
        self.assertEqual(len(positions), 1)


class TestPendingEntryLifecycle(_Base):

    def test_partial_fill_cancels_remainder_but_does_not_open_early(self):
        pos = _watching_pos(trigger=10.0)
        pos.status = "pending_entry"
        pos.buy_order_id = "buy-partial"
        session = MagicMock()
        with patch("bot.day_trader._cancel_order", return_value=True) as cancel, \
             patch("bot.day_trader._place_stop_order") as stop:
            changed = _apply_entry_order_result(
                session,
                "acct",
                pos,
                OrderResult("buy-partial", "partially_filled", 10.01, 0.75, 7.5075),
            )
        self.assertTrue(changed)
        self.assertEqual(pos.status, "pending_entry")
        self.assertEqual(pos.entry_filled_qty, 0.75)
        self.assertEqual(pos.entry_cancel_reason, "partial_fill")
        cancel.assert_called_once_with(session, "acct", "buy-partial")
        stop.assert_not_called()

    def test_cancelled_partial_fill_opens_only_final_quantity(self):
        pos = _watching_pos(trigger=10.0)
        pos.status = "pending_entry"
        pos.buy_order_id = "buy-partial"
        pos.entry_filled_qty = 0.75
        pos.entry_filled_value = 7.5075
        pos.entry_cancel_requested_at = datetime.now(timezone.utc).isoformat()
        pos.entry_cancel_reason = "partial_fill"
        session = MagicMock()
        with patch("bot.day_trader._place_stop_order", return_value="stop-1") as stop, \
             patch("bot.day_trader._place_limit_sell", return_value=None):
            changed = _apply_entry_order_result(
                session,
                "acct",
                pos,
                OrderResult("buy-partial", "cancelled", 10.02, 0.8, 8.016),
            )
        self.assertTrue(changed)
        self.assertEqual(pos.status, "open")
        self.assertEqual(pos.fill_qty, 0.8)
        self.assertEqual(pos.fill_price, 10.02)
        stop.assert_called_once_with(session, "acct", "TEST", 0.8, 9.8196)

    def test_cancelled_unfilled_entry_expires(self):
        pos = _watching_pos(trigger=10.0)
        pos.status = "pending_entry"
        pos.buy_order_id = "buy-empty"
        pos.entry_cancel_reason = "lost_trigger"
        changed = _apply_entry_order_result(
            MagicMock(),
            "acct",
            pos,
            OrderResult("buy-empty", "cancelled", None, None, None),
        )
        self.assertTrue(changed)
        self.assertEqual(pos.status, "expired")
        self.assertEqual(pos.exit_reason, "entry_lost_trigger")

    def test_cancelled_unfilled_manual_entry_returns_to_waiting_rearm(self):
        pos = _watching_pos(trigger=10.0)
        pos.status = "pending_entry"
        pos.source = "manual"
        pos.manual_plan_id = "manual-1"
        pos.good_til_cancelled = True
        pos.armed = True
        pos.buy_order_id = "buy-empty"
        pos.entry_limit_price = 10.02
        pos.entry_cancel_reason = "lost_trigger"
        changed = _apply_entry_order_result(
            MagicMock(),
            "acct",
            pos,
            OrderResult("buy-empty", "cancelled", None, None, None),
        )
        self.assertTrue(changed)
        self.assertEqual(pos.status, "watching")
        self.assertFalse(pos.armed)
        self.assertEqual(pos.entry_attempt_no, 1)
        self.assertIsNone(pos.buy_order_id)
        self.assertIsNone(pos.entry_limit_price)

    def test_price_losing_trigger_requests_cancel_and_stays_pending(self):
        pos = _watching_pos(trigger=10.0)
        pos.status = "pending_entry"
        pos.buy_order_id = "buy-queued"
        pos.entry_submitted_at = ET_MARKET_OPEN.isoformat()
        with patch(
            "bot.day_trader._poll_order",
            return_value=OrderResult("buy-queued", "queued", None, None, None),
        ):
            _, mocks = self._run([pos], price=9.99)
        self.assertEqual(pos.status, "pending_entry")
        self.assertEqual(pos.entry_cancel_reason, "lost_trigger")
        mocks["cancel"].assert_called_once()

    def test_entry_timeout_requests_cancel(self):
        pos = _watching_pos(trigger=10.0)
        pos.status = "pending_entry"
        pos.buy_order_id = "buy-queued"
        pos.entry_submitted_at = (
            ET_MARKET_OPEN - timedelta(seconds=ENTRY_ORDER_TTL_S + 1)
        ).isoformat()
        with patch(
            "bot.day_trader._poll_order",
            return_value=OrderResult("buy-queued", "queued", None, None, None),
        ):
            _, mocks = self._run([pos], price=10.01)
        self.assertEqual(pos.status, "pending_entry")
        self.assertEqual(pos.entry_cancel_reason, "timeout")
        mocks["cancel"].assert_called_once()

    def test_eod_terminal_partial_fill_is_closed_not_orphaned(self):
        pos = _watching_pos(trigger=10.0)
        pos.status = "pending_entry"
        pos.buy_order_id = "buy-partial"
        pos.entry_submitted_at = ET_MARKET_OPEN.isoformat()
        pos.entry_cancel_requested_at = ET_FORCE_CLOSE.isoformat()
        pos.entry_cancel_reason = "eod"
        with patch(
            "bot.day_trader._poll_order",
            return_value=OrderResult(
                "buy-partial", "cancelled", 10.01, 0.75, 7.5075
            ),
        ):
            self._run([pos], now=ET_FORCE_CLOSE, price=10.0)
        self.assertEqual(pos.status, "closed")
        self.assertEqual(pos.exit_reason, "eod")
        self.assertEqual(pos.fill_qty, 0.75)
        self.assertEqual(pos.exit_filled_qty, 0.75)

    def test_entry_ref_is_stable_for_safe_retry(self):
        def make_session():
            session = MagicMock()
            session.call.side_effect = [
                {"data": {"order": {"id": "order-1", "state": "queued"}}},
                {"data": {"orders": [{
                    "id": "order-1",
                    "state": "filled",
                    "average_price": "10.01",
                    "cumulative_quantity": "1.998",
                }]}},
            ]
            return session

        first = make_session()
        second = make_session()
        with patch("bot.day_trader.time.sleep"):
            _place_fractional_market_buy(first, "acct", "TEST", 20, 10.02, "same-position")
            _place_fractional_market_buy(second, "acct", "TEST", 20, 10.02, "same-position")
        self.assertEqual(
            first.call.call_args_list[0].kwargs["ref_id"],
            second.call.call_args_list[0].kwargs["ref_id"],
        )


# ===========================================================================
# 2. Stop loss
# ===========================================================================

class TestStopLoss(_Base):

    def test_stop_triggers_market_sell(self):
        """THE BUG: price ≤ stop MUST call _market_sell_all."""
        pos = _open_pos(fill=10.0, stop=9.8, qty=2.0)
        positions, m = self._run([pos], price=9.79)
        self.assertEqual(m["sell"].call_args.args[:4], (unittest.mock.ANY, "acct", "TEST", 2.0))

    def test_stop_closes_position(self):
        """Position status becomes 'closed' after stop."""
        pos = _open_pos(fill=10.0, stop=9.8, qty=2.0)
        positions, m = self._run([pos], price=9.79)
        self.assertEqual(pos.status, "closed")
        self.assertEqual(pos.exit_reason, "stop")

    def test_stop_records_exit_price_and_pnl(self):
        """exit_price and realized_pnl are set correctly on stop."""
        fill, stop, qty = 10.0, 9.8, 2.0
        pos = _open_pos(fill=fill, stop=stop, qty=qty)
        positions, m = self._run([pos], price=stop - 0.05)
        exit_p = stop - 0.05
        self.assertAlmostEqual(pos.exit_price, exit_p)
        self.assertAlmostEqual(pos.realized_pnl, (exit_p - fill) * qty, places=4)

    def test_stop_tracks_existing_broker_stop_without_duplicate_sell(self):
        """A broker stop already owns the exit; never submit a duplicate sell."""
        pos = _open_pos(fill=10.0, stop=9.8, qty=2.0, stop_order_id="stp-999")
        positions, m = self._run([pos], price=9.7)
        self.assertEqual(pos.status, "pending_exit")
        self.assertEqual(pos.exit_order_id, "stp-999")
        m["cancel"].assert_not_called()
        m["sell"].assert_not_called()

    def test_stop_cancels_existing_limit_order(self):
        """If a broker limit order exists, it's cancelled on stop."""
        pos = _open_pos(fill=10.0, stop=9.8, qty=2.0, limit_order_id="lim-999")
        positions, m = self._run([pos], price=9.7)
        m["cancel"].assert_any_call(unittest.mock.ANY, "acct", "lim-999")

    def test_no_sell_when_price_above_stop(self):
        """Price above stop: no sell."""
        pos = _open_pos(fill=10.0, stop=9.8, qty=2.0)
        positions, m = self._run([pos], price=9.85)
        m["sell"].assert_not_called()

    def test_no_sell_when_price_exactly_above_stop(self):
        """Price exactly one tick above stop: no sell."""
        pos = _open_pos(fill=10.0, stop=9.8, qty=2.0)
        positions, m = self._run([pos], price=9.81)
        m["sell"].assert_not_called()

    def test_stop_at_exact_stop_price(self):
        """Price == stop_price still triggers the sell."""
        pos = _open_pos(fill=10.0, stop=9.8, qty=2.0)
        positions, m = self._run([pos], price=9.8)
        m["sell"].assert_called_once()


# ===========================================================================
# 3. Target hit (bot-managed)
# ===========================================================================

class TestTarget(_Base):

    def test_target_hit_triggers_market_sell(self):
        """price ≥ target MUST call _market_sell_all."""
        pos = _open_pos(fill=10.0, stop=9.8, qty=2.0, target=12.0)
        positions, m = self._run([pos], price=12.0)
        self.assertEqual(m["sell"].call_args.args[:4], (unittest.mock.ANY, "acct", "TEST", 2.0))

    def test_target_hit_closes_position_as_target(self):
        pos = _open_pos(fill=10.0, stop=9.8, qty=2.0, target=12.0)
        positions, m = self._run([pos], price=12.5)
        self.assertEqual(pos.status, "closed")
        self.assertEqual(pos.exit_reason, "target")

    def test_target_hit_cancels_stop_order(self):
        """Broker stop order is cancelled when target is hit."""
        pos = _open_pos(fill=10.0, stop=9.8, qty=2.0, target=12.0, stop_order_id="stp-x")
        positions, m = self._run([pos], price=12.5)
        m["cancel"].assert_any_call(unittest.mock.ANY, "acct", "stp-x")

    def test_no_sell_when_below_target(self):
        pos = _open_pos(fill=10.0, stop=9.8, qty=2.0, target=12.0)
        positions, m = self._run([pos], price=11.99)
        m["sell"].assert_not_called()


# ===========================================================================
# 4. EOD force close
# ===========================================================================

class TestForceClose(_Base):

    def test_force_close_sells_open_position(self):
        """At 3:50 pm ET, open positions MUST be market-sold."""
        pos = _open_pos(fill=10.0, stop=9.8, qty=2.5)
        positions, m = self._run([pos], now=ET_FORCE_CLOSE, price=10.5)
        self.assertEqual(m["sell"].call_args.args[:4], (unittest.mock.ANY, "acct", "TEST", 2.5))

    def test_force_close_sets_exit_reason_eod(self):
        pos = _open_pos(fill=10.0, stop=9.8, qty=2.5)
        positions, m = self._run([pos], now=ET_FORCE_CLOSE, price=10.5)
        self.assertEqual(pos.status, "closed")
        self.assertEqual(pos.exit_reason, "eod")

    def test_force_close_cancels_stop_order(self):
        pos = _open_pos(fill=10.0, stop=9.8, qty=2.5, stop_order_id="stp-eod")
        positions, m = self._run([pos], now=ET_FORCE_CLOSE, price=10.5)
        m["cancel"].assert_any_call(unittest.mock.ANY, "acct", "stp-eod")

    def test_force_close_cancels_limit_order(self):
        pos = _open_pos(fill=10.0, stop=9.8, qty=2.5, limit_order_id="lim-eod")
        positions, m = self._run([pos], now=ET_FORCE_CLOSE, price=10.5)
        m["cancel"].assert_any_call(unittest.mock.ANY, "acct", "lim-eod")

    def test_force_close_records_pnl(self):
        fill, qty, exit_price = 10.0, 2.5, 10.5
        pos = _open_pos(fill=fill, stop=9.8, qty=qty)
        positions, m = self._run([pos], now=ET_FORCE_CLOSE, price=exit_price)
        self.assertAlmostEqual(pos.realized_pnl, (exit_price - fill) * qty, places=4)

    def test_watching_positions_expire_at_eod(self):
        """Watching positions (never triggered) expire at force-close time."""
        pos = _watching_pos(trigger=10.0)
        positions, m = self._run([pos], now=ET_FORCE_CLOSE, price=9.0)
        self.assertEqual(pos.status, "expired")
        m["sell"].assert_not_called()

    def test_in_session_discord_plan_carries_through_next_session(self):
        pos = _watching_pos(trigger=10.0)
        pos.discord_carry_sessions_remaining = 1

        self._run([pos], now=ET_FORCE_CLOSE, price=9.0)

        self.assertEqual(pos.status, "watching")
        self.assertFalse(pos.armed)
        self.assertEqual(pos.discord_carry_sessions_remaining, 0)
        self.assertEqual(pos.discord_carry_from_date, "2026-06-16")
        self.assertEqual(pos.exit_reason, "waiting_next_session")

    def test_carried_discord_plan_expires_at_following_session_eod(self):
        pos = _watching_pos(trigger=10.0)
        pos.discord_carry_from_date = "2026-06-15"

        self._run([pos], now=ET_FORCE_CLOSE, price=9.0)

        self.assertEqual(pos.status, "expired")
        self.assertEqual(pos.exit_reason, "eod")

    def test_repeated_force_close_poll_does_not_consume_carry_window(self):
        pos = _watching_pos(trigger=10.0)
        pos.discord_carry_from_date = "2026-06-16"
        pos.exit_reason = "waiting_next_session"

        self._run([pos], now=ET_FORCE_CLOSE, price=9.0)

        self.assertEqual(pos.status, "watching")

    def test_carried_discord_gap_waits_for_rearm_instead_of_chasing(self):
        pos = _watching_pos(trigger=10.0)
        pos.armed = False
        pos.discord_carry_from_date = "2026-06-15"

        _, first = self._run([pos], now=ET_MARKET_OPEN, price=10.5)
        self.assertEqual(pos.status, "watching")
        self.assertFalse(pos.armed)
        first["buy"].assert_not_called()

        self._run([pos], now=ET_MARKET_OPEN, price=9.99)
        self.assertTrue(pos.armed)
        _, breakout = self._run([pos], now=ET_MARKET_OPEN, price=10.01)
        self.assertEqual(pos.status, "open")
        breakout["buy"].assert_called_once()

    def test_legacy_previous_session_discord_watch_is_recovered_once(self):
        pos = _watching_pos(trigger=904.0)
        pos.ticker = "MU"
        pos.status = "expired"
        pos.plan_received_at = "2026-06-15T13:44:43+00:00"

        changed = _recover_legacy_discord_carryovers(
            [pos], datetime(2026, 6, 16, 10, 0, tzinfo=ET_MARKET_OPEN.tzinfo)
        )

        self.assertTrue(changed)
        self.assertEqual(pos.status, "watching")
        self.assertFalse(pos.armed)
        self.assertEqual(pos.discord_carry_from_date, "2026-06-15")
        self.assertFalse(_recover_legacy_discord_carryovers(
            [pos], datetime(2026, 6, 16, 10, 0, tzinfo=ET_MARKET_OPEN.tzinfo)
        ))

    def test_no_force_close_before_350pm(self):
        """Before 3:50 pm, open positions are NOT force-closed."""
        pos = _open_pos(fill=10.0, stop=9.8, qty=2.5)
        positions, m = self._run([pos], now=ET_MARKET_OPEN, price=10.5)
        m["sell"].assert_not_called()
        self.assertEqual(pos.status, "open")


class TestBrokerConfirmedExit(unittest.TestCase):

    def test_queued_sell_remains_pending_exit(self):
        pos = _open_pos(fill=10.0, stop=9.8, qty=2.0)
        session = MagicMock()
        with patch(
            "bot.day_trader._market_sell_all",
            return_value=OrderResult("sell-1", "queued", None, None, None),
        ):
            changed = _start_or_retry_exit(session, "acct", pos, "stop")
        self.assertTrue(changed)
        self.assertEqual(pos.status, "pending_exit")
        self.assertEqual(pos.exit_order_id, "sell-1")
        self.assertIsNone(pos.realized_pnl)
        self.assertIsNone(pos.closed_at)

    def test_filled_sell_uses_actual_broker_fill(self):
        pos = _open_pos(fill=10.0, stop=9.8, qty=2.0)
        pos.status = "pending_exit"
        pos.exit_reason = "stop"
        pos.exit_order_id = "sell-1"
        changed = _apply_exit_order_result(
            pos,
            OrderResult("sell-1", "filled", 9.73, 2.0, 19.46),
        )
        self.assertTrue(changed)
        self.assertEqual(pos.status, "closed")
        self.assertAlmostEqual(pos.exit_price, 9.73)
        self.assertEqual(pos.realized_pnl, -0.54)
        self.assertAlmostEqual(pos.realized_pnl_pct, -2.7)

    def test_rejected_sell_stays_pending_and_retries(self):
        pos = _open_pos(fill=10.0, stop=9.8, qty=2.0)
        pos.status = "pending_exit"
        pos.exit_reason = "stop"
        pos.exit_order_id = "sell-rejected"
        _apply_exit_order_result(
            pos,
            OrderResult("sell-rejected", "rejected", None, None, None),
        )
        self.assertEqual(pos.status, "pending_exit")
        self.assertIsNone(pos.exit_order_id)
        self.assertEqual(pos.exit_last_error, "rejected")

        with patch(
            "bot.day_trader._market_sell_all",
            return_value=OrderResult("sell-retry", "queued", None, None, None),
        ) as sell:
            _start_or_retry_exit(MagicMock(), "acct", pos, "stop")
        self.assertEqual(sell.call_args.args[3], 2.0)
        self.assertEqual(pos.exit_order_id, "sell-retry")

    def test_terminal_partial_fill_retries_only_remainder(self):
        pos = _open_pos(fill=10.0, stop=9.8, qty=2.0)
        pos.status = "pending_exit"
        pos.exit_reason = "stop"
        pos.exit_order_id = "sell-partial"
        _apply_exit_order_result(
            pos,
            OrderResult("sell-partial", "cancelled", 9.75, 0.75, 7.3125),
        )
        self.assertEqual(pos.exit_filled_qty, 0.75)
        self.assertIsNone(pos.exit_order_id)

        with patch(
            "bot.day_trader._market_sell_all",
            return_value=OrderResult("sell-rest", "filled", 9.70, 1.25, 12.125),
        ) as sell:
            _start_or_retry_exit(MagicMock(), "acct", pos, "stop")
        self.assertAlmostEqual(sell.call_args.args[3], 1.25)
        self.assertEqual(pos.status, "closed")
        self.assertAlmostEqual(pos.exit_price, (9.75 * 0.75 + 9.70 * 1.25) / 2)

    def test_submission_failure_never_marks_position_closed(self):
        pos = _open_pos(fill=10.0, stop=9.8, qty=2.0)
        with patch(
            "bot.day_trader._market_sell_all",
            side_effect=RuntimeError("temporary broker error"),
        ):
            _start_or_retry_exit(MagicMock(), "acct", pos, "stop")
        self.assertEqual(pos.status, "pending_exit")
        self.assertIsNone(pos.exit_order_id)
        self.assertIn("temporary broker error", pos.exit_last_error or "")
        self.assertIsNone(pos.realized_pnl)


# ===========================================================================
# 5. EOD stop tightening (3:30 pm)
# ===========================================================================

class TestEodTighten(_Base):

    def test_stop_tightens_after_330pm(self):
        """At 3:35 pm, stop moves to current_price × 0.99."""
        price = 11.0
        pos = _open_pos(fill=10.0, stop=9.8, qty=2.0)
        positions, m = self._run([pos], now=ET_EOD_TIGHTEN, price=price)
        expected_stop = round(price * 0.99, 4)
        self.assertAlmostEqual(pos.stop_price, expected_stop, places=4)
        self.assertTrue(pos.eod_tightened)

    def test_tighten_only_happens_once(self):
        """eod_tightened flag prevents a second tighten on the next poll."""
        pos = _open_pos(fill=10.0, stop=9.8, qty=2.0, eod_tightened=True)
        original_stop = pos.stop_price
        positions, m = self._run([pos], now=ET_EOD_TIGHTEN, price=11.0)
        self.assertEqual(pos.stop_price, original_stop)  # unchanged

    def test_tighten_does_not_lower_stop(self):
        """If current_price × 0.99 < existing stop, stop is NOT lowered."""
        price = 9.0
        pos = _open_pos(fill=10.0, stop=9.8, qty=2.0)  # stop already at 9.8
        # price * 0.99 = 8.91 < 9.8 — should NOT replace the stop
        positions, m = self._run([pos], now=ET_EOD_TIGHTEN, price=price)
        self.assertEqual(pos.stop_price, 9.8)  # unchanged


# ===========================================================================
# 6. Trailing stop milestones
# ===========================================================================

class TestTrailingStop(_Base):

    def test_first_milestone_upgrades_on_first_poll(self):
        """The +1% risk-reduction milestone needs only one 5-second poll."""
        fill = 10.0
        pos = _open_pos(fill=fill, stop=fill * 0.98, qty=2.0)
        threshold = fill * (1 + _TRAILING_MILESTONES[0][0] / 100)  # +1%
        positions, m = self._run([pos], price=threshold)
        self.assertEqual(_FIRST_MILESTONE_CONFIRM_POLLS, 1)
        self.assertEqual(pos.confirm_count, 0)
        self.assertEqual(pos.milestone_idx, 1)
        self.assertEqual(pos.stop_price, round(fill * 0.995, 4))

    def test_later_milestone_still_requires_second_confirm(self):
        """The +2% and later milestones retain two-poll confirmation."""
        fill = 10.0
        lock_in_pct = _TRAILING_MILESTONES[1][1]
        pos = _open_pos(fill=fill, stop=fill * 0.995, qty=2.0,
                        milestone_idx=1,
                        confirm_count=_CONFIRM_POLLS - 1,
                        confirm_milestone_idx=1)
        threshold = fill * (1 + _TRAILING_MILESTONES[1][0] / 100)
        positions, m = self._run([pos], price=threshold + 0.01)
        expected_new_stop = round(fill * (1 + lock_in_pct / 100), 4)
        self.assertAlmostEqual(pos.stop_price, expected_new_stop, places=4)
        self.assertEqual(pos.milestone_idx, 2)
        self.assertEqual(pos.confirm_count, 0)

    def test_dip_resets_confirm_count(self):
        """If price dips below milestone threshold, confirm_count resets."""
        fill = 10.0
        pos = _open_pos(
            fill=fill, stop=fill * 0.98, qty=2.0,
            confirm_count=1, confirm_milestone_idx=0,
        )
        below_threshold = fill * 1.005  # below +1% milestone
        positions, m = self._run([pos], price=below_threshold)
        self.assertEqual(pos.confirm_count, 0)

    def test_stop_upgrade_cancels_old_broker_stop(self):
        """When trailing stop upgrades, old broker stop order is cancelled."""
        fill = 10.0
        pos = _open_pos(fill=fill, stop=fill * 0.98, qty=2.0,
                        stop_order_id="old-stop",
                        confirm_count=_CONFIRM_POLLS - 1,
                        confirm_milestone_idx=0)
        threshold = fill * (1 + _TRAILING_MILESTONES[0][0] / 100)
        positions, m = self._run([pos], price=threshold + 0.01)
        m["cancel"].assert_any_call(unittest.mock.ANY, "acct", "old-stop")

    def test_multiple_milestones_advance_correctly(self):
        """Starting at milestone 1, confirming +2% advances to milestone 2."""
        fill = 10.0
        pos = _open_pos(fill=fill, stop=round(fill * 0.995, 4), qty=2.0,
                        milestone_idx=1, confirm_count=_CONFIRM_POLLS - 1,
                        confirm_milestone_idx=1)
        threshold = fill * (1 + _TRAILING_MILESTONES[1][0] / 100)  # +2%
        positions, m = self._run([pos], price=threshold + 0.01)
        self.assertEqual(pos.milestone_idx, 2)
        self.assertEqual(pos.stop_price, round(fill * 1.002, 4))

    def test_early_risk_reduction_policy_is_explicit(self):
        self.assertEqual(
            _TRAILING_MILESTONES[:4],
            [(1.0, -0.5), (2.0, 0.2), (3.0, 0.5), (6.0, 3.0)],
        )

    def test_confirmed_three_percent_locks_half_percent(self):
        fill = 10.0
        pos = _open_pos(
            fill=fill,
            stop=round(fill * 1.002, 4),
            qty=2.0,
            milestone_idx=2,
            confirm_count=_CONFIRM_POLLS - 1,
            confirm_milestone_idx=2,
        )
        positions, m = self._run([pos], price=fill * 1.031)
        self.assertEqual(pos.milestone_idx, 3)
        self.assertEqual(pos.stop_price, round(fill * 1.005, 4))

    def test_price_jump_confirms_highest_reached_milestone_without_delay(self):
        fill = 10.0
        pos = _open_pos(
            fill=fill,
            stop=fill * 0.98,
            qty=2.0,
            milestone_idx=0,
            confirm_count=_CONFIRM_POLLS - 1,
            confirm_milestone_idx=2,
        )
        self._run([pos], price=fill * 1.031)
        self.assertEqual(pos.milestone_idx, 3)
        self.assertEqual(pos.stop_price, round(fill * 1.005, 4))

    def test_old_policy_position_keeps_existing_stop_and_skips_lower_steps(self):
        pos = _open_pos(fill=10.0, stop=10.05, qty=2.0, milestone_idx=1)
        pos.stop_policy_version = 1
        self.assertTrue(_migrate_stop_policy(pos))
        self.assertEqual(pos.stop_price, 10.05)
        self.assertEqual(pos.milestone_idx, 3)  # next useful step is +6% -> +3%
        self.assertEqual(pos.stop_policy_version, _STOP_POLICY_VERSION)

    def test_legacy_json_is_marked_for_policy_migration(self):
        data = _open_pos(fill=10.0, stop=10.05, qty=2.0).to_dict()
        data.pop("stop_policy_version")
        restored = DayPosition.from_dict(data)
        self.assertEqual(restored.stop_policy_version, 1)


# ===========================================================================
# 7. Market hours guard
# ===========================================================================

class TestMarketHours(_Base):

    def test_no_orders_outside_market_hours(self):
        """After hours: no buy, no sell — just plan ingestion."""
        pos = _watching_pos(trigger=10.0)
        positions, m = self._run([pos], now=ET_AFTER_HOURS, price=10.5)
        m["buy"].assert_not_called()
        m["sell"].assert_not_called()
        self.assertEqual(pos.status, "watching")

    def test_no_orders_on_weekend(self):
        pos = _open_pos(fill=10.0, stop=9.8, qty=2.0)
        positions, m = self._run([pos], now=ET_WEEKEND, price=9.0)
        m["sell"].assert_not_called()
        self.assertEqual(pos.status, "open")

    def test_already_closed_positions_skipped(self):
        """Closed/expired positions are never re-processed."""
        pos = _open_pos()
        pos.status = "closed"
        positions, m = self._run([pos], price=5.0)  # way below stop
        m["sell"].assert_not_called()


# ===========================================================================
# 8. State persistence
# ===========================================================================

class TestPersistence(_Base):

    def test_flush_called_when_price_changes(self):
        """_flush_positions is called when current_price updates."""
        pos = _open_pos(fill=10.0, stop=9.8, qty=2.0)
        pos.current_price = 9.0  # old price
        positions, m = self._run([pos], price=10.5)  # new price
        m["flush"].assert_called()

    def test_flush_called_on_stop(self):
        """State is flushed to disk after a stop triggers."""
        pos = _open_pos(fill=10.0, stop=9.8, qty=2.0)
        positions, m = self._run([pos], price=9.7)
        m["flush"].assert_called()

    def test_flush_called_on_entry(self):
        """State is flushed to disk after entry."""
        pos = _watching_pos(trigger=10.0)
        positions, m = self._run([pos], price=10.0)
        m["flush"].assert_called()

    def test_flush_called_on_force_close(self):
        pos = _open_pos(fill=10.0, stop=9.8, qty=2.5)
        positions, m = self._run([pos], now=ET_FORCE_CLOSE, price=10.5)
        m["flush"].assert_called()


# ===========================================================================
# 9. Plan loading
# ===========================================================================

class TestPlanLoading(_Base):

    def test_loader_keeps_long_and_discards_short_or_missing_side(self):
        """The JSONL boundary fails closed before a Discord plan reaches execution."""
        received_at = datetime.now(timezone.utc).isoformat()
        records = [
            {
                "kind": "PLAN", "ticker": "KLAC", "side": "SHORT",
                "trigger": 210.86, "target": 194.0, "received_at": received_at,
                "discord": {"message_id": "short-1"},
            },
            {
                "kind": "PLAN", "ticker": "MISSING", "side": None,
                "trigger": 10.0, "received_at": received_at,
                "discord": {"message_id": "missing-1"},
            },
            {
                "kind": "PLAN", "ticker": "LONG", "side": "LONG",
                "trigger": 50.0, "received_at": received_at,
                "discord": {"message_id": "long-1"},
            },
        ]

        signals_log = MagicMock()
        signals_log.exists.return_value = True
        signals_log.read_text.return_value = "\n".join(
            json.dumps(record) for record in records
        )
        seen_ids: set[str] = set()
        with patch("bot.day_trader.SIGNALS_LOG", signals_log):
            plans = _load_new_plans(seen_ids)

        self.assertEqual([plan.ticker for plan in plans], ["LONG"])
        self.assertEqual(plans[0].side.value, "LONG")
        self.assertEqual(seen_ids, {"short-1", "missing-1", "long-1"})

    def test_new_plan_added_to_positions(self):
        """A new PLAN signal creates a watching DayPosition."""
        from bot.parser import Side, Signal, SignalKind
        sig = Signal(kind=SignalKind.PLAN, ticker="NEW", trigger=50.0,
                     side=Side.LONG,
                     received_at=ET_MARKET_OPEN)
        sig.message_id = "sig-999"  # type: ignore
        positions = []
        self._run(positions, new_plans=[sig], price=49.0)
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0].ticker, "NEW")
        self.assertEqual(positions[0].status, "watching")
        self.assertEqual(positions[0].discord_carry_sessions_remaining, 1)

    def test_after_hours_plan_is_for_next_session_only(self):
        from bot.parser import Side, Signal, SignalKind
        sig = Signal(
            kind=SignalKind.PLAN,
            ticker="NEXT",
            trigger=50.0,
            side=Side.LONG,
            received_at=ET_AFTER_HOURS,
        )
        sig.message_id = "sig-next"  # type: ignore
        positions = []

        self._run(positions, new_plans=[sig], price=49.0)

        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0].discord_carry_sessions_remaining, 0)

    def test_short_plan_never_creates_position_or_order(self):
        """A Discord SHORT alert must not be reinterpreted as a long buy."""
        from bot.parser import Side, Signal, SignalKind
        sig = Signal(
            kind=SignalKind.PLAN,
            ticker="KLAC",
            side=Side.SHORT,
            trigger=210.86,
            target=194.0,
            received_at=datetime.now(timezone.utc),
        )
        sig.message_id = "sig-klac-short"  # type: ignore

        positions, mocks = self._run([], new_plans=[sig], price=211.0)

        self.assertEqual(positions, [])
        mocks["append"].assert_not_called()
        mocks["buy"].assert_not_called()

    def test_plan_without_explicit_side_fails_closed(self):
        """Missing direction is unsafe and must not fall back to LONG."""
        from bot.parser import Signal, SignalKind
        sig = Signal(
            kind=SignalKind.PLAN,
            ticker="UNKNOWN",
            trigger=50.0,
            received_at=datetime.now(timezone.utc),
        )

        positions, mocks = self._run([], new_plans=[sig], price=51.0)

        self.assertEqual(positions, [])
        mocks["buy"].assert_not_called()

    def test_plan_for_already_watching_ticker_skipped(self):
        """Duplicate plan for same ticker doesn't create a second position."""
        from bot.parser import Side, Signal, SignalKind
        existing = _watching_pos(trigger=50.0)
        existing.ticker = "DUP"
        sig = Signal(kind=SignalKind.PLAN, ticker="DUP", trigger=51.0,
                     side=Side.LONG,
                     received_at=datetime.now(timezone.utc))
        sig.message_id = "sig-dup"  # type: ignore
        positions = [existing]
        self._run(positions, new_plans=[sig], price=49.0)
        self.assertEqual(len(positions), 1)  # still just one

    def test_append_position_called_for_new_plan(self):
        """_append_position is called for each new plan."""
        from bot.parser import Side, Signal, SignalKind
        sig = Signal(kind=SignalKind.PLAN, ticker="APP", trigger=50.0,
                     side=Side.LONG,
                     received_at=datetime.now(timezone.utc))
        sig.message_id = "sig-app"  # type: ignore
        positions = []
        positions, m = self._run(positions, new_plans=[sig], price=49.0)
        m["append"].assert_called_once()


# ===========================================================================
# 10. MCP failure resilience
# ===========================================================================

class TestResilience(_Base):

    def test_mcp_session_failure_returns_positions_unchanged(self):
        """If MCP session fails to open, positions are untouched."""
        pos = _open_pos(fill=10.0, stop=9.8, qty=2.0)
        original_status = pos.status
        with patch("bot.day_trader._load_new_plans", return_value=[]), \
             patch("bot.day_trader._load_token", side_effect=Exception("token expired")), \
             patch("bot.day_trader._flush_positions"), \
             patch("bot.day_trader.datetime") as m_dt:
            m_dt.now.return_value = ET_MARKET_OPEN
            run_once([pos], set())
        self.assertEqual(pos.status, original_status)
        self.assertIsNone(pos.closed_at)

    def test_price_fetch_failure_skips_position(self):
        """If price fetch fails, that position is skipped for this cycle."""
        pos = _open_pos(fill=10.0, stop=9.8, qty=2.0)
        with patch("bot.day_trader._load_new_plans", return_value=[]), \
             patch("bot.day_trader._load_token", return_value="tok"), \
             patch("bot.day_trader._MCPSession", return_value=MagicMock()), \
             patch("bot.day_trader._get_agentic_account", return_value="acct"), \
             patch("bot.day_trader._get_prices", side_effect=Exception("network err")), \
             patch("bot.day_trader._market_sell_all") as m_sell, \
             patch("bot.day_trader._flush_positions"), \
             patch("bot.day_trader._append_position"), \
             patch("bot.day_trader.datetime") as m_dt:
            m_dt.now.return_value = ET_MARKET_OPEN
            run_once([pos], set())
        m_sell.assert_not_called()
        self.assertEqual(pos.status, "open")

    def test_buy_failure_remains_pending_for_idempotent_recovery(self):
        """An ambiguous submit never retries as a fresh logical order."""
        from bot.robinhood_mcp_client import RobinhoodMCPError
        pos = _watching_pos(trigger=10.0)
        with patch("bot.day_trader._load_new_plans", return_value=[]), \
             patch("bot.day_trader._load_token", return_value="tok"), \
             patch("bot.day_trader._MCPSession", return_value=MagicMock()), \
             patch("bot.day_trader._get_agentic_account", return_value="acct"), \
             patch("bot.day_trader._get_prices", return_value={"TEST": 10.0}), \
             patch("bot.day_trader._validate_entry_preflight",
                   return_value=(10.0, 9.99, 10.0, 0.1)), \
             patch("bot.day_trader._place_fractional_market_buy",
                   side_effect=RobinhoodMCPError("order rejected")), \
             patch("bot.day_trader._flush_positions"), \
             patch("bot.day_trader._append_position"), \
             patch("bot.day_trader.datetime") as m_dt:
            m_dt.now.return_value = ET_MARKET_OPEN
            run_once([pos], set())
        self.assertEqual(pos.status, "pending_entry")
        self.assertIn("submission_ambiguous", pos.entry_last_error or "")

    def test_preflight_rejection_returns_manual_watch_to_rearm(self):
        pos = _watching_pos(trigger=10.0)
        pos.source = "manual"
        pos.manual_plan_id = "manual-1"
        pos.good_til_cancelled = True
        pos.entry_limit_price = 10.02
        rejection = EntryPreflightRejected(
            "ask_above_cap", "TEST ask 10.03 exceeds entry cap 10.02"
        )
        with patch(
            "bot.day_trader._validate_entry_preflight", side_effect=rejection
        ), patch("bot.day_trader._place_fractional_market_buy") as place:
            changed = _submit_or_recover_entry(MagicMock(), "acct", pos)
        self.assertTrue(changed)
        place.assert_not_called()
        self.assertEqual(pos.status, "watching")
        self.assertFalse(pos.armed)
        self.assertEqual(pos.entry_attempt_no, 1)
        self.assertEqual(pos.exit_reason, "waiting_rearm_after_ask_above_cap")

    def test_preflight_outage_keeps_watch_armed_for_retry(self):
        pos = _watching_pos(trigger=10.0)
        pos.entry_limit_price = 10.02
        with patch(
            "bot.day_trader._validate_entry_preflight",
            side_effect=EntryPreflightUnavailable("quote timeout"),
        ), patch("bot.day_trader._place_fractional_market_buy") as place:
            changed = _submit_or_recover_entry(MagicMock(), "acct", pos)
        self.assertTrue(changed)
        place.assert_not_called()
        self.assertEqual(pos.status, "watching")
        self.assertTrue(pos.armed)
        self.assertEqual(pos.entry_attempt_no, 0)
        self.assertIn("preflight_unavailable", pos.entry_last_error or "")

    def test_explicit_broker_rejection_does_not_retry_every_five_seconds(self):
        from bot.robinhood_mcp_client import RobinhoodMCPError
        pos = _watching_pos(trigger=10.0)
        pos.source = "manual"
        pos.manual_plan_id = "manual-1"
        pos.good_til_cancelled = True
        pos.entry_limit_price = 10.02
        error = RobinhoodMCPError(
            "'place_equity_order' returned isError: buying power unavailable"
        )
        with patch(
            "bot.day_trader._validate_entry_preflight",
            return_value=(10.0, 9.99, 10.0, 0.1),
        ), patch("bot.day_trader._append_position"), patch(
            "bot.day_trader._place_fractional_market_buy", side_effect=error
        ) as place:
            changed = _submit_or_recover_entry(MagicMock(), "acct", pos)
        self.assertTrue(changed)
        self.assertEqual(place.call_count, 1)
        self.assertEqual(pos.status, "watching")
        self.assertFalse(pos.armed)
        self.assertEqual(pos.entry_attempt_no, 1)
        self.assertEqual(pos.exit_reason, "waiting_rearm_after_broker_rejected")

    def test_legacy_nvda_rejection_recovers_even_after_hours(self):
        pos = _watching_pos(trigger=212.70)
        pos.ticker = "NVDA"
        pos.status = "pending_entry"
        pos.source = "manual"
        pos.manual_plan_id = "manual-nvda"
        pos.good_til_cancelled = True
        pos.entry_limit_price = 213.13
        pos.entry_submitted_at = "2026-07-15T13:32:14+00:00"
        pos.entry_last_error = (
            "submission_ambiguous:'place_equity_order' returned isError: "
            "dollar_amount is only supported for plain market orders"
        )
        _, mocks = self._run([pos], now=ET_AFTER_HOURS, price=212.49)
        mocks["buy"].assert_not_called()
        self.assertEqual(pos.status, "watching")
        self.assertFalse(pos.armed)
        self.assertEqual(pos.entry_attempt_no, 1)
        self.assertIsNone(pos.entry_last_error)


class TestProtectedEntryAndPolling(unittest.TestCase):

    def test_pltr_prefers_pltu_over_tighter_underlying_fallback(self):
        session = MagicMock()
        session.call.side_effect = [
            {"data": {"results": [
                {"symbol": "PLTU", "quote": {
                    "last_trade_price": "32.00", "bid_price": "31.98",
                    "ask_price": "32.02", "average_volume_30_days": "2000000",
                }},
                {"symbol": "PLTR", "quote": {
                    "last_trade_price": "138.00", "bid_price": "137.99",
                    "ask_price": "138.01", "average_volume_30_days": "50000000",
                }},
            ]}},
            {"data": {"results": [
                {"symbol": "PLTU", "tradeable": True, "fractional_tradability": "tradable"},
                {"symbol": "PLTR", "tradeable": True, "fractional_tradability": "tradable"},
            ]}},
        ]

        selected = _select_leveraged_etf(session, "acct", "PLTR", "long")

        self.assertEqual(selected.ticker, "PLTU")
        self.assertEqual(selected.leverage, 2.0)

    def test_curated_pltu_does_not_require_quote_volume(self):
        session = MagicMock()
        session.call.side_effect = [
            {"data": {"results": [
                {"symbol": "PLTU", "quote": {
                    "last_trade_price": "32.00", "bid_price": "31.98",
                    "ask_price": "32.02",
                }},
                {"symbol": "PLTR", "quote": {
                    "last_trade_price": "138.00", "bid_price": "137.99",
                    "ask_price": "138.01", "average_volume_30_days": "50000000",
                }},
            ]}},
            {"data": {"results": [
                {"symbol": "PLTU", "tradeable": True, "fractional_tradability": "tradable"},
                {"symbol": "PLTR", "tradeable": True, "fractional_tradability": "tradable"},
            ]}},
        ]

        selected = _select_leveraged_etf(session, "acct", "PLTR", "long")

        self.assertEqual(selected.ticker, "PLTU")
        self.assertEqual(selected.leverage, 2.0)
        self.assertEqual(selected.liquidity_basis, "curated_liquid_route")

    def test_spxl_missing_quote_volume_still_routes_spy_to_spxl(self):
        session = MagicMock()
        session.call.side_effect = [
            {"data": {"results": [
                {"symbol": "SPXL", "quote": {
                    "last_trade_price": "271.31", "bid_price": "271.30",
                    "ask_price": "271.39",
                }},
                {"symbol": "SPY", "quote": {
                    "last_trade_price": "748.61", "bid_price": "748.61",
                    "ask_price": "748.63",
                }},
            ]}},
            {"data": {"results": [
                {"symbol": "SPXL", "tradeable": True, "fractional_tradability": "tradable"},
                {"symbol": "SPY", "tradeable": True, "fractional_tradability": "tradable"},
            ]}},
        ]

        with self.assertLogs("bot.day_trader", level="INFO") as logs:
            selected = _select_leveraged_etf(session, "acct", "SPY", "long")

        self.assertEqual(selected.ticker, "SPXL")
        self.assertIsNone(selected.volume)
        self.assertEqual(selected.liquidity_basis, "curated_liquid_route")
        self.assertIn("selected=SPXL", "\n".join(logs.output))
        self.assertIn("volume=missing", "\n".join(logs.output))

    def test_non_curated_leveraged_route_still_requires_volume(self):
        session = MagicMock()
        session.call.side_effect = [
            {"data": {"results": [
                {"symbol": "NVDU", "quote": {
                    "last_trade_price": "18.00", "bid_price": "17.99",
                    "ask_price": "18.01",
                }},
                {"symbol": "NVDA", "quote": {
                    "last_trade_price": "190.00", "bid_price": "189.99",
                    "ask_price": "190.01",
                }},
            ]}},
            {"data": {"results": [
                {"symbol": "NVDU", "tradeable": True, "fractional_tradability": "tradable"},
                {"symbol": "NVDA", "tradeable": True, "fractional_tradability": "tradable"},
            ]}},
        ]

        with patch(
            "bot.day_trader.execution_candidates",
            return_value=(
                LeveragedETF("NVDU", 2.0),
                LeveragedETF("NVDA", 1.0),
            ),
        ):
            selected = _select_leveraged_etf(session, "acct", "NVDA", "long")

        self.assertEqual(selected.ticker, "NVDA")
        self.assertEqual(selected.liquidity_basis, "underlying_fallback")

    def test_selects_tightest_spread_fractional_leveraged_etf(self):
        session = MagicMock()
        session.call.side_effect = [
            {"data": {"results": [
                {"symbol": "NVDL", "quote": {
                    "last_trade_price": "64.00", "bid_price": "63.94",
                    "ask_price": "64.06", "volume": "2000000",
                }},
                {"symbol": "NVDX", "quote": {
                    "last_trade_price": "25.00", "bid_price": "24.99",
                    "ask_price": "25.01", "volume": "1500000",
                }},
                {"symbol": "NVDU", "quote": {
                    "last_trade_price": "18.00", "bid_price": "17.99",
                    "ask_price": "18.01", "volume": "3000000",
                }},
            ]}},
            {"data": {"results": [
                {"symbol": "NVDL", "tradeable": True, "fractional_tradability": "tradable"},
                {"symbol": "NVDX", "tradeable": True, "fractional_tradability": "tradable"},
                {"symbol": "NVDU", "tradeable": True, "fractional_tradability": "tradable"},
            ]}},
        ]
        selected = _select_leveraged_etf(session, "acct", "NVDA", "long")
        self.assertEqual(selected.ticker, "NVDX")
        self.assertLess(selected.spread_pct, 0.1)

    def test_heat_entry_buys_and_protects_execution_etf(self):
        pos = _watching_pos(trigger=150.0)
        pos.source = "heat"
        pos.heat_idea_id = "heat-1"
        pos.entry_limit_price = 150.30
        selection = LeveragedETFSelection(
            "NVDL", 2.0, 64.0, 63.99, 64.01, 0.03125, 2_000_000
        )
        fill = OrderResult("buy-etf", "filled", 64.0, 0.3125, 20.0)
        with patch(
            "bot.day_trader._validate_entry_preflight",
            return_value=(150.0, 149.99, 150.01, 0.013),
        ), patch(
            "bot.day_trader._select_leveraged_etf", return_value=selection
        ), patch(
            "bot.day_trader._place_fractional_market_buy", return_value=fill
        ) as buy, patch(
            "bot.day_trader._place_stop_order", return_value=None
        ) as stop, patch("bot.day_trader._append_position"):
            changed = _submit_or_recover_entry(MagicMock(), "acct", pos)
        self.assertTrue(changed)
        self.assertEqual(pos.execution_ticker, "NVDL")
        self.assertEqual(pos.status, "open")
        self.assertEqual(buy.call_args.args[2], "NVDL")
        self.assertEqual(stop.call_args.args[2], "NVDL")
        self.assertAlmostEqual(pos.stop_price or 0, 62.72)

    def test_batch_quotes_use_one_request_for_multiple_tickers(self):
        session = MagicMock()
        session.call.return_value = {"data": {"results": [
            {"symbol": "AAPL", "quote": {"last_trade_price": "317.30"}},
            {"symbol": "NVDA", "quote": {"last_trade_price": "190.50"}},
        ]}}
        prices = _get_prices(session, ["AAPL", "NVDA"])
        session.call.assert_called_once_with(
            "get_equity_quotes", symbols=["AAPL", "NVDA"]
        )
        self.assertEqual(prices, {"AAPL": 317.30, "NVDA": 190.50})

    def test_batch_quotes_fall_back_to_request_order_when_symbol_is_omitted(self):
        session = MagicMock()
        session.call.return_value = {"data": {"results": [
            {"quote": {"last_trade_price": "317.30"}},
            {"quote": {"last_trade_price": "190.50"}},
        ]}}
        self.assertEqual(
            _get_prices(session, ["AAPL", "NVDA"]),
            {"AAPL": 317.30, "NVDA": 190.50},
        )

    def test_runtime_schedules_each_ticker_independently(self):
        runtime = DayTraderRuntime()
        near = _watching_pos(trigger=10.0)
        near.current_price = 9.96
        far = _watching_pos(trigger=10.0)
        far.current_price = 9.0
        with patch("bot.day_trader.time.monotonic", return_value=100.0):
            runtime.schedule(near)
            runtime.schedule(far)
        self.assertEqual(runtime.next_due[near.id], 100.0 + NEAR_POLL_INTERVAL_S)
        self.assertEqual(runtime.next_due[far.id], 105.0)


class TestManualDayWatches(_Base):

    @staticmethod
    def _manual_watch(*, armed: bool = False) -> DayPosition:
        pos = _watching_pos(trigger=10.0)
        pos.source = "manual"
        pos.manual_plan_id = "manual-1"
        pos.good_til_cancelled = True
        pos.armed = armed
        return pos

    def test_sync_creates_unarmed_persistent_watch(self):
        plans = [{
            "id": "manual-1",
            "ticker": "GTLB",
            "trigger_price": 34.06,
            "target_price": None,
            "setup": "Breakout",
            "status": "active",
            "created_at": "2026-07-14T12:00:00+00:00",
        }]
        positions: list[DayPosition] = []
        with patch("bot.day_trader.load_plans", return_value=plans), \
             patch("bot.day_trader._append_position") as append:
            changed = _sync_manual_plans(positions)
        self.assertTrue(changed)
        self.assertEqual(len(positions), 1)
        pos = positions[0]
        self.assertEqual(pos.ticker, "GTLB")
        self.assertEqual(pos.source, "manual")
        self.assertTrue(pos.good_til_cancelled)
        self.assertFalse(pos.armed)
        append.assert_called_once_with(pos)

    def test_new_watch_above_trigger_does_not_buy(self):
        pos = self._manual_watch(armed=False)
        _, mocks = self._run([pos], price=10.01)
        self.assertEqual(pos.status, "watching")
        self.assertFalse(pos.armed)
        mocks["buy"].assert_not_called()

    def test_watch_arms_below_then_buys_on_later_breakout(self):
        pos = self._manual_watch(armed=False)
        _, first = self._run([pos], price=9.99)
        self.assertTrue(pos.armed)
        first["buy"].assert_not_called()

        _, second = self._run([pos], price=10.0)
        self.assertEqual(pos.status, "open")
        second["buy"].assert_called_once()

    def test_gap_does_not_chase_and_waits_to_rearm(self):
        pos = self._manual_watch(armed=True)
        _, mocks = self._run([pos], price=10.50)
        self.assertEqual(pos.status, "watching")
        self.assertFalse(pos.armed)
        self.assertEqual(pos.exit_reason, "waiting_rearm_after_gap")
        mocks["buy"].assert_not_called()

    def test_manual_watch_survives_end_of_day(self):
        pos = self._manual_watch(armed=True)
        self._run([pos], now=ET_FORCE_CLOSE, price=9.99)
        self.assertEqual(pos.status, "watching")

    def test_registry_cancel_expires_unfilled_watch(self):
        pos = self._manual_watch(armed=True)
        cancelled = [{
            "id": "manual-1",
            "ticker": "TEST",
            "trigger_price": 10.0,
            "status": "cancelled",
        }]
        with patch("bot.day_trader.load_plans", return_value=cancelled):
            changed = _sync_manual_plans([pos])
        self.assertTrue(changed)
        self.assertEqual(pos.status, "expired")
        self.assertEqual(pos.exit_reason, "manual_cancel")

    def test_cancelled_partial_fill_is_exited_safely(self):
        pos = self._manual_watch(armed=True)
        pos.status = "pending_entry"
        pos.buy_order_id = "buy-partial"
        pos.entry_submitted_at = ET_MARKET_OPEN.isoformat()
        pos.manual_cancel_requested = True
        with patch(
            "bot.day_trader._poll_order",
            return_value=OrderResult(
                "buy-partial", "cancelled", 10.01, 0.75, 7.5075
            ),
        ):
            _, mocks = self._run([pos], price=10.0)
        self.assertEqual(pos.status, "closed")
        self.assertEqual(pos.exit_reason, "manual")
        self.assertEqual(pos.fill_qty, 0.75)
        mocks["sell"].assert_called_once()


class TestProtectedEntryHelpers(unittest.TestCase):

    def test_runtime_reuses_mcp_session_and_account(self):
        runtime = DayTraderRuntime()
        session = MagicMock()
        with patch("bot.day_trader._load_token", return_value="tok") as load_token, \
             patch("bot.day_trader._MCPSession", return_value=session) as make_session, \
             patch("bot.day_trader._get_agentic_account", return_value="acct") as get_account:
            first = runtime.connection()
            second = runtime.connection()
        self.assertEqual(first, second)
        load_token.assert_called_once()
        make_session.assert_called_once_with("tok")
        get_account.assert_called_once_with(session)

    def test_fractional_buy_uses_supported_market_order_shape(self):
        session = MagicMock()
        session.call.side_effect = [
            {"data": {"order": {"id": "order-1", "state": "queued"}}},
            {"data": {"orders": [{
                "id": "order-1",
                "state": "filled",
                "average_price": "10.01",
                "cumulative_quantity": "1.998",
            }]}},
        ]
        with patch("bot.day_trader.time.sleep"):
            result = _place_fractional_market_buy(
                session, "acct", "TEST", 20, 10.02, "position-1"
            )
        kwargs = session.call.call_args_list[0].kwargs
        self.assertEqual(kwargs["type"], "market")
        self.assertEqual(kwargs["dollar_amount"], "20.00")
        self.assertEqual(kwargs["market_hours"], "regular_hours")
        self.assertNotIn("limit_price", kwargs)
        self.assertNotIn("quantity", kwargs)
        self.assertEqual(result.fill_price, 10.01)

    def test_preflight_accepts_price_inside_cap_with_tight_spread(self):
        session = MagicMock()
        session.call.return_value = {"data": {"results": [{"quote": {
            "last_trade_price": "212.77",
            "bid_price": "212.76",
            "ask_price": "212.78",
        }}]}}
        last, bid, ask, spread = _validate_entry_preflight(
            session, "NVDA", 212.70, 213.13
        )
        self.assertEqual((last, bid, ask), (212.77, 212.76, 212.78))
        self.assertLess(spread, ENTRY_MAX_SPREAD_PCT)

    def test_preflight_rejects_ask_above_cap(self):
        session = MagicMock()
        session.call.return_value = {"data": {"results": [{"quote": {
            "last_trade_price": "213.10",
            "bid_price": "213.12",
            "ask_price": "213.14",
        }}]}}
        with self.assertRaises(EntryPreflightRejected) as raised:
            _validate_entry_preflight(session, "NVDA", 212.70, 213.13)
        self.assertEqual(raised.exception.reason, "ask_above_cap")

    def test_preflight_rejects_wide_spread(self):
        session = MagicMock()
        session.call.return_value = {"data": {"results": [{"quote": {
            "last_trade_price": "10.01",
            "bid_price": "9.98",
            "ask_price": "10.02",
        }}]}}
        with self.assertRaises(EntryPreflightRejected) as raised:
            _validate_entry_preflight(session, "TEST", 10.00, 10.02)
        self.assertEqual(raised.exception.reason, "spread_too_wide")

    def test_bearish_preflight_accepts_break_below_inside_floor(self):
        session = MagicMock()
        session.call.return_value = {"data": {"results": [{"quote": {
            "last_trade_price": "149.90",
            "bid_price": "149.89",
            "ask_price": "149.91",
        }}]}}
        last, _, _, _ = _validate_entry_preflight(
            session, "NVDA", 150.0, 149.70, "below"
        )
        self.assertEqual(last, 149.90)

    def test_preflight_missing_quote_is_temporarily_unavailable(self):
        session = MagicMock()
        session.call.return_value = {"data": {"results": []}}
        with self.assertRaises(EntryPreflightUnavailable):
            _validate_entry_preflight(session, "NVDA", 212.70, 213.13)

    def test_far_watching_position_uses_slow_poll(self):
        pos = _watching_pos(trigger=10.0)
        pos.current_price = 9.0
        self.assertEqual(_position_poll_interval(pos), FAR_POLL_INTERVAL_S)

    def test_near_watching_position_uses_fast_poll(self):
        pos = _watching_pos(trigger=10.0)
        pos.current_price = 9.96
        self.assertEqual(_position_poll_interval(pos), NEAR_POLL_INTERVAL_S)

    def test_open_position_uses_fast_poll_for_risk_management(self):
        self.assertEqual(_position_poll_interval(_open_pos()), NEAR_POLL_INTERVAL_S)


if __name__ == "__main__":
    unittest.main(verbosity=2)
