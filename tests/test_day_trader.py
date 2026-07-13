"""
Unit tests for bot/day_trader.py — dry-run with mocked MCP calls.

Every test patches the network/IO layer so no real orders are placed.
The primary contract verified in each test:

  "If condition X is true, _market_sell_all / _place_limit_buy /
   _cancel_order MUST be called with correct arguments and the position
   state MUST transition correctly."
"""

from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, call, patch

# ---------------------------------------------------------------------------
# Make bot package importable from the project root without installing.
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.day_trader import (
    DAY_TRADE_BUDGET_USD,
    _CONFIRM_POLLS,
    _INITIAL_STOP_PCT,
    _TRAILING_MILESTONES,
    ENTRY_LIMIT_OFFSET_PCT,
    FAR_POLL_INTERVAL_S,
    NEAR_POLL_INTERVAL_S,
    DayTraderRuntime,
    DayPosition,
    _apply_exit_order_result,
    _get_prices,
    _position_poll_interval,
    _place_limit_buy,
    _start_or_retry_exit,
    run_once,
)
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
              milestone_idx=0, confirm_count=0, eod_tightened=False) -> DayPosition:
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
        patch("bot.day_trader._place_limit_buy", return_value=buy_result),
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

    def _run(self, positions, now=ET_MARKET_OPEN, price=10.0, buy_result=None, new_plans=None):
        """Run run_once with all I/O mocked. Returns (positions, mock_namespace)."""
        if buy_result is None:
            buy_result = OrderResult(
                order_id="buy-001", state="filled",
                fill_price=price, fill_qty=2.0, fill_usd=price * 2,
            )
        mocks = {}
        with patch("bot.day_trader._load_new_plans", return_value=new_plans or []) as m_plans, \
             patch("bot.day_trader._load_token", return_value="tok") as m_tok, \
             patch("bot.day_trader._MCPSession", return_value=MagicMock()) as m_sess, \
             patch("bot.day_trader._get_agentic_account", return_value="acct") as m_acct, \
             patch("bot.day_trader._get_prices", return_value={"TEST": price}) as m_price, \
             patch("bot.day_trader._place_limit_buy", return_value=buy_result) as m_buy, \
             patch("bot.day_trader._place_stop_order", return_value="stop-001") as m_stop, \
             patch("bot.day_trader._place_limit_sell", return_value="limit-001") as m_lim, \
             patch("bot.day_trader._cancel_order") as m_cancel, \
             patch("bot.day_trader._market_sell_all") as m_sell, \
             patch("bot.day_trader._append_position") as m_append, \
             patch("bot.day_trader._flush_positions") as m_flush, \
             patch("bot.day_trader.datetime") as m_dt:

            # Make datetime.now(ET) return `now` so time checks work
            m_dt.now.return_value = now
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
                "buy": m_buy, "stop": m_stop, "lim": m_lim,
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
        """A small breakout within the configured cap places a limit buy."""
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

    def test_limit_buy_uses_trigger_based_cap(self):
        pos = _watching_pos(trigger=10.0)
        positions, m = self._run([pos], price=10.0)
        args = m["buy"].call_args[0]
        self.assertEqual(args[4], round(10.0 * (1 + ENTRY_LIMIT_OFFSET_PCT / 100), 2))

    def test_unfilled_limit_order_becomes_pending(self):
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
        from bot.parser import Signal, SignalKind
        from datetime import timezone
        sig2 = Signal(kind=SignalKind.PLAN, ticker="TEST", trigger=10.0,
                      received_at=datetime.now(timezone.utc))
        sig2.message_id = "sig-002"  # type: ignore
        positions, m = self._run([pos], price=9.0, new_plans=[sig2])
        # Still only one position
        self.assertEqual(len(positions), 1)


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

    def test_first_milestone_confirm_count_increments(self):
        """First time price hits +3%, confirm_count goes to 1 (not yet upgraded)."""
        fill = 10.0
        pos = _open_pos(fill=fill, stop=fill * 0.98, qty=2.0)
        threshold = fill * (1 + _TRAILING_MILESTONES[0][0] / 100)  # +3%
        positions, m = self._run([pos], price=threshold)
        self.assertEqual(pos.confirm_count, 1)
        self.assertEqual(pos.stop_price, round(fill * 0.98, 4))  # not yet upgraded

    def test_second_confirm_upgrades_stop(self):
        """After CONFIRM_POLLS consecutive polls at +3%, stop is upgraded."""
        fill = 10.0
        lock_in_pct = _TRAILING_MILESTONES[0][1]  # 0.5%
        pos = _open_pos(fill=fill, stop=fill * 0.98, qty=2.0,
                        confirm_count=_CONFIRM_POLLS - 1)
        threshold = fill * (1 + _TRAILING_MILESTONES[0][0] / 100)
        positions, m = self._run([pos], price=threshold + 0.01)
        expected_new_stop = round(fill * (1 + lock_in_pct / 100), 4)
        self.assertAlmostEqual(pos.stop_price, expected_new_stop, places=4)
        self.assertEqual(pos.milestone_idx, 1)
        self.assertEqual(pos.confirm_count, 0)

    def test_dip_resets_confirm_count(self):
        """If price dips below milestone threshold, confirm_count resets."""
        fill = 10.0
        pos = _open_pos(fill=fill, stop=fill * 0.98, qty=2.0, confirm_count=1)
        below_threshold = fill * 1.02  # below +3% milestone
        positions, m = self._run([pos], price=below_threshold)
        self.assertEqual(pos.confirm_count, 0)

    def test_stop_upgrade_cancels_old_broker_stop(self):
        """When trailing stop upgrades, old broker stop order is cancelled."""
        fill = 10.0
        pos = _open_pos(fill=fill, stop=fill * 0.98, qty=2.0,
                        stop_order_id="old-stop",
                        confirm_count=_CONFIRM_POLLS - 1)
        threshold = fill * (1 + _TRAILING_MILESTONES[0][0] / 100)
        positions, m = self._run([pos], price=threshold + 0.01)
        m["cancel"].assert_any_call(unittest.mock.ANY, "acct", "old-stop")

    def test_multiple_milestones_advance_correctly(self):
        """Starting at milestone 1, confirming +6% advances to milestone 2."""
        fill = 10.0
        pos = _open_pos(fill=fill, stop=round(fill * 1.005, 4), qty=2.0,
                        milestone_idx=1, confirm_count=_CONFIRM_POLLS - 1)
        threshold = fill * (1 + _TRAILING_MILESTONES[1][0] / 100)  # +6%
        positions, m = self._run([pos], price=threshold + 0.01)
        self.assertEqual(pos.milestone_idx, 2)


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

    def test_new_plan_added_to_positions(self):
        """A new PLAN signal creates a watching DayPosition."""
        from bot.parser import Signal, SignalKind
        sig = Signal(kind=SignalKind.PLAN, ticker="NEW", trigger=50.0,
                     received_at=datetime.now(timezone.utc))
        sig.message_id = "sig-999"  # type: ignore
        positions = []
        self._run(positions, new_plans=[sig], price=49.0)
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0].ticker, "NEW")
        self.assertEqual(positions[0].status, "watching")

    def test_plan_for_already_watching_ticker_skipped(self):
        """Duplicate plan for same ticker doesn't create a second position."""
        from bot.parser import Signal, SignalKind
        existing = _watching_pos(trigger=50.0)
        existing.ticker = "DUP"
        sig = Signal(kind=SignalKind.PLAN, ticker="DUP", trigger=51.0,
                     received_at=datetime.now(timezone.utc))
        sig.message_id = "sig-dup"  # type: ignore
        positions = [existing]
        self._run(positions, new_plans=[sig], price=49.0)
        self.assertEqual(len(positions), 1)  # still just one

    def test_append_position_called_for_new_plan(self):
        """_append_position is called for each new plan."""
        from bot.parser import Signal, SignalKind
        sig = Signal(kind=SignalKind.PLAN, ticker="APP", trigger=50.0,
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

    def test_buy_failure_leaves_position_watching(self):
        """If buy order fails, position stays watching (not stuck in open)."""
        from bot.robinhood_mcp_client import RobinhoodMCPError
        pos = _watching_pos(trigger=10.0)
        with patch("bot.day_trader._load_new_plans", return_value=[]), \
             patch("bot.day_trader._load_token", return_value="tok"), \
             patch("bot.day_trader._MCPSession", return_value=MagicMock()), \
             patch("bot.day_trader._get_agentic_account", return_value="acct"), \
             patch("bot.day_trader._get_prices", return_value={"TEST": 10.0}), \
             patch("bot.day_trader._place_limit_buy",
                   side_effect=RobinhoodMCPError("order rejected")), \
             patch("bot.day_trader._flush_positions"), \
             patch("bot.day_trader._append_position"), \
             patch("bot.day_trader.datetime") as m_dt:
            m_dt.now.return_value = ET_MARKET_OPEN
            run_once([pos], set())
        self.assertEqual(pos.status, "watching")


class TestProtectedEntryAndPolling(unittest.TestCase):

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
        self.assertEqual(runtime.next_due[far.id], 100.0 + FAR_POLL_INTERVAL_S)

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

    def test_limit_buy_sends_no_market_order(self):
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
            result = _place_limit_buy(session, "acct", "TEST", 20, 10.02)
        kwargs = session.call.call_args_list[0].kwargs
        self.assertEqual(kwargs["type"], "limit")
        self.assertEqual(kwargs["limit_price"], "10.02")
        self.assertEqual(result.fill_price, 10.01)

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
