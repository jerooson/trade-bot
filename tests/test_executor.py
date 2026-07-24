"""Unit tests for the DRY_RUN executor's decision engine."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from bot.executor import (
    ExecutorConfig,
    VirtualBook,
    decide,
    _fraction_of,
    reconcile_book_from_reviews,
    replay_history,
)
from bot.shadow_reviewer import validate_proposal, ShadowConfig
from bot.swing_parser import parse_swing


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------

@pytest.fixture
def config() -> ExecutorConfig:
    return ExecutorConfig(
        budget_per_ticker=20.0,
        max_open_tickers=5,
        mode="DRY_RUN",
        book_path=Path("/tmp/test_book.json"),
        orders_path=Path("/tmp/test_orders.jsonl"),
        poll_interval_s=1.0,
        swing_live_path=Path("/tmp/swings.jsonl"),
        swing_history_path=Path("/tmp/swings_history.jsonl"),
        notify_after=datetime.now(timezone.utc),
        replay_from=None,
    )


@pytest.fixture
def book(config: ExecutorConfig) -> VirtualBook:
    return VirtualBook(
        mode=config.mode,
        budget_per_ticker=config.budget_per_ticker,
        max_open_tickers=config.max_open_tickers,
        started_at=datetime.now(timezone.utc).isoformat(),
    )


def _entry(
    ticker: str,
    *,
    price: float | None = 100.0,
    fraction: float | None = 0.5,
    size: str | None = "1/2",
    side: str = "LONG",
    stop: float | None = None,
) -> dict:
    return {
        "kind": "ENTRY",
        "ticker": ticker,
        "side": side,
        "price": price,
        "position_size": size,
        "position_fraction": fraction,
        "stop_loss": stop,
        "stop_loss_label": f"${stop:.2f}" if stop else "无",
        "received_at": "2026-06-08T15:00:00+00:00",
    }


def _add(ticker: str, *, fraction: float, size: str, price: float = 100.0) -> dict:
    return {
        "kind": "ADD",
        "ticker": ticker,
        "side": "LONG",
        "price": price,
        "position_size": size,
        "position_fraction": fraction,
        "received_at": "2026-06-08T15:30:00+00:00",
    }


def _reduce(ticker: str, *, delta: str, price: float = 100.0) -> dict:
    return {
        "kind": "REDUCE",
        "ticker": ticker,
        "side": "LONG",
        "price": price,
        "position_size": None,
        "position_fraction": None,
        "delta_size": delta,
        "received_at": "2026-06-08T16:00:00+00:00",
    }


def _close(ticker: str, *, kind: str = "CLOSE", price: float | None = 110.0) -> dict:
    return {
        "kind": kind,
        "ticker": ticker,
        "side": "LONG",
        "price": price,
        "received_at": "2026-06-08T17:00:00+00:00",
    }


# ---------------------------------------------------------------------------
# Helper.
# ---------------------------------------------------------------------------

def test_fraction_of_parses_simple_fractions():
    assert _fraction_of("1/8") == pytest.approx(0.125)
    assert _fraction_of("1/2") == pytest.approx(0.5)
    assert _fraction_of("3/4") == pytest.approx(0.75)


def test_fraction_of_rejects_garbage():
    assert _fraction_of(None) is None
    assert _fraction_of("") is None
    assert _fraction_of("hello") is None
    assert _fraction_of("1/0") is None
    assert _fraction_of("1") is None


# ---------------------------------------------------------------------------
# ENTRY tests.
# ---------------------------------------------------------------------------

def test_entry_sizes_to_fraction_of_budget(book, config):
    """ENTRY 1/3 at $100 should buy $20 × 1/3 ≈ $6.67 worth (= 0.0667 sh)."""
    d = decide(_entry("HOOD", price=100.0, fraction=1/3, size="1/3"), book, config)
    assert d.action == "BUY"
    assert d.ticker == "HOOD"
    assert d.usd_amount == pytest.approx(20.0 / 3, rel=1e-3)
    assert d.shares_estimate == pytest.approx(20.0 / 3 / 100.0, rel=1e-3)
    assert "HOOD" in book.positions


def test_entry_full_size_uses_full_budget(book, config):
    d = decide(_entry("AAPL", price=200.0, fraction=1.0, size="1/1"), book, config)
    assert d.action == "BUY"
    assert d.usd_amount == pytest.approx(20.0)
    assert d.shares_estimate == pytest.approx(0.1)


def test_entry_rejects_short(book, config):
    d = decide(_entry("XYZ", side="SHORT"), book, config)
    assert d.action == "REJECT"
    assert "SHORT" in d.rationale


def test_entry_rejects_when_already_held(book, config):
    decide(_entry("HOOD", price=100.0), book, config)
    d = decide(_entry("HOOD", price=110.0), book, config)
    assert d.action == "REJECT"
    assert "already holding" in d.rationale


def test_entry_rejects_when_max_tickers_reached(book, config):
    for t in ("AAA", "BBB", "CCC", "DDD", "EEE"):
        decide(_entry(t, price=100.0), book, config)
    assert book.open_count == 5
    d = decide(_entry("FFF", price=100.0), book, config)
    assert d.action == "REJECT"
    assert "max 5" in d.rationale


def test_entry_rejects_with_no_price(book, config):
    d = decide(_entry("HOOD", price=None), book, config)
    assert d.action == "REJECT"
    assert "price" in d.rationale.lower()


# ---------------------------------------------------------------------------
# ADD tests.
# ---------------------------------------------------------------------------

def test_add_at_quarter_buys_five_dollars(book, config):
    """The headline rule: 'will says +1/4 → we buy $5'."""
    decide(_entry("HOOD", price=100.0, fraction=0.5, size="1/2"), book, config)
    # After ENTRY 1/2 ($10 deployed). ADD with new-total 3/4 → delta = 1/4 → $5.
    d = decide(_add("HOOD", fraction=0.75, size="+1/4 → 3/4", price=100.0), book, config)
    assert d.action == "BUY"
    assert d.usd_amount == pytest.approx(5.0)
    pos = book.positions["HOOD"]
    assert pos.deployed_usd == pytest.approx(15.0)


def test_add_arrow_format_uses_delta_not_total(book, config):
    """IREN bug: '+1/8 → 1/2' with 3/8 already held should buy 1/8 ($2.50), not 1/2 ($10)."""
    # Start at 1/4 ($5)
    decide(_entry("IREN", price=47.0, fraction=0.25, size="1/4"), book, config)
    # ADD +1/8 → 3/8: new total 3/8, already have 1/4 → delta = 1/8 → $2.50
    d = decide(_add("IREN", fraction=0.375, size="+1/8 → 3/8", price=49.0), book, config)
    assert d.action == "BUY"
    assert d.usd_amount == pytest.approx(2.5, abs=0.01)
    # Now at 3/8. ADD +1/8 → 1/2: delta = 1/8 → $2.50
    d2 = decide(_add("IREN", fraction=0.5, size="+1/8 → 1/2", price=41.0), book, config)
    assert d2.action == "BUY"
    assert d2.usd_amount == pytest.approx(2.5, abs=0.01)
    pos = book.positions["IREN"]
    assert pos.deployed_usd == pytest.approx(10.0, abs=0.01)


def test_add_caps_at_remaining_budget(book, config):
    decide(_entry("HOOD", price=100.0, fraction=0.75, size="3/4"), book, config)
    # Deployed $15 of $20; ADD new-total 1/1 wants $5 of delta → fits exactly.
    d = decide(_add("HOOD", fraction=1.0, size="+1/4 → 1/1"), book, config)
    assert d.action == "BUY"
    assert d.usd_amount == pytest.approx(5.0)


def test_add_rejected_when_at_cap(book, config):
    decide(_entry("HOOD", price=100.0, fraction=1.0, size="1/1"), book, config)
    # Already at 1/1. ADD with new-total 1/1 → delta=0 → reject.
    d = decide(_add("HOOD", fraction=1.0, size="+1/4 → 1/1"), book, config)
    assert d.action == "REJECT"


def test_add_rejected_when_not_held(book, config):
    d = decide(_add("HOOD", fraction=0.25, size="+1/4"), book, config)
    assert d.action == "REJECT"
    assert "don't hold" in d.rationale


def test_add_updates_avg_price(book, config):
    decide(_entry("HOOD", price=100.0, fraction=0.5, size="1/2"), book, config)
    # ENTRY: $10 at $100/sh = 0.1 sh, avg = $100
    # ADD new-total 3/4: delta = 1/4 → $5 at $120/sh ≈ 0.0417 sh
    decide(_add("HOOD", fraction=0.75, size="+1/4 → 3/4", price=120.0), book, config)
    # New avg ≈ ($100 × 0.1 + $120 × 0.0417) / 0.1417 ≈ $105.88
    pos = book.positions["HOOD"]
    assert pos.avg_price == pytest.approx(105.88, abs=0.05)


@pytest.mark.parametrize(
    ("raw", "initial_fraction", "expected_ticker", "expected_usd"),
    [
        (
            """🚨 正股加仓
