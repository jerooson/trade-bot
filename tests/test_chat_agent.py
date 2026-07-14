from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path

import pytest

from bot import chat_agent


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )


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


def test_context_merges_manual_registry_with_runtime(workspace_tmp: Path) -> None:
    log_dir = workspace_tmp / "logs"
    plans_path = workspace_tmp / "state" / "manual_day_trade_plans.json"
    plans_path.parent.mkdir(parents=True)
    plans_path.write_text(json.dumps([
        {
            "id": "manual-gtlb",
            "ticker": "GTLB",
            "trigger_price": 34.06,
            "target_price": None,
            "setup": "breakout watch",
            "status": "active",
        },
        {
            "id": "manual-nvda",
            "ticker": "NVDA",
            "trigger_price": 212.70,
            "target_price": 216.0,
            "setup": "Fib 0",
            "status": "active",
        },
    ]), encoding="utf-8")
    _write_jsonl(log_dir / "day_trade_positions.jsonl", [
        {
            "id": "position-gtlb",
            "ticker": "GTLB",
            "manual_plan_id": "manual-gtlb",
            "source": "manual",
            "status": "watching",
            "trigger_price": 999.0,
            "current_price": 33.005,
            "armed": True,
        },
        {
            "id": "position-aapl",
            "ticker": "AAPL",
            "source": "discord",
            "status": "watching",
            "trigger_price": 317.25,
            "current_price": 316.5,
            "armed": True,
        },
    ])

    ctx = chat_agent._load_context(log_dir, plans_path)
    plans = {plan["ticker"]: plan for plan in ctx["day_trade_plans"]}

    assert set(plans) == {"AAPL", "GTLB", "NVDA"}
    assert plans["GTLB"]["trigger_price"] == 34.06
    assert plans["GTLB"]["status"] == "armed"
    assert plans["GTLB"]["current_price"] == 33.005
    assert plans["NVDA"]["status"] == "queued"
    assert plans["NVDA"]["trigger_price"] == 212.70
    assert plans["AAPL"]["source"] == "discord"


def test_prompt_exposes_triggers_and_local_plan_routing(monkeypatch) -> None:
    monkeypatch.setattr(chat_agent, "_load_context", lambda: {
        "swing_positions": {},
        "recent_signals": [],
        "day_trades": [],
        "day_trade_plans": [{
            "ticker": "HPE",
            "source": "manual",
            "status": "armed",
            "trigger_price": 49.88,
            "target_price": None,
            "current_price": 49.295,
            "stop_price": None,
            "armed": True,
            "setup": "breakout",
        }],
    })

    prompt = chat_agent.build_prompt("what is my HPE trigger?", [])

    assert "HPE: source=manual, status=armed, LONG trigger > $49.88" in prompt
    assert "authoritative for plans, watches, and triggers" in prompt
    assert "Do not\n   call Robinhood tools solely to discover these plans" in prompt
    assert "lack of Robinhood\n   orders" in prompt


def test_robinhood_profile_enables_quote_tool() -> None:
    config = Path("deploy/codex-robinhood-live.toml").read_text(encoding="utf-8")
    assert '"get_equity_quotes"' in config
