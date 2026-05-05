"""Parser tests using verbatim message fixtures from the user's signal channel."""

import pytest

from bot.parser import (
    Side,
    Signal,
    SignalKind,
    parse_blob,
    parse_message,
    split_messages,
)
from tests import fixtures as f


# -- PLAN ---------------------------------------------------------------------

def test_plan_bold_extracts_all_fields():
    sig = parse_message(f.PLAN_BOLD)
    assert sig is not None
    assert sig.kind is SignalKind.PLAN
    assert sig.ticker == "SOUN"
    assert sig.side is Side.LONG
    assert sig.trigger == 9.64
    assert sig.target == 11.07
    assert sig.chart_url == "https://www.tradingview.com/x/ZLvRTfN8/"
    assert "\u7A81\u7834" in (sig.setup or "")  # 突破 = "breakout"
    assert sig.is_actionable is False  # PLAN is not actionable, only TRIGGER is


# -- TRIGGER ------------------------------------------------------------------

def test_trigger_bold_axti():
    sig = parse_message(f.TRIGGER_BOLD_AXTI)
    assert sig is not None
    assert sig.kind is SignalKind.TRIGGER
    assert sig.ticker == "AXTI"
    assert sig.side is Side.LONG
    assert sig.trigger == 96.32
    assert sig.current_price == 98.99
    # The "Setup:" line on TRIGGER messages is itself a key:value soup;
    # we keep the raw value for now -- can be parsed further if useful.
    assert sig.target is None  # not at top level on this format
    assert sig.is_actionable is True


def test_trigger_bold_soun_has_target_in_setup_line():
    sig = parse_message(f.TRIGGER_BOLD_SOUN)
    assert sig is not None
    assert sig.kind is SignalKind.TRIGGER
    assert sig.ticker == "SOUN"
    assert sig.side is Side.LONG
    assert sig.trigger == 9.64
    assert sig.current_price == 9.71
    assert sig.is_actionable is True


def test_trigger_plain_lac():
    sig = parse_message(f.TRIGGER_PLAIN_LAC)
    assert sig is not None
    assert sig.kind is SignalKind.TRIGGER
    assert sig.ticker == "LAC"
    assert sig.side is Side.LONG
    assert sig.trigger == 5.77
    assert sig.current_price == 5.83
    assert sig.is_actionable is True


def test_trigger_plain_lwlg_no_target():
    sig = parse_message(f.TRIGGER_PLAIN_LWLG_NO_TARGET)
    assert sig is not None
    assert sig.kind is SignalKind.TRIGGER
    assert sig.ticker == "LWLG"
    assert sig.trigger == 17.28
    assert sig.current_price == 17.44
    # "Target: None" must produce None, not 0.0
    assert sig.target is None
    assert sig.is_actionable is True


# -- PROFIT -------------------------------------------------------------------

def test_profit_plain_axti():
    sig = parse_message(f.PROFIT_PLAIN_AXTI)
    assert sig is not None
    assert sig.kind is SignalKind.PROFIT
    assert sig.ticker == "AXTI"
    assert sig.side is Side.LONG
    assert sig.trigger == 96.32
    assert sig.current_price == 99.82
    assert sig.profit_pct == 3.6
    assert sig.is_actionable is False  # PROFIT is informational


# -- Noise / non-signal -------------------------------------------------------

@pytest.mark.parametrize(
    "text",
    [f.EMPTY, f.NOISE_PLAIN_CHAT, f.NOISE_BOT_NAME_LINE, "   \n  \n"],
)
def test_non_signals_return_none(text: str):
    assert parse_message(text) is None


# -- Multi-post splitting -----------------------------------------------------

def test_split_messages_finds_all_four_posts():
    parts = split_messages(f.MIXED_BLOB)
    assert len(parts) == 4


def test_parse_blob_returns_all_signals_in_order():
    sigs: list[Signal] = parse_blob(f.MIXED_BLOB)
    assert [s.ticker for s in sigs] == ["LAC", "SOUN", "LWLG", "AXTI"]
    assert [s.kind for s in sigs] == [
        SignalKind.TRIGGER,
        SignalKind.TRIGGER,
        SignalKind.TRIGGER,
        SignalKind.PROFIT,
    ]


# -- Serialization round-trip ------------------------------------------------

def test_signal_to_dict_is_json_safe():
    import json

    sig = parse_message(f.TRIGGER_PLAIN_LAC)
    assert sig is not None
    payload = json.dumps(sig.to_dict())
    restored = json.loads(payload)
    assert restored["ticker"] == "LAC"
    assert restored["kind"] == "TRIGGER"
    assert restored["side"] == "LONG"
    assert restored["trigger"] == 5.77