股票: ARM
操作: 🔵 买入加仓 (做多)
价格: $287.00 → 均价: $318.50
仓位: +1/2 → full
止损: 无
止损类型: 立即
Posted by: Will
""",
            0.5,
            "ARM",
            10.0,
        ),
        (
            """🚨 正股加仓
股票: GOOGL
操作: 🔵 买入加仓 (做多)
价格: $319.00 → 均价: $333.50
仓位: +1/3 → 2/3
止损: 无
止损类型: 立即
Posted by: Will
""",
            1 / 3,
            "GOOG",
            6.6666,
        ),
        (
            """🚨 正股加仓
股票: DDOG
操作: 🔵 买入加仓 (做多)
价格: $245.48 → 均价: $238.15
仓位: +1/4 → 3/8
止损: $223.50
止损类型: 立即
Posted by: Will
""",
            1 / 8,
            "DDOG",
            5.0,
        ),
    ],
)
def test_real_add_messages_pass_parser_executor_and_shadow_validation(
    raw, initial_fraction, expected_ticker, expected_usd, book, config
):
    parsed = parse_swing(raw)
    assert parsed is not None
    decide(
        _entry(
            expected_ticker,
            price=parsed.price,
            fraction=initial_fraction,
            size=str(initial_fraction),
        ),
        book,
        config,
    )

    decision = decide(parsed.to_dict(), book, config)

    assert decision.action == "BUY"
    assert decision.ticker == expected_ticker
    assert decision.usd_amount == pytest.approx(expected_usd, abs=0.0001)
    ok, reason, validated_usd = validate_proposal(
        decision.to_dict(),
        ShadowConfig(
            orders_path=Path("orders.jsonl"),
            ledger_path=Path("reviews.jsonl"),
            codex_command="codex",
            budget_per_ticker=20,
            max_age_s=300,
            poll_interval_s=1,
            codex_timeout_s=120,
            place_orders=True,
        ),
    )
    assert ok, reason
    assert validated_usd == pytest.approx(expected_usd, abs=0.0001)


# ---------------------------------------------------------------------------
# REDUCE tests.
# ---------------------------------------------------------------------------

def test_reduce_sells_fraction_of_budget(book, config):
    decide(_entry("HOOD", price=100.0, fraction=1.0, size="1/1"), book, config)
    # Deployed $20; REDUCE 1/4 → sell $5.
    d = decide(_reduce("HOOD", delta="1/4"), book, config)
    assert d.action == "SELL"
    assert d.usd_amount == pytest.approx(5.0)
    pos = book.positions["HOOD"]
    assert pos.deployed_usd == pytest.approx(15.0)


def test_reduce_caps_at_holdings(book, config):
    decide(_entry("HOOD", price=100.0, fraction=0.25, size="1/4"), book, config)
    # Deployed $5; REDUCE 1/2 wants $10 but only $5 to sell → cap, then closes.
    d = decide(_reduce("HOOD", delta="1/2"), book, config)
    assert d.action == "SELL"
    assert d.usd_amount == pytest.approx(5.0)
    # Selling everything closes the position.
    assert "HOOD" not in book.positions


def test_reduce_rejected_when_not_held(book, config):
    d = decide(_reduce("HOOD", delta="1/4"), book, config)
    assert d.action == "REJECT"
    assert "don't hold" in d.rationale


# ---------------------------------------------------------------------------
# CLOSE / STOP_TRIGGER tests.
# ---------------------------------------------------------------------------

def test_close_exits_full_position(book, config):
    decide(_entry("HOOD", price=100.0, fraction=0.5, size="1/2"), book, config)
    d = decide(_close("HOOD"), book, config)
    assert d.action == "SELL"
    assert d.usd_amount == pytest.approx(10.0)
    assert "HOOD" not in book.positions


def test_stop_trigger_exits_full_position(book, config):
    decide(_entry("HOOD", price=100.0, fraction=1.0, size="1/1"), book, config)
    d = decide(_close("HOOD", kind="STOP_TRIGGER", price=95.0), book, config)
    assert d.action == "SELL"
    assert d.usd_amount == pytest.approx(20.0)
    assert "HOOD" not in book.positions


def test_close_rejected_when_not_held(book, config):
    d = decide(_close("HOOD"), book, config)
    assert d.action == "REJECT"
    assert "don't hold" in d.rationale


def test_close_works_even_without_signal_price(book, config):
    decide(_entry("HOOD", price=100.0, fraction=0.5, size="1/2"), book, config)
    d = decide(_close("HOOD", price=None), book, config)
    assert d.action == "SELL"
    assert d.usd_amount == pytest.approx(10.0)
    assert "HOOD" not in book.positions


# ---------------------------------------------------------------------------
# Sequencing.
# ---------------------------------------------------------------------------

def test_full_lifecycle(book, config):
    """ENTRY 1/3 → ADD 1/4 → REDUCE 1/8 → CLOSE — leaves an empty book."""
    decide(_entry("HOOD", price=100.0, fraction=1/3, size="1/3"), book, config)
    decide(_add("HOOD", fraction=0.25, size="+1/4", price=105.0), book, config)
    decide(_reduce("HOOD", delta="1/8", price=110.0), book, config)
    decide(_close("HOOD", price=120.0), book, config)
    assert book.open_count == 0
    assert book.positions == {}


def test_book_summary_reflects_decisions(book, config):
    decide(_entry("HOOD", price=100.0, fraction=0.5, size="1/2"), book, config)
    decide(_entry("AAPL", price=200.0, fraction=1.0, size="1/1"), book, config)
    snap = book.snapshot()
    assert snap["summary"]["open_tickers"] == 2
    assert snap["summary"]["total_deployed_usd"] == pytest.approx(30.0)
    assert snap["summary"]["available_usd"] == pytest.approx(70.0)


def test_skipped_real_add_is_removed_during_replay_and_live_reconciliation(
    tmp_path, book, config
):
    entry = _entry("DDOG", price=223.5, fraction=1 / 8, size="1/8")
    entry["discord"] = {"message_id": 1}
    add = _add("DDOG", fraction=3 / 8, size="+1/4 → 3/8", price=245.48)
    add["discord"] = {"message_id": 2}

    swings = tmp_path / "swings.jsonl"
    ledger = tmp_path / "reviews.jsonl"
    swings.write_text(
        "\n".join(json.dumps(row) for row in (entry, add)) + "\n",
        encoding="utf-8",
    )
    ledger.write_text(
        json.dumps(
            {
                "dedupe_key": "2:DDOG:ADD",
                "status": "SKIPPED",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    cfg = replace(
        config,
        swing_live_path=swings,
        swing_history_path=tmp_path / "history.jsonl",
        review_ledger_path=ledger,
    )

    decide(entry, book, cfg)
    decide(add, book, cfg)
    assert book.positions["DDOG"].deployed_usd == pytest.approx(7.5)

    changed = reconcile_book_from_reviews(cfg, book)

    assert changed
    assert book.positions["DDOG"].deployed_usd == pytest.approx(2.5)

    restarted = VirtualBook(
        mode=cfg.mode,
        budget_per_ticker=cfg.budget_per_ticker,
        max_open_tickers=cfg.max_open_tickers,
    )
    replay_history(cfg, restarted)
    assert restarted.positions["DDOG"].deployed_usd == pytest.approx(2.5)


def test_unverified_broker_outcome_remains_provisionally_applied(tmp_path, config):
    entry = _entry("DDOG", price=223.5, fraction=1 / 8, size="1/8")
    entry["discord"] = {"message_id": 1}
    add = _add("DDOG", fraction=3 / 8, size="+1/4 → 3/8", price=245.48)
    add["discord"] = {"message_id": 2}
    swings = tmp_path / "swings.jsonl"
    ledger = tmp_path / "reviews.jsonl"
    swings.write_text(
        "\n".join(json.dumps(row) for row in (entry, add)) + "\n",
        encoding="utf-8",
    )
    ledger.write_text(
        json.dumps({"dedupe_key": "2:DDOG:ADD", "status": "UNVERIFIED"}) + "\n",
        encoding="utf-8",
    )
    cfg = replace(
        config,
        swing_live_path=swings,
        swing_history_path=tmp_path / "history.jsonl",
        review_ledger_path=ledger,
    )
    restarted = VirtualBook(
        mode=cfg.mode,
        budget_per_ticker=cfg.budget_per_ticker,
        max_open_tickers=cfg.max_open_tickers,
    )

    replay_history(cfg, restarted)

    assert restarted.positions["DDOG"].deployed_usd == pytest.approx(7.5)


def test_replay_then_live_doesnt_double_count(book, config):
    """An entry applied once shouldn't double-deploy if decide() is called once."""
    decide(_entry("HOOD", price=100.0, fraction=0.5, size="1/2"), book, config)
    assert book.positions["HOOD"].deployed_usd == pytest.approx(10.0)
    # A second decide() for a *different* action sequence shouldn't touch
    # HOOD's deployed_usd — confirms idempotence of the decision engine.
    decide(_entry("AAPL", price=200.0), book, config)
    assert book.positions["HOOD"].deployed_usd == pytest.approx(10.0)
