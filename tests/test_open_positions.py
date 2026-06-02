"""Tests for the open-positions folding logic in server.api."""

from server.api import (
    _derive_open_positions,
    _size_to_fraction,
    _format_fraction,
)


def _action(
    ts: str,
    kind: str,
    ticker: str,
    *,
    side: str | None = "LONG",
    price: float | None = None,
    avg_cost: float | None = None,
    position_size: str | None = None,
    position_fraction: float | None = None,
    delta_size: str | None = None,
    stop_loss: float | None = None,
    stop_loss_label: str | None = None,
    profit_pct: float | None = None,
) -> dict:
    return {
        "kind": kind,
        "ticker": ticker,
        "side": side,
        "price": price,
        "avg_cost": avg_cost,
        "position_size": position_size,
        "position_fraction": position_fraction,
        "delta_size": delta_size,
        "stop_loss": stop_loss,
        "stop_loss_label": stop_loss_label,
        "profit_pct": profit_pct,
        "discord": {"created_at": ts},
    }


# -- Fraction helper ---------------------------------------------------------

def test_size_to_fraction_simple():
    assert _size_to_fraction("1/3") == _size_to_fraction("1/3")
    assert _format_fraction(_size_to_fraction("1/3")) == "1/3"


def test_size_to_fraction_arrow_takes_right_side():
    """ADD uses '+1/4 → 3/4' (delta arrow new-total). We want the new total."""
    assert _format_fraction(_size_to_fraction("+1/4 → 3/4")) == "3/4"
    assert _format_fraction(_size_to_fraction("+1/4 -> 3/4")) == "3/4"
    assert _format_fraction(_size_to_fraction("7/8 -> 3/4")) == "3/4"


def test_size_to_fraction_garbage_returns_none():
    assert _size_to_fraction(None) is None
    assert _size_to_fraction("") is None
    assert _size_to_fraction("hello") is None
    assert _size_to_fraction("1/0") is None


# -- Folding regression: the INFQ case ---------------------------------------

def test_reduce_subtracts_from_running_size():
    """ENTRY 1/3, then REDUCE 1/8 -> running size should be 5/24, not 1/3."""
    actions = [
        _action(
            "2026-05-01T12:00:00",
            "ENTRY",
            "INFQ",
            price=16.01,
            position_size="1/3",
            position_fraction=1 / 3,
        ),
        _action(
            "2026-05-02T12:00:00",
            "REDUCE",
            "INFQ",
            delta_size="1/8",
            profit_pct=15.0,
        ),
    ]
    positions = _derive_open_positions(actions)
    assert len(positions) == 1
    p = positions[0]
    assert p["ticker"] == "INFQ"
    # 1/3 - 1/8 = 8/24 - 3/24 = 5/24
    assert p["position_size"] == "5/24"
    assert abs(p["position_fraction"] - 5 / 24) < 1e-9
    assert p["last_action_kind"] == "REDUCE"
    assert p["last_pnl_pct"] == 15.0


def test_reduce_without_delta_keeps_size():
    """If REDUCE has no delta_size (bare 'Sell to trim'), keep the prior size."""
    actions = [
        _action("2026-05-01T12:00:00", "ENTRY", "AAPL",
                price=200.0, position_size="1/2", position_fraction=0.5),
        _action("2026-05-02T12:00:00", "REDUCE", "AAPL",
                delta_size=None, profit_pct=5.0),
    ]
    positions = _derive_open_positions(actions)
    assert len(positions) == 1
    assert positions[0]["position_size"] == "1/2"
    assert positions[0]["last_action_kind"] == "REDUCE"


def test_reduce_to_zero_or_below_closes_position():
    """If a trim crosses zero, the position is effectively closed."""
    actions = [
        _action("2026-05-01T12:00:00", "ENTRY", "TSLA",
                price=400.0, position_size="1/8", position_fraction=0.125),
        _action("2026-05-02T12:00:00", "REDUCE", "TSLA", delta_size="1/4"),
    ]
    positions = _derive_open_positions(actions)
    assert positions == []


def test_add_then_reduce_uses_post_add_total():
    """ENTRY 1/4, ADD '+1/4 → 1/2', REDUCE 1/8 -> running 3/8."""
    actions = [
        _action("2026-05-01T12:00:00", "ENTRY", "NVDA",
                price=210.0, position_size="1/4", position_fraction=0.25),
        _action("2026-05-02T12:00:00", "ADD", "NVDA",
                price=215.0, avg_cost=212.5,
                position_size="+1/4 → 1/2", position_fraction=0.5),
        _action("2026-05-03T12:00:00", "REDUCE", "NVDA",
                delta_size="1/8", profit_pct=2.0),
    ]
    positions = _derive_open_positions(actions)
    assert len(positions) == 1
    p = positions[0]
    # 1/2 - 1/8 = 4/8 - 1/8 = 3/8
    assert p["position_size"] == "3/8"
    assert abs(p["position_fraction"] - 3 / 8) < 1e-9


def test_close_drops_position():
    actions = [
        _action("2026-05-01T12:00:00", "ENTRY", "MU",
                price=100.0, position_size="1/2", position_fraction=0.5),
        _action("2026-05-02T12:00:00", "CLOSE", "MU", profit_pct=50.0),
    ]
    assert _derive_open_positions(actions) == []


def test_stop_trigger_drops_position():
    actions = [
        _action("2026-05-01T12:00:00", "ENTRY", "SOFI",
                price=29.62, position_size="1/3", position_fraction=1 / 3),
        _action("2026-05-02T12:00:00", "STOP_TRIGGER", "SOFI",
                price=28.45, stop_loss=28.5),
    ]
    assert _derive_open_positions(actions) == []


def test_stop_update_keeps_position_updates_stop():
    actions = [
        _action("2026-05-01T12:00:00", "ENTRY", "INFQ",
                price=16.01, position_size="1/3", position_fraction=1 / 3,
                stop_loss=15.0, stop_loss_label="$15.00"),
        _action("2026-05-02T12:00:00", "STOP_UPDATE", "INFQ",
                stop_loss=None, stop_loss_label="保本 (均价)"),
    ]
    positions = _derive_open_positions(actions)
    assert len(positions) == 1
    assert positions[0]["stop_loss_label"] == "保本 (均价)"
    assert positions[0]["last_action_kind"] == "STOP_UPDATE"


def test_chronological_ordering_independent_of_input_order():
    """The fold should sort by timestamp, not rely on input order."""
    a_entry = _action("2026-05-01T12:00:00", "ENTRY", "X",
                      price=10.0, position_size="1/3", position_fraction=1 / 3)
    a_reduce = _action("2026-05-02T12:00:00", "REDUCE", "X", delta_size="1/8")
    # Pass in REVERSE chronological order (newest first, like the API gives us).
    positions = _derive_open_positions([a_reduce, a_entry])
    assert len(positions) == 1
    assert positions[0]["position_size"] == "5/24"
