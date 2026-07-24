from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from bot.shadow_reviewer import (
    REF_ID_NAMESPACE,
    ShadowConfig,
    _extract_broker_order_id,
    _ref_id,
    _warn_stale_pending,
    build_prompt,
    review_one,
    validate_proposal,
)
from bot.robinhood_mcp_client import OrderResult, RobinhoodMCPError


def _config(place_orders: bool = True) -> ShadowConfig:
    return ShadowConfig(
        orders_path=Path("orders.jsonl"),
        ledger_path=Path("reviews.jsonl"),
        codex_command="codex",
        budget_per_ticker=20,
        max_age_s=300,
        poll_interval_s=1,
        codex_timeout_s=120,
        place_orders=place_orders,
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
        "book_before": {"ticker_position": {"deployed_usd": 5}},
    }


# ---------------------------------------------------------------------------
# validate_proposal
# ---------------------------------------------------------------------------

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


def test_add_verifies_increment_not_target_total():
    cases = (
        (1.0, 10.0, 10.0),       # ARM: 1/2 -> full
        (2 / 3, 6.6667, 6.6666), # GOOG: 1/3 -> 2/3
        (3 / 8, 2.5, 5.0),       # DDOG: 1/8 -> 3/8
    )
    for target_fraction, deployed, proposal_usd in cases:
        p = _proposal("ADD", "BUY", proposal_usd)
        p["signal"]["position_fraction"] = target_fraction
        p["book_before"]["ticker_position"]["deployed_usd"] = deployed
        ok, reason, expected = validate_proposal(p, _config())
        assert ok, reason
        assert expected == proposal_usd


def test_add_rejects_target_total_as_increment():
    p = _proposal("ADD", "BUY", 13.3333)
    p["signal"]["position_fraction"] = 2 / 3
    p["book_before"]["ticker_position"]["deployed_usd"] = 6.6667
    ok, reason, expected = validate_proposal(p, _config())
    assert not ok
    assert expected == 6.6666
    assert "mismatch" in reason


def test_close_verifies_proposal_amount():
    ok, reason, expected = validate_proposal(_proposal("CLOSE", "SELL", 5), _config())
    assert ok, reason
    assert expected == 5


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


# ---------------------------------------------------------------------------
# build_prompt — kill switch (place_orders=False)
# ---------------------------------------------------------------------------

def test_review_only_prompt_forbids_placement():
    prompt = build_prompt(_proposal("ENTRY", "BUY", 5), 5, place_orders=False)
    assert "REVIEW ONLY" in prompt
    assert "Do not call place_equity_order" in prompt
    assert "review_equity_order once" in prompt
    # placement should not appear after the explicit prohibition
    after_prohibition = prompt.split("Do not call place_equity_order")[1]
    assert "place_equity_order" not in after_prohibition


def test_review_only_prompt_reduce():
    prompt = build_prompt(_proposal("REDUCE", "SELL", 5), 5, place_orders=False)
    assert "REVIEW ONLY" in prompt
    assert "Do not call place_equity_order" in prompt


# ---------------------------------------------------------------------------
# build_prompt — live placement (place_orders=True)
# ---------------------------------------------------------------------------

def test_live_prompt_has_structured_steps():
    prompt = build_prompt(_proposal("ENTRY", "BUY", 5), 5, place_orders=True)
    assert "place_equity_order" in prompt
    assert "ref_id=" in prompt
    assert "Do not cancel the order" in prompt


def test_live_prompt_uses_exact_tool_bypass_phrases():
    """Robinhood tool description requires exact phrases to skip confirmation."""
    prompt = build_prompt(_proposal("ENTRY", "BUY", 5), 5, place_orders=True)
    assert "skip the review" in prompt
    assert "just place it, don't review" in prompt
    assert "Do not call review_equity_order" in prompt


def test_live_prompt_requires_tagged_output():
    """Codex must emit BROKER_ORDER_ID= on its own line — no free UUID scan."""
    prompt = build_prompt(_proposal("ENTRY", "BUY", 5), 5, place_orders=True)
    assert "BROKER_ORDER_ID=" in prompt
    assert "ORDER_STATE=" in prompt


def test_live_prompt_includes_open_order_check():
    """get_equity_orders must appear so the duplicate-order check can execute."""
    prompt = build_prompt(_proposal("ENTRY", "BUY", 5), 5, place_orders=True)
    assert "get_equity_orders" in prompt


def test_live_prompt_reduce_caps_to_actual_position():
    prompt = build_prompt(_proposal("REDUCE", "SELL", 5), 5, place_orders=True)
    assert "get_equity_positions" in prompt
    assert "cap the sell quantity" in prompt.lower()
    assert "min(" in prompt
    assert "actual_shares_held" in prompt
    assert "BROKER_ORDER_ID=NONE" in prompt  # abort signal when position absent


