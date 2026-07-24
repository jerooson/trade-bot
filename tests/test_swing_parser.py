"""Tests for the swing-trade channel parser."""

import json

from bot.swing_parser import ActionKind, Side, parse_swing
from tests import fixtures_swing as f


def test_entry_lite():
    a = parse_swing(f.ENTRY)
    assert a is not None
    assert a.kind is ActionKind.ENTRY
    assert a.ticker == "LITE"
    assert a.side is Side.LONG
    assert a.price == 873.00
    assert a.position_size == "1/2"
    assert a.position_fraction == 0.5
    assert a.stop_loss is None
    assert a.stop_loss_label == "无"
    assert a.stop_type == "立即"
    assert a.posted_by == "Will"
    assert a.is_actionable


def test_add_nvda_with_arrow():
    a = parse_swing(f.ADD)
    assert a is not None
    assert a.kind is ActionKind.ADD
    assert a.ticker == "NVDA"
    assert a.side is Side.LONG
    # Two prices on one line: "$210.80 → 均价: $214.90".
    # We pull the first number from "Price:" -- 210.80 (the add fill).
    assert a.price == 210.80
    # Position size string preserved verbatim, fraction is the right side.
    assert a.position_size == "+1/4 → 3/4"
    assert a.position_fraction == 0.75
    assert a.is_actionable


def test_add_arm_full_position_maps_to_one():
    text = """🚨 正股加仓
股票: ARM
操作: 🔵 买入加仓 (做多)
价格: $287.00 → 均价: $318.50
仓位: +1/2 → full
止损: 无
止损类型: 立即

Posted by: Will
"""
    a = parse_swing(text)
    assert a is not None
    assert a.kind is ActionKind.ADD
    assert a.ticker == "ARM"
    assert a.position_size == "+1/2 → full"
    assert a.position_fraction == 1.0
    assert a.is_actionable


def test_reduce_infq():
    a = parse_swing(f.REDUCE)
    assert a is not None
    assert a.kind is ActionKind.REDUCE
    assert a.ticker == "INFQ"
    assert a.profit_pct == 15.0
    # The delta (amount sold) is embedded in the action text, not a separate
    # field. position_size stays None because the message doesn't tell us the
    # NEW total size, only how much was trimmed.
    assert a.delta_size == "1/8"
    assert a.position_size is None
    assert a.is_actionable


def test_reduce_english_trim_with_fraction():
    """English-style trim message with a fraction: 'Sell to trim 1/4'."""
    text = """🚨 正股减仓
股票: NVDA
操作: 🟠 Sell to trim 1/4
盈亏: +12.30%

Posted by: Will
"""
    a = parse_swing(text)
    assert a is not None
    assert a.kind is ActionKind.REDUCE
    assert a.delta_size == "1/4"
    assert a.profit_pct == 12.3


def test_reduce_no_fraction_in_action():
    """Bare 'Sell to trim' / '卖出减仓' with no fraction stays delta_size=None."""
    text = """🚨 正股减仓
股票: AAPL
操作: 🟠 Sell to trim
盈亏: +5.00%

Posted by: Will
"""
    a = parse_swing(text)
    assert a is not None
    assert a.kind is ActionKind.REDUCE
    assert a.delta_size is None
    assert a.position_size is None


def test_close_simplified_chinese():
    a = parse_swing(f.CLOSE_SIMPLIFIED)
    assert a is not None
    assert a.kind is ActionKind.CLOSE
    assert a.ticker == "MU"
    assert a.profit_pct == 50.0
    assert a.is_actionable


def test_close_traditional_chinese():
    """The 平倉 (Traditional) variant uses English labels."""
    a = parse_swing(f.CLOSE_TRADITIONAL)
    assert a is not None
    assert a.kind is ActionKind.CLOSE
    assert a.ticker == "TQQQ"
    assert a.profit_pct == -16.0


def test_stop_trigger_nvda():
    a = parse_swing(f.STOP_TRIGGER)
    assert a is not None
    assert a.kind is ActionKind.STOP_TRIGGER
    assert a.ticker == "NVDA"
    assert a.side is Side.LONG
    assert a.avg_cost == 218.84
    assert a.stop_loss == 217.5
    # current_price was extracted as price fallback when no "price" field.
    assert a.price == 217.49
    assert a.is_actionable


def test_stop_update_infq_breakeven():
    a = parse_swing(f.STOP_UPDATE)
    assert a is not None
    assert a.kind is ActionKind.STOP_UPDATE
    assert a.ticker == "INFQ"
    # "保本 (均价)" = "breakeven (avg cost)" -- no numeric stop, label preserved.
    assert a.stop_loss is None
    assert a.stop_loss_label == "保本 (均价)"
    # Stop-update is informational, not actionable.
    assert a.is_actionable is False


def test_position_update_docn():
    a = parse_swing(f.POSITION_UPDATE)
    assert a is not None
    assert a.kind is ActionKind.POSITION_UPDATE
    assert a.ticker == "DOCN"
    assert a.side is Side.LONG
    assert a.avg_cost == 149.68
    assert a.price == 176.00  # falls back to current_price
    assert a.position_size == "1/2"
    assert a.position_fraction == 0.5
    assert a.profit_pct == 17.6
    assert a.is_actionable is False


def test_size_update_with_arrow():
    a = parse_swing(f.SIZE_UPDATE_RARE)
    assert a is not None
    assert a.kind is ActionKind.POSITION_UPDATE
    assert a.ticker == "CRWD"
    assert a.position_size == "7/8 → 3/4"
    assert a.position_fraction == 0.75


def test_noise_rejected():
    assert parse_swing(f.NOISE_NO_HEADER) is None
    assert parse_swing(f.NOISE_EMPTY) is None
    assert parse_swing("") is None


def test_googl_maps_to_goog_class_c():
    text = f.ENTRY.replace("LITE", "GOOGL")
    a = parse_swing(text)
    assert a is not None
    assert a.ticker == "GOOG"


def test_to_dict_is_json_safe():
    a = parse_swing(f.ENTRY)
    d = a.to_dict()
    json.dumps(d)
    assert d["kind"] == "ENTRY"
    assert d["side"] == "LONG"
    assert isinstance(d["received_at"], str)
