"""A newer Heat level replaces an older, unfilled Heat watch on the same ticker."""

from __future__ import annotations

import json
from datetime import datetime

from bot import day_trader
from bot.day_trader import DayPosition, ET, _sync_heat_ideas


def _write_jsonl(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


def _setup(tmp_path, monkeypatch, ideas):
    monkeypatch.setattr(day_trader, "HEAT_IDEAS_PATH", _write_jsonl(tmp_path / "heat.jsonl", ideas))
    monkeypatch.setattr(day_trader, "HEAT_DECISIONS_PATH", tmp_path / "decisions.jsonl")
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"auto_trading_enabled": True}), encoding="utf-8")
    monkeypatch.setattr(day_trader, "HEAT_SETTINGS_PATH", settings)


NEW = {"event_type": "idea", "id": "tsla-new", "ticker": "TSLA", "trigger_price": 370.0,
       "direction": "long", "trigger_operator": "above", "auto_eligible": True,
       "created_at": "2026-09-11T13:54:00+00:00"}
OLD = {"event_type": "idea", "id": "tsla-old", "ticker": "TSLA", "trigger_price": 280.0,
       "direction": "long", "trigger_operator": "above", "auto_eligible": True,
       "created_at": "2026-08-20T14:00:00+00:00"}
NOW = datetime(2026, 9, 11, 10, 0, tzinfo=ET)


def test_new_heat_level_supersedes_stale_heat_watch(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, [OLD, NEW])
    old = DayPosition(ticker="TSLA", source="heat", heat_idea_id="tsla-old", status="watching",
                      trigger_price=280.0, good_til_cancelled=True, armed=False,
                      plan_received_at="2026-08-20T14:00:00+00:00")
    positions = [old]
    assert _sync_heat_ideas(positions, now=NOW) is True
    assert old.status == "expired" and old.exit_reason == "heat_superseded"
    new = [p for p in positions if p.heat_idea_id == "tsla-new"]
    assert len(new) == 1 and new[0].trigger_price == 370.0 and new[0].status == "watching"


def test_filled_heat_position_still_blocks_new_watch(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, [NEW])
    open_heat = DayPosition(ticker="TSLA", source="heat", heat_idea_id="tsla-open", status="open",
                            trigger_price=300.0, fill_qty=1.0, plan_received_at="2026-09-10T14:00:00+00:00")
    positions = [open_heat]
    _sync_heat_ideas(positions, now=NOW)
    assert open_heat.status == "open"
    assert not any(p.heat_idea_id == "tsla-new" for p in positions)


def test_manual_watch_still_blocks_new_heat_watch(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, [NEW])
    manual = DayPosition(ticker="TSLA", source="manual", manual_plan_id="m1", status="watching",
                         trigger_price=350.0, plan_received_at="2026-09-11T13:00:00+00:00")
    positions = [manual]
    _sync_heat_ideas(positions, now=NOW)
    assert manual.status == "watching"
    assert not any(p.heat_idea_id == "tsla-new" for p in positions)