def test_live_prompt_post_placement_verification():
    prompt = build_prompt(_proposal("ENTRY", "BUY", 5), 5, place_orders=True)
    # Codex must call get_equity_orders AFTER placing to confirm acknowledgement.
    assert "get_equity_orders" in prompt
    after_placement = prompt.split("place_equity_order")[1]
    assert "get_equity_orders" in after_placement


def test_live_prompt_ref_id_is_stable():
    p = _proposal("ENTRY", "BUY", 5)
    assert build_prompt(p, 5, place_orders=True) == build_prompt(p, 5, place_orders=True)
    assert _ref_id(p) in build_prompt(p, 5, place_orders=True)


# ---------------------------------------------------------------------------
# _extract_broker_order_id — tagged format only
# ---------------------------------------------------------------------------

def test_extract_returns_none_for_free_uuid():
    """Any UUID not in the BROKER_ORDER_ID= tag must be ignored."""
    random_uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    assert _extract_broker_order_id(f"session {random_uuid} confirmed") is None


def test_extract_returns_none_for_json_id_field():
    """UUIDs in JSON 'id' fields (session, account, correlation) must be ignored."""
    assert _extract_broker_order_id('{"id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"}') is None


def test_extract_returns_broker_id_from_tag():
    broker_id = "11111111-2222-3333-4444-555555555555"
    text = f"Order submitted.\nBROKER_ORDER_ID={broker_id}\nORDER_STATE=new"
    assert _extract_broker_order_id(text) == broker_id


def test_extract_is_case_insensitive():
    broker_id = "AAAABBBB-CCCC-DDDD-EEEE-FFFFFFFFFFFF"
    text = f"BROKER_ORDER_ID={broker_id}\n"
    result = _extract_broker_order_id(text)
    assert result == broker_id.lower()


def test_extract_returns_none_on_empty_output():
    assert _extract_broker_order_id("") is None


def test_extract_ignores_none_sentinel():
    """BROKER_ORDER_ID=NONE (no-position REDUCE) must not be treated as a UUID."""
    assert _extract_broker_order_id("BROKER_ORDER_ID=NONE\nORDER_STATE=skipped") is None


# ---------------------------------------------------------------------------
# review_one — status logic
# ---------------------------------------------------------------------------

