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


def test_heat_parser_still_rejects_position_management_only():
    assert parse_heat_idea(
        "GOOGL 减仓一半",
        idea_id="5",
        created_at="2026-07-15T15:00:00+00:00",
    ) is None


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
    assert initial["heat_ideas"][0]["derived_status"] == "needs_review"
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
