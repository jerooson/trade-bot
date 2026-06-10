from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from bot.shadow_reviewer import ShadowConfig, build_prompt, validate_proposal


def _config() -> ShadowConfig:
    return ShadowConfig(
        orders_path=Path("orders.jsonl"),
        ledger_path=Path("reviews.jsonl"),
        codex_command="codex",
        budget_per_ticker=20,
        max_age_s=300,
        poll_interval_s=1,
        codex_timeout_s=120,
    )


def _proposal(kind: str, action: str, usd: float) -> dict:
    return {
        "id": "ord_1",
        "decided_at": datetime.now(timezone.utc).isoformat(),
        "signal_kind": kind,
        "ticker": "HOOD",
        "action": action,
        "usd_amount": usd,
        "shares_estimate": 0.05,
        "signal": {
            "side": "LONG",
            "position_fraction": 0.25,
            "delta_size": "1/4",
            "message_id": 123,
        },
        "book_before": {"ticker_position": {"deployed_usd": 20}},
    }


def test_entry_verifies_twenty_dollar_fraction():
    ok, reason, expected = validate_proposal(_proposal("ENTRY", "BUY", 5), _config())
    assert ok
    assert expected == 5
    assert "eligible" in reason


def test_reduce_verifies_twenty_dollar_fraction():
    ok, _, expected = validate_proposal(_proposal("REDUCE", "SELL", 5), _config())
    assert ok
    assert expected == 5


def test_reduce_caps_expected_amount_at_virtual_holding():
    p = _proposal("REDUCE", "SELL", 3)
    p["book_before"]["ticker_position"]["deployed_usd"] = 3
    ok, _, expected = validate_proposal(p, _config())
    assert ok
    assert expected == 3


def test_rejects_add_and_close():
    for kind, action in (("ADD", "BUY"), ("CLOSE", "SELL")):
        ok, reason, _ = validate_proposal(_proposal(kind, action, 5), _config())
        assert not ok
        assert "ENTRY" in reason


def test_rejects_incorrect_proportional_amount():
    ok, reason, expected = validate_proposal(_proposal("ENTRY", "BUY", 10), _config())
    assert not ok
    assert expected == 5
    assert "mismatch" in reason


def test_rejects_stale_proposal():
    p = _proposal("ENTRY", "BUY", 5)
    p["decided_at"] = (datetime.now(timezone.utc) - timedelta(minutes=6)).isoformat()
    ok, reason, _ = validate_proposal(p, _config())
    assert not ok
    assert "stale" in reason


def test_prompt_forbids_placement():
    prompt = build_prompt(_proposal("ENTRY", "BUY", 5), 5)
    assert "Never place or cancel an order" in prompt
    assert "review_equity_order once" in prompt
    assert "Do not call place_equity_order" in prompt