def _make_result(
    returncode: int, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


def test_review_one_skipped_for_ineligible_proposal(tmp_path):
    cfg = ShadowConfig(
        orders_path=tmp_path / "orders.jsonl",
        ledger_path=tmp_path / "ledger.jsonl",
        codex_command="codex",
        budget_per_ticker=20,
        max_age_s=300,
        poll_interval_s=1,
        codex_timeout_s=120,
        place_orders=True,
    )
    record = review_one(_proposal("ADD", "REJECT", 5), cfg, _append_pending=False)
    assert record.status == "SKIPPED"
    assert not cfg.ledger_path.exists()


def test_review_one_placed_when_direct_mcp_returns_order(tmp_path):
    broker_id = "11111111-2222-3333-4444-555555555555"
    p = _proposal("ENTRY", "BUY", 5)
    cfg = ShadowConfig(
        orders_path=tmp_path / "orders.jsonl",
        ledger_path=tmp_path / "ledger.jsonl",
        codex_command="codex",
        budget_per_ticker=20,
        max_age_s=300,
        poll_interval_s=1,
        codex_timeout_s=120,
        place_orders=True,
        pnl_path=tmp_path / "pnl.jsonl",
    )
    result = OrderResult(broker_id, "new", 100.0, 0.05, 5.0)
    with patch(
        "bot.shadow_reviewer.robinhood_mcp_client.place_order",
        return_value=result,
    ):
        record = review_one(p, cfg, _append_pending=False)
    assert record.status == "PLACED"
    assert record.broker_order_id == broker_id


def test_review_one_unverified_when_direct_mcp_errors(tmp_path):
    p = _proposal("ENTRY", "BUY", 5)
    cfg = ShadowConfig(
        orders_path=tmp_path / "orders.jsonl",
        ledger_path=tmp_path / "ledger.jsonl",
        codex_command="codex",
        budget_per_ticker=20,
        max_age_s=300,
        poll_interval_s=1,
        codex_timeout_s=120,
        place_orders=True,
    )
    with patch(
        "bot.shadow_reviewer.robinhood_mcp_client.place_order",
        side_effect=RobinhoodMCPError("test broker error"),
    ):
        record = review_one(p, cfg, _append_pending=False)
    assert record.status == "UNVERIFIED"
    assert record.broker_order_id is None
    assert "test broker error" in record.rationale


def test_review_one_unverified_on_unexpected_direct_mcp_error(tmp_path):
    cfg = ShadowConfig(
        orders_path=tmp_path / "orders.jsonl",
        ledger_path=tmp_path / "ledger.jsonl",
        codex_command="codex",
        budget_per_ticker=20,
        max_age_s=300,
        poll_interval_s=1,
        codex_timeout_s=120,
        place_orders=True,
    )
    with patch(
        "bot.shadow_reviewer.robinhood_mcp_client.place_order",
        side_effect=RuntimeError("unexpected"),
    ):
        record = review_one(_proposal("ENTRY", "BUY", 5), cfg, _append_pending=False)
    assert record.status == "UNVERIFIED"
    assert "unexpected" in record.rationale


def test_review_one_reviewed_status_when_placement_disabled(tmp_path):
    cfg = ShadowConfig(
        orders_path=tmp_path / "orders.jsonl",
        ledger_path=tmp_path / "ledger.jsonl",
        codex_command="codex",
        budget_per_ticker=20,
        max_age_s=300,
        poll_interval_s=1,
        codex_timeout_s=120,
        place_orders=False,
    )
    with patch("bot.shadow_reviewer.invoke_codex", return_value=_make_result(0, stdout="reviewed")):
        record = review_one(_proposal("ENTRY", "BUY", 5), cfg, _append_pending=False)
    assert record.status == "REVIEWED"
    assert record.broker_order_id is None


# ---------------------------------------------------------------------------
# PENDING ledger entry (crash safety)
# ---------------------------------------------------------------------------

def test_review_one_writes_pending_before_direct_mcp(tmp_path):
    broker_id = "aaaabbbb-cccc-dddd-eeee-ffffffffffff"
    p = _proposal("ENTRY", "BUY", 5)
    cfg = ShadowConfig(
        orders_path=tmp_path / "orders.jsonl",
        ledger_path=tmp_path / "ledger.jsonl",
        codex_command="codex",
        budget_per_ticker=20,
        max_age_s=300,
        poll_interval_s=1,
        codex_timeout_s=120,
        place_orders=True,
        pnl_path=tmp_path / "pnl.jsonl",
    )
    result = OrderResult(broker_id, "new", 100.0, 0.05, 5.0)
    with patch(
        "bot.shadow_reviewer.robinhood_mcp_client.place_order",
        return_value=result,
    ):
        review_one(p, cfg, _append_pending=True)

    lines = cfg.ledger_path.read_text().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["status"] == "PENDING"


# ---------------------------------------------------------------------------
# Stale PENDING warning
# ---------------------------------------------------------------------------

def test_warn_stale_pending_emits_warning(tmp_path, caplog):
    ledger = tmp_path / "ledger.jsonl"
    row = {
        "status": "PENDING",
        "dedupe_key": "123:HOOD:ENTRY",
        "ticker": "HOOD",
        "reviewed_at": "2026-01-01T00:00:00+00:00",
    }
    ledger.write_text(json.dumps(row) + "\n")
    import logging
    with caplog.at_level(logging.WARNING, logger="bot.shadow_reviewer"):
        _warn_stale_pending(ledger)
    assert "STALE PENDING" in caplog.text
    assert "HOOD" in caplog.text


def test_warn_stale_pending_silent_when_no_pending(tmp_path, caplog):
    ledger = tmp_path / "ledger.jsonl"
    row = {"status": "PLACED", "dedupe_key": "123:HOOD:ENTRY", "ticker": "HOOD",
           "reviewed_at": "2026-01-01T00:00:00+00:00"}
    ledger.write_text(json.dumps(row) + "\n")
    import logging
    with caplog.at_level(logging.WARNING, logger="bot.shadow_reviewer"):
        _warn_stale_pending(ledger)
    assert "STALE PENDING" not in caplog.text


def test_warn_stale_pending_silent_when_ledger_absent(tmp_path, caplog):
    import logging
    with caplog.at_level(logging.WARNING, logger="bot.shadow_reviewer"):
        _warn_stale_pending(tmp_path / "nonexistent.jsonl")
    assert caplog.text == ""


# ---------------------------------------------------------------------------
# load_config default — placement disabled when env var absent
# ---------------------------------------------------------------------------

def test_load_config_defaults_placement_to_false(monkeypatch):
    monkeypatch.delenv("SHADOW_REVIEW_PLACE_ORDERS", raising=False)
    monkeypatch.setenv("EXECUTOR_ORDERS_PATH", "/tmp/orders.jsonl")
    monkeypatch.setenv("SHADOW_REVIEW_LEDGER_PATH", "/tmp/ledger.jsonl")
    with patch("bot.shadow_reviewer.load_dotenv"):
        from bot.shadow_reviewer import load_config
        cfg = load_config()
    assert cfg.place_orders is False


def test_load_config_placement_enabled_when_explicit(monkeypatch):
    monkeypatch.setenv("SHADOW_REVIEW_PLACE_ORDERS", "true")
    monkeypatch.setenv("EXECUTOR_ORDERS_PATH", "/tmp/orders.jsonl")
    monkeypatch.setenv("SHADOW_REVIEW_LEDGER_PATH", "/tmp/ledger.jsonl")
    with patch("bot.shadow_reviewer.load_dotenv"):
        from bot.shadow_reviewer import load_config
        cfg = load_config()
    assert cfg.place_orders is True
