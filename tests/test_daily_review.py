"""Daily review assembles the ledgers for one session."""

from __future__ import annotations

import json
from datetime import date

from bot import daily_review as dr


def _jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def test_build_and_render(tmp_path):
    p = dr.Paths(tmp_path)
    _jsonl(p.heat_ideas, [
        {"event_type": "idea", "id": "h1", "ticker": "QQQ", "trigger_price": 708.0, "direction": "long",
         "trigger_operator": "above", "auto_eligible": True, "classification": "actionable_setup",
         "text": "QQQ 关注能否站上 708", "created_at": "2026-09-02T14:15:00+00:00", "attachments": []},
        {"event_type": "idea", "id": "h2", "ticker": "SPY", "trigger_price": None, "direction": "long",
         "trigger_operator": "above", "auto_eligible": False, "classification": "needs_level",
         "text": "SPY 看图", "created_at": "2026-09-02T14:20:00+00:00", "attachments": ["a.png"]},
        {"event_type": "idea", "id": "h3", "ticker": "TSLA", "trigger_price": None, "direction": "long",
         "trigger_operator": "above", "auto_eligible": False, "classification": "option_post",
         "text": "TSLA 期权 Trim 成 runner", "created_at": "2026-09-02T15:00:00+00:00", "attachments": []},
    ])
    _jsonl(p.positions, [
        {"id": "p1", "ticker": "QQQ", "source": "heat", "heat_idea_id": "h1", "status": "closed", "trigger_price": 708.0,
         "execution_ticker": "TQQQ", "execution_leverage": 3.0, "entered_at": "2026-09-02T14:40:00+00:00",
         "fill_price": 71.0, "fill_qty": 0.7, "entry_filled_value": 49.7, "entry_limit_price": 71.1,
         "closed_at": "2026-09-02T19:50:00+00:00", "exit_price": 72.0, "exit_reason": "eod",
         "realized_pnl": 0.7, "realized_pnl_pct": 1.4, "armed": True},
        {"id": "p2", "ticker": "AMD", "source": "discord", "status": "watching", "trigger_price": 150.0, "armed": True,
         "plan_received_at": "2026-09-02T13:00:00+00:00"},
    ])
    _jsonl(p.signals, [{"kind": "PLAN", "ticker": "BTE", "side": "LONG", "trigger": 5.02, "received_at": "2026-09-02T14:30:00+00:00"}])
    _jsonl(p.option_shadow, [
        {"event": "open", "idea_id": "h1", "ticker": "QQQ", "contract": {"expiration": "2026-09-02", "strike": 708.0, "kind": "call"},
         "price": 1.2, "underlying": 708.1, "ts": "2026-09-02T14:40:00+00:00"},
        {"event": "closed", "idea_id": "h1", "ticker": "QQQ", "exit_reason": "eod", "realized_pct": 25.0, "realized_usd": 300.0,
         "max_gain_pct": 40.0, "ts": "2026-09-02T19:50:00+00:00"},
    ])
    r = dr.build(date(2026, 9, 2), p)
    assert r["heat"]["count"] == 3 and len(r["heat"]["approved"]) == 1 and len(r["heat"]["needs_review"]) == 1
    assert len(r["heat"]["option_posts"]) == 1
    assert r["day_trades"]["summary"]["n"] == 1 and r["day_trades"]["summary"]["pnl_usd"] == 0.7
    assert r["day_trades"]["closed"][0]["slippage_vs_cap_pct"] == -0.14
    assert [w["ticker"] for w in r["day_trades"]["still_watching"]] == ["AMD"]
    outcomes = {x["ticker"]: x["outcome"] for x in r["heat_vs_bot"]}
    assert outcomes["QQQ"].startswith("traded:eod") and outcomes["SPY"] == "not_approved:needs_review"
    assert r["discord_plans_recorded"]["count"] == 1
    assert r["option_shadow"]["summary"] == {"n": 1, "avg_pct": 25.0, "usd": 300.0}
    md = dr.render_markdown(r)
    assert "| QQQ | TQQQ x3 | heat |" in md and "BTE@5.02" in md and "traded:eod" in md
    out = dr.write(date(2026, 9, 2), p)
    assert out.exists() and (p.out_dir / "2026-09-02.json").exists()


def test_narrative_skips_on_usage_limit(monkeypatch):
    import subprocess

    class P:
        returncode = 1
        stdout = ""
        stderr = "ERROR: You have hit your usage limit."

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: P())
    text, err = dr.narrative({"date": "2026-09-02"})
    assert text is None and "usage limit" in err


def test_email_not_configured(monkeypatch):
    monkeypatch.delenv("REVIEW_EMAIL_TO", raising=False)
    assert dr.send_email("s", "b") == "email not configured"


def test_discord_not_configured(monkeypatch):
    monkeypatch.delenv("REVIEW_DISCORD_WEBHOOK", raising=False)
    assert dr.send_discord("s", "# x\n\n## Heat feed\n- a", "x.md") == "discord not configured"


def test_discord_embeds_respect_limits(tmp_path):
    from pathlib import Path
    r = json.loads(Path("logs/reviews/2026-09-02.json").read_text(encoding="utf-8")) if Path("logs/reviews/2026-09-02.json").exists() else None
    if r is None:
        p = dr.Paths(tmp_path)
        r = dr.build(date(2026, 9, 2), p)
    embeds = dr.discord_embeds(r)
    e = embeds[0]
    assert e["title"].startswith("📊")
    assert len(e["fields"]) <= 25
    assert all(len(f["value"]) <= 1024 and len(f["name"]) <= 256 for f in e["fields"])
    total = len(e["title"]) + sum(len(f["name"]) + len(f["value"]) for f in e["fields"]) + len(e.get("description", ""))
    assert total <= 6000
