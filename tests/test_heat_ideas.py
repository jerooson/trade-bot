from __future__ import annotations

import json
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from bot import day_trader
from bot.day_trader import DayPosition, ET, _sync_heat_ideas
from bot.heat_ideas import materialize_heat_ideas, parse_heat_idea
from server import api


@pytest.fixture
def workspace_tmp():
    path = Path.cwd() / ".test-artifacts" / str(uuid.uuid4())
    path.mkdir(parents=True)
    try:
        yield path
    finally:
        shutil.rmtree(path)
        try:
            path.parent.rmdir()
        except OSError:
            pass


def _write_jsonl(path: Path, rows: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def test_heat_parser_routes_chart_watch_to_review():
    idea = parse_heat_idea(
        "关注GOOGL，有可能要突破了",
        idea_id="1",
        created_at="2026-07-14T17:29:00+00:00",
    )
    assert idea is not None
    assert idea["ticker"] == "GOOGL"
    assert idea["trigger_price"] is None
    assert idea["auto_eligible"] is False
    assert idea["classification"] == "needs_level"


def test_heat_parser_normalizes_mixed_case_ticker_without_matching_word_fragment():
    msft = parse_heat_idea(
        "Msft关注能否站稳 fib 0.5",
        idea_id="msft-1",
        created_at="2026-07-16T17:14:03+00:00",
    )
    false_positive = parse_heat_idea(
        "不是，是今天才突破黄线的，一触发Alter，就可以买入",
        idea_id="alter-1",
        created_at="2026-07-16T16:56:07+00:00",
    )
    assert msft is not None
    assert msft["ticker"] == "MSFT"
    assert msft["classification"] == "needs_level"
    assert false_positive is None


def test_heat_parser_requires_cash_tag_for_single_letter_ticker():
    assert parse_heat_idea(
        "M关注突破",
        idea_id="bare-m",
        created_at="2026-07-16T17:14:03+00:00",
    ) is None
    tagged = parse_heat_idea(
        "$M关注突破",
        idea_id="tagged-m",
        created_at="2026-07-16T17:14:03+00:00",
    )
    assert tagged is not None and tagged["ticker"] == "M"


def test_heat_parser_classifies_swing_context_and_position_updates():
    ibm = parse_heat_idea(
        "喜欢抄底操作的可以关注IBM，fib 1支撑反弹，有可能会去回补缺口",
        idea_id="ibm-1",
        created_at="2026-07-16T17:25:18+00:00",
    )
    iwm = parse_heat_idea(
        "IWM站上YDH，所以并不是全面下跌的市场，只是资金轮换",
        idea_id="iwm-1",
        created_at="2026-07-16T14:11:47+00:00",
    )
    muu = parse_heat_idea(
        "说一下我买入MUU的交易策略，买早了，仓位很轻，今天收不回去就应该止损",
        idea_id="muu-1",
        created_at="2026-07-16T13:11:24+00:00",
    )
    assert ibm is not None and ibm["classification"] == "swing_dca"
    assert iwm is not None and iwm["classification"] == "market_context"
    assert muu is not None and muu["classification"] == "position_update"
    assert not ibm["auto_eligible"]
    assert not iwm["auto_eligible"]
    assert not muu["auto_eligible"]


def test_heat_parser_uses_reply_ticker_and_ignores_moving_average_numbers():
    idea = parse_heat_idea(
        "底下看21日线的支撑，如果它站上200日线，最好是站上64.2，我才会考虑操作",
        reply_text="TEM setup",
        idea_id="2",
        created_at="2026-07-15T15:00:00+00:00",
    )
    assert idea is not None
    assert idea["ticker"] == "TEM"
    assert idea["trigger_price"] == 64.2
    assert idea["auto_eligible"] is False
    assert idea["mapping_supported"] is False


def test_heat_parser_prefers_actual_buy_ticker_and_reviews_high_risk_trade():
    idea = parse_heat_idea(
        "刚发现schwab夜盘不能买MUZ，但是MUU可以买。30.5买了些MUU，短线博反弹，极高风险",
        idea_id="live-1",
        created_at="2026-07-16T01:49:00+00:00",
    )
    assert idea is not None
    assert idea["ticker"] == "MUU"
    assert idea["trigger_price"] == 30.5
    assert idea["auto_eligible"] is False


def test_heat_parser_prefers_explicit_uppercase_ticker_over_later_alias():
    idea = parse_heat_idea(
        "说一下我买入MUU的交易策略。原计划是MU跌破YDL后收回做反弹。",
        idea_id="muu-context",
        created_at="2026-07-16T13:11:24+00:00",
    )
    assert idea is not None
    assert idea["ticker"] == "MUU"


@pytest.mark.parametrize("text", [
    "SPY put 关注突破600",
    "关注 SPY sell puts",
    "NVDA 200C +80% nice win",
])
def test_heat_parser_ignores_option_show_and_tell(text):
    assert parse_heat_idea(
        text,
        idea_id="option-show",
        created_at="2026-07-15T15:00:00+00:00",
    ) is None


def test_heat_parser_accepts_explicit_bearish_breakdown_route():
    idea = parse_heat_idea(
        "NVDA 做空，跌破150",
        idea_id="4",
        created_at="2026-07-15T15:00:00+00:00",
    )
    assert idea is not None
    assert idea["trigger_price"] == 150
    assert idea["direction"] == "short"
    assert idea["trigger_operator"] == "below"
    assert idea["leveraged_candidates"] == ["NVD", "NVDQ"]


def test_heat_parser_keeps_position_management_as_non_trade_context():
    idea = parse_heat_idea(
        "GOOGL 减仓一半",
        idea_id="5",
        created_at="2026-07-15T15:00:00+00:00",
    )
    assert idea is not None
    assert idea["classification"] == "position_update"
    assert idea["auto_eligible"] is False


def test_materializer_associates_followup_chart_and_review_decision():
    idea = parse_heat_idea(
        "关注GOOGL，有可能要突破了",
        idea_id="10",
        created_at="2026-07-14T17:29:00+00:00",
    )
    rows = materialize_heat_ideas([
        idea,
        {"event_type": "attachment_update", "idea_id": "10", "attachments": ["chart.png"]},
    ], [{
        "idea_id": "10", "decision": "approved", "ticker": "GOOGL",
        "trigger_price": 360.5, "setup": "chart line",
    }])
    assert rows[0]["attachments"] == ["chart.png"]
    assert rows[0]["trigger_price"] == 360.5
    assert rows[0]["status"] == "approved"


def test_materializer_repairs_legacy_m_and_hides_alter_false_positive():
    rows = materialize_heat_ideas([
        {
            "event_type": "idea", "id": "msft", "ticker": "M",
            "text": "Msft关注能否站稳 fib 0.5", "reply_text": None,
            "trigger_price": None, "direction": "long", "auto_eligible": False,
            "created_at": "2026-07-16T17:14:03+00:00",
        },
        {
            "event_type": "idea", "id": "alter", "ticker": "A",
            "text": "不是，是今天才突破黄线的，一触发Alter，就可以买入",
            "reply_text": None, "trigger_price": None, "direction": "long",
            "auto_eligible": False, "created_at": "2026-07-16T16:56:07+00:00",
        },
    ])
    assert [row["ticker"] for row in rows] == ["MSFT"]


def test_day_trader_sync_creates_unarmed_current_day_heat_watch(monkeypatch, workspace_tmp):
    ideas = workspace_tmp / "heat.jsonl"
    decisions = workspace_tmp / "decisions.jsonl"
    settings = workspace_tmp / "settings.json"
    _write_jsonl(ideas, [{
        "event_type": "idea", "id": "heat-1", "ticker": "NVDA",
        "trigger_price": 150.2, "target_price": None, "setup": "Heat breakout",
        "direction": "long", "trigger_operator": "above",
        "auto_eligible": True, "created_at": "2026-07-15T14:00:00+00:00",
    }])
    settings.write_text('{"auto_trading_enabled": true}', encoding="utf-8")
    monkeypatch.setattr(day_trader, "HEAT_IDEAS_PATH", ideas)
    monkeypatch.setattr(day_trader, "HEAT_DECISIONS_PATH", decisions)
    monkeypatch.setattr(day_trader, "HEAT_SETTINGS_PATH", settings)
    positions: list[DayPosition] = []
    with patch("bot.day_trader._append_position") as append:
        changed = _sync_heat_ideas(
            positions, now=datetime(2026, 7, 15, 10, 0, tzinfo=ET)
        )
    assert changed is True
    assert positions[0].source == "heat"
    assert positions[0].heat_idea_id == "heat-1"
    assert positions[0].armed is False
    append.assert_called_once_with(positions[0])


def test_disabling_heat_expires_only_unfilled_watch(monkeypatch, workspace_tmp):
    settings = workspace_tmp / "settings.json"
    settings.write_text('{"auto_trading_enabled": false}', encoding="utf-8")
    monkeypatch.setattr(day_trader, "HEAT_IDEAS_PATH", workspace_tmp / "missing.jsonl")
    monkeypatch.setattr(day_trader, "HEAT_DECISIONS_PATH", workspace_tmp / "missing-decisions.jsonl")
    monkeypatch.setattr(day_trader, "HEAT_SETTINGS_PATH", settings)
    watching = DayPosition(source="heat", heat_idea_id="1", status="watching")
    opened = DayPosition(
        source="heat", heat_idea_id="2", status="open", fill_qty=1.0, fill_price=10.0
    )
    assert _sync_heat_ideas([watching, opened]) is True
    assert watching.status == "expired"
    assert opened.status == "open"
    assert opened.manual_cancel_requested is False


def test_existing_unsupported_heat_watch_is_expired(monkeypatch, workspace_tmp):
    ideas = workspace_tmp / "heat.jsonl"
    settings = workspace_tmp / "settings.json"
    _write_jsonl(ideas, [{
        "event_type": "idea", "id": "nq-1", "ticker": "NQ",
        "trigger_price": 29685, "target_price": None,
        "setup": "29685 买入NQ，20点SL", "auto_eligible": True,
        "created_at": "2026-07-16T02:48:29+00:00",
    }])
    settings.write_text('{"auto_trading_enabled": true}', encoding="utf-8")
    monkeypatch.setattr(day_trader, "HEAT_IDEAS_PATH", ideas)
    monkeypatch.setattr(day_trader, "HEAT_DECISIONS_PATH", workspace_tmp / "decisions.jsonl")
    monkeypatch.setattr(day_trader, "HEAT_SETTINGS_PATH", settings)
    position = DayPosition(
        ticker="NQ", source="heat", heat_idea_id="nq-1", status="watching"
    )
    assert _sync_heat_ideas([position]) is True
    assert position.status == "expired"
    assert position.exit_reason == "heat_unsupported_mapping"


def test_heat_api_review_approve_and_toggle(monkeypatch, workspace_tmp):
    ideas = workspace_tmp / "logs" / "heat.jsonl"
    decisions = workspace_tmp / "state" / "decisions.jsonl"
    settings = workspace_tmp / "state" / "settings.json"
    positions = workspace_tmp / "logs" / "positions.jsonl"
    _write_jsonl(ideas, [{
        "event_type": "idea", "id": "spy-1", "ticker": "SPY",
        "trigger_price": None, "target_price": None, "setup": "chart watch",
        "text": "关注SPY，有可能要突破了", "reply_text": None,
        "attachments": [], "auto_eligible": False, "confidence": "review",
        "created_at": "2026-07-15T14:00:00+00:00",
    }])
    monkeypatch.setattr(api, "HEAT_IDEAS_PATH", ideas)
    monkeypatch.setattr(api, "HEAT_DECISIONS_PATH", decisions)
    monkeypatch.setattr(api, "HEAT_SETTINGS_PATH", settings)
    monkeypatch.setattr(api, "DAY_TRADE_POSITIONS_PATH", positions)
    client = TestClient(api.app)

    initial = client.get("/api/daytrader").json()
    assert initial["heat_ideas"][0]["derived_status"] == "needs_level"
    approved = client.post("/api/daytrader/heat-ideas/spy-1/approve", json={
        "ticker": "SPY", "trigger_price": 660.5,
        "target_price": 672, "setup": "descending-line breakout",
        "direction": "long", "trigger_operator": "above",
    })
    assert approved.status_code == 200
    assert approved.json()["trigger_price"] == 660.5
    assert approved.json()["leveraged_candidates"] == ["SPXL"]

    toggled = client.put("/api/daytrader/heat-settings", json={
        "auto_trading_enabled": True,
    })
    assert toggled.status_code == 200
    assert toggled.json()["auto_trading_enabled"] is True
    final = client.get("/api/daytrader").json()
    assert final["heat_ideas"][0]["derived_status"] == "queued"


def test_heat_api_groups_earlier_context_by_ticker(monkeypatch, workspace_tmp):
    ideas = workspace_tmp / "logs" / "heat.jsonl"
    decisions = workspace_tmp / "state" / "decisions.jsonl"
    settings = workspace_tmp / "state" / "settings.json"
    positions = workspace_tmp / "logs" / "positions.jsonl"
    _write_jsonl(ideas, [
        {
            "event_type": "idea", "id": "msft-old", "ticker": "MSFT",
            "trigger_price": 520, "target_price": None,
            "text": "MSFT突破520关注", "setup": "MSFT突破520关注",
            "reply_text": None, "attachments": ["old.png"],
            "direction": "long", "auto_eligible": False,
            "created_at": "2026-07-15T14:00:00+00:00",
        },
        {
            "event_type": "idea", "id": "msft-new", "ticker": "M",
            "trigger_price": None, "target_price": None,
            "text": "Msft关注能否站稳 fib 0.5", "setup": "fib watch",
            "reply_text": None, "attachments": [],
            "direction": "long", "auto_eligible": False,
            "created_at": "2026-07-16T17:14:03+00:00",
        },
    ])
    monkeypatch.setattr(api, "HEAT_IDEAS_PATH", ideas)
    monkeypatch.setattr(api, "HEAT_DECISIONS_PATH", decisions)
    monkeypatch.setattr(api, "HEAT_SETTINGS_PATH", settings)
    monkeypatch.setattr(api, "DAY_TRADE_POSITIONS_PATH", positions)

    heat = TestClient(api.app).get("/api/daytrader").json()["heat_ideas"]
    assert heat[0]["ticker"] == "MSFT"
    assert heat[0]["is_latest_for_ticker"] is True
    assert heat[0]["history_count"] == 1
    assert heat[0]["ticker_history"][0]["trigger_price"] == 520
    assert heat[0]["ticker_history"][0]["attachment_urls"] == [
        "/api/daytrader/heat-attachments/old.png"
    ]
    assert heat[1]["is_latest_for_ticker"] is False


def test_heat_api_rejected_idea_overrides_expired_position_status(monkeypatch, workspace_tmp):
    ideas = workspace_tmp / "logs" / "heat.jsonl"
    decisions = workspace_tmp / "state" / "decisions.jsonl"
    settings = workspace_tmp / "state" / "settings.json"
    positions = workspace_tmp / "logs" / "positions.jsonl"
    _write_jsonl(ideas, [{
        "event_type": "idea", "id": "nq-1", "ticker": "NQ",
        "trigger_price": 29685, "target_price": None, "setup": "20 point stop",
        "text": "Buy NQ at 29685, 20 point SL", "reply_text": None,
        "attachments": [], "auto_eligible": True, "confidence": "explicit",
        "created_at": "2026-07-16T02:48:29+00:00",
    }])
    _write_jsonl(decisions, [{
        "idea_id": "nq-1", "decision": "rejected",
        "decided_at": "2026-07-16T03:10:25+00:00",
    }])
    _write_jsonl(positions, [{
        "id": "heat-nq-1", "ticker": "NQ", "source": "heat",
        "heat_idea_id": "nq-1", "status": "expired", "fill_qty": None,
        "exit_reason": "heat_disabled",
    }])
    settings.write_text('{"auto_trading_enabled": true}', encoding="utf-8")
    monkeypatch.setattr(api, "HEAT_IDEAS_PATH", ideas)
    monkeypatch.setattr(api, "HEAT_DECISIONS_PATH", decisions)
    monkeypatch.setattr(api, "HEAT_SETTINGS_PATH", settings)
    monkeypatch.setattr(api, "DAY_TRADE_POSITIONS_PATH", positions)

    idea = TestClient(api.app).get("/api/daytrader").json()["heat_ideas"][0]
    assert idea["derived_status"] == "rejected"
    assert idea["position_status"] == "expired"
