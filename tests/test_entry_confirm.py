"""DAY_TRADE_ENTRY_CONFIRM_S: hold the cross for N seconds before entering."""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from bot import day_trader
from bot.day_trader import DayPosition, run_once
from bot.robinhood_mcp_client import OrderResult

ET = ZoneInfo("America/New_York")
T0 = datetime(2026, 9, 14, 10, 0, 0, tzinfo=ET)


def _watch() -> DayPosition:
    return DayPosition(ticker="TEST", status="watching", trigger_price=100.0, armed=True,
                       plan_received_at=T0.isoformat(), plan_signal_id="sig-1")


def _tick(pos: DayPosition, now: datetime, price: float) -> MagicMock:
    fill = OrderResult("buy-1", "filled", price, 2.0, price * 2)
    with patch.object(day_trader, "_load_new_plans", return_value=[]), \
         patch.object(day_trader, "load_plans", return_value=[]), \
         patch.object(day_trader, "_connect_broker", return_value=(MagicMock(), "acct")), \
         patch.object(day_trader, "_get_prices", return_value={"TEST": price}), \
         patch.object(day_trader, "_validate_entry_preflight", return_value=(price, price, price, 0.0)), \
         patch.object(day_trader, "_place_fractional_market_buy", return_value=fill) as buy, \
         patch.object(day_trader, "_place_stop_order", return_value=None), \
         patch.object(day_trader, "_place_limit_sell", return_value=None), \
         patch.object(day_trader, "_append_position"), patch.object(day_trader, "_flush_positions"), \
         patch.object(day_trader, "datetime") as m_dt:
        m_dt.now.return_value = now
        m_dt.fromisoformat.side_effect = datetime.fromisoformat
        run_once([pos], set())
    return buy


def test_default_zero_keeps_immediate_entry():
    pos = _watch()
    with patch.object(day_trader, "ENTRY_CONFIRM_S", 0):
        buy = _tick(pos, T0, 100.05)
    buy.assert_called_once()
    assert pos.status == "open" and pos.trigger_confirm_started_at is None


def test_entry_waits_for_the_cross_to_hold():
    pos = _watch()
    with patch.object(day_trader, "ENTRY_CONFIRM_S", 60):
        first = _tick(pos, T0, 100.05)
        assert pos.status == "watching" and pos.trigger_confirm_started_at == T0.isoformat()
        first.assert_not_called()
        second = _tick(pos, T0 + timedelta(seconds=30), 100.08)
        second.assert_not_called()
        third = _tick(pos, T0 + timedelta(seconds=65), 100.04)
    third.assert_called_once()
    assert pos.status == "open" and pos.trigger_confirm_started_at is None


def test_wick_back_below_the_level_resets_confirmation():
    pos = _watch()
    with patch.object(day_trader, "ENTRY_CONFIRM_S", 60):
        _tick(pos, T0, 100.05)
        _tick(pos, T0 + timedelta(seconds=20), 99.97)
        assert pos.trigger_confirm_started_at is None and pos.status == "watching"
        _tick(pos, T0 + timedelta(seconds=40), 100.02)
        assert pos.trigger_confirm_started_at == (T0 + timedelta(seconds=40)).isoformat()
        buy = _tick(pos, T0 + timedelta(seconds=90), 100.03)
        buy.assert_not_called()          # only 50 s held since the restart
        buy = _tick(pos, T0 + timedelta(seconds=101), 100.03)
    buy.assert_called_once()


def test_gap_past_cap_during_confirmation_clears_it():
    pos = _watch()
    with patch.object(day_trader, "ENTRY_CONFIRM_S", 60):
        _tick(pos, T0, 100.01)
        buy = _tick(pos, T0 + timedelta(seconds=10), 100.5)   # beyond the +0.2% guard
    buy.assert_not_called()
    assert pos.status == "expired" and pos.exit_reason == "entry_gap_above_limit"
    assert pos.trigger_confirm_started_at is None
