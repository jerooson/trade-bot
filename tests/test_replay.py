"""Replay engine on synthetic minute bars."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import pytest

from bot.market_data import ET, Bar, save_bars, session_date_for
from bot.replay import POLICIES, Policy, replay_row, simulate, summarize
from bot.signal_dataset import SignalRow

DAY = date(2026, 9, 2)


def _bars(path, start_minute=0, count=390, ohlc=None):
    """Flat 100.0 bars from 09:30 unless ``path`` (list of closes) is given."""
    bars = []
    t0 = datetime(DAY.year, DAY.month, DAY.day, 9, 30, tzinfo=ET)
    closes = path or [100.0] * count
    prev = closes[0]
    for i, c in enumerate(closes):
        ts = t0 + timedelta(minutes=i)
        o = prev
        hi, lo = max(o, c), min(o, c)
        if ohlc and i in ohlc:
            o, hi, lo, c = ohlc[i]
        bars.append(Bar(ts, o, hi, lo, c, 1000.0))
        prev = c
    return bars


def _row(trigger=101.0, source="discord", target=None, posted="2026-09-02T09:00:00-04:00", execution=None, leverage=1.0):
    return SignalRow(id=f"{source}:1", source=source, ticker="TEST", trigger=trigger, operator="above",
                     direction="long", target=target, posted_at=posted, session=DAY.isoformat(),
                     execution=execution or "TEST", leverage=leverage)


def test_cross_enters_at_level_and_stop_fires_on_bar_low():
    closes = [100.0] * 5 + [101.5] + [101.0] * 10 + [98.5] + [99.0] * 373
    r = simulate(_row(), _bars(closes), _bars(closes), POLICIES[0], session=DAY, armed=True)
    assert r.entered and r.entry_price == 101.0
    assert r.exit_reason == "stop"
    assert r.exit_price == pytest.approx(round(101.0 * 0.98, 4))
    assert r.pnl_pct == pytest.approx(-2.0, abs=1e-3)
    assert r.mae_pct <= -2.0


def test_unarmed_watch_needs_a_close_below_the_level_first():
    closes = [102.0] * 5 + [100.0] * 5 + [101.5] + [101.0] * 379
    r = simulate(_row(source="heat"), _bars(closes), _bars(closes), POLICIES[0], session=DAY, armed=False)
    assert r.entered and r.entry_ts.endswith("09:40:00-04:00")
    armed_too_early = simulate(_row(source="heat"), _bars([102.0] * 390), _bars([102.0] * 390), POLICIES[0], session=DAY, armed=False)
    assert not armed_too_early.entered and armed_too_early.skip_reason == "never_triggered"


def test_gap_beyond_entry_cap_is_not_filled():
    closes = [100.0] * 5 + [103.0] * 385
    bars = _bars(closes, ohlc={5: (103.0, 103.2, 102.9, 103.0)})   # opens 2% above the 101 level
    r = simulate(_row(), bars, bars, POLICIES[0], session=DAY, armed=True)
    assert not r.entered
    # A bar that opens below the level and runs through it fills at the level.
    filled = simulate(_row(), _bars(closes), _bars(closes), POLICIES[0], session=DAY, armed=True)
    assert filled.entered and filled.entry_price == 101.0


def test_milestone_locks_profit_and_force_close_exits_at_1550():
    # +1% confirmed on one close -> stop to -0.5%; then drift; force close at 15:50
    closes = [100.0] * 5 + [101.0] + [102.2] * 384
    r = simulate(_row(), _bars(closes), _bars(closes), POLICIES[0], session=DAY, armed=True)
    assert r.entered and r.exit_reason == "eod"
    assert r.exit_ts.endswith("15:50:00-04:00")
    assert r.pnl_pct == pytest.approx((102.2 - 101.0) / 101.0 * 100, abs=1e-3)


def test_trailing_stop_after_milestone_beats_flat_stop():
    # Runs to +4%, then collapses to -3%: live locks +0.5%; flat -2% stop loses 2%.
    closes = [100.0] * 5 + [101.0] + [103.0, 104.0, 105.0, 105.1] + [97.0] * 380
    live = simulate(_row(), _bars(closes), _bars(closes), POLICIES[0], session=DAY, armed=True)
    flat = simulate(_row(), _bars(closes), _bars(closes), Policy("stop2_flat", 2.0, False, False, False), session=DAY, armed=True)
    assert live.exit_reason == "stop" and live.pnl_pct > 0
    assert flat.exit_reason == "stop" and flat.pnl_pct == pytest.approx(-2.0, abs=1e-3)


def test_leveraged_route_uses_underlying_for_risk_and_etf_for_pnl():
    under = [100.0] * 5 + [101.0] + [101.5] * 10 + [98.0] + [98.0] * 373
    etf = [50.0] * 5 + [51.5] + [52.2] * 10 + [47.0] + [47.0] * 373
    row = _row(execution="TEST3X", leverage=3.0)
    r = simulate(row, _bars(under), _bars(etf), POLICIES[0], session=DAY, armed=True)
    assert r.entered and r.entry_price == 51.5 and r.entry_risk_price == 101.0
    assert r.exit_reason == "stop" and r.exit_price == 47.0
    assert r.pnl_pct == pytest.approx((47.0 - 51.5) / 51.5 * 100, abs=1e-3)


def test_replay_row_tries_later_sessions_for_gtc_levels(tmp_path):
    row = _row(source="heat", posted="2026-09-02T18:00:00-04:00")
    row.session = "2026-09-03"
    save_bars("TEST", date(2026, 9, 3), _bars([99.0] * 390), tmp_path)
    next_day = _bars([100.0] * 5 + [101.2] + [101.0] * 384)
    next_day = [Bar(b.ts + timedelta(days=2), b.open, b.high, b.low, b.close, b.volume) for b in next_day]
    save_bars("TEST", date(2026, 9, 4), next_day, tmp_path)
    one = replay_row(row, [POLICIES[0]], sessions=1, root=tmp_path)[0]
    five = replay_row(row, [POLICIES[0]], sessions=5, root=tmp_path)[0]
    assert not one.entered and one.skip_reason == "never_triggered"
    assert five.entered and five.session == "2026-09-04"
    # Walking past the cached days is still "never triggered", not "no bars".
    save_bars("TEST", date(2026, 9, 4), [Bar(b.ts, 99.0, 99.0, 99.0, 99.0, 1.0) for b in next_day], tmp_path)
    many = replay_row(row, [POLICIES[0]], sessions=10, root=tmp_path)[0]
    assert not many.entered and many.skip_reason == "never_triggered"


def test_close_entry_mode_ignores_a_wick_through_the_level():
    closes = [100.0] * 5 + [100.5] + [100.0] * 384
    bars = _bars(closes, ohlc={5: (100.0, 101.4, 100.0, 100.5)})   # wick to 101.4, closes 100.5
    touch = simulate(_row(), bars, bars, Policy("t", entry_mode="touch"), session=DAY, armed=True)
    close = simulate(_row(), bars, bars, Policy("c", entry_mode="close"), session=DAY, armed=True)
    assert touch.entered and touch.entry_price == 101.0 and not close.entered
    # A confirmed close pays the close, not the level.
    confirmed = _bars([100.0] * 5 + [101.15] + [101.0] * 384)
    c = simulate(_row(), confirmed, confirmed, Policy("c", entry_mode="close"), session=DAY, armed=True)
    assert c.entered and c.entry_price == 101.15


def test_session_date_rolls_after_close_and_over_weekends():
    assert session_date_for(datetime(2026, 9, 11, 15, 59, tzinfo=ET)) == date(2026, 9, 11)
    assert session_date_for(datetime(2026, 9, 11, 16, 0, tzinfo=ET)) == date(2026, 9, 14)
    assert session_date_for(datetime(2026, 9, 12, 10, 0, tzinfo=ET)) == date(2026, 9, 14)


def test_summary_groups_by_policy_and_source():
    closes = [100.0] * 5 + [101.5] + [102.0] * 384
    r = simulate(_row(), _bars(closes), _bars(closes), POLICIES[0], session=DAY, armed=True)
    s = summarize([r])
    assert s["live/discord"]["entered"] == 1 and s["live/all"]["win_rate"] == 100.0


def test_reviewed_chart_levels_fill_heat_ideas_without_numeric_levels(tmp_path):
    from bot.signal_dataset import heat_rows
    ideas = tmp_path / "heat.jsonl"
    ideas.write_text(json.dumps({
        "event_type": "idea", "id": "h1", "ticker": "PLTR", "trigger_price": None,
        "text": "PLTR 黄线强阻力，关注能否站上去", "direction": "long", "trigger_operator": "above",
        "auto_eligible": False, "classification": "needs_level",
        "created_at": "2026-08-05T15:43:00+00:00", "attachments": ["x-0.png"],
    }) + "\n", encoding="utf-8")
    reviewed = tmp_path / "reviewed.jsonl"
    reviewed.write_text(
        json.dumps({"id": "h1", "level": 162.4, "operator": "above", "kind": "yellow_line", "confidence": 0.85}) + "\n"
        + json.dumps({"id": "h2", "level": 10.0, "operator": "above", "kind": "guess", "confidence": 0.3}) + "\n",
        encoding="utf-8",
    )
    rows = heat_rows(ideas, None, reviewed)
    assert len(rows) == 1 and rows[0].trigger == 162.4
    assert any(n.startswith("chart_level:yellow_line") for n in rows[0].notes)
    # A low-confidence review never becomes a level.
    ideas.write_text(ideas.read_text(encoding="utf-8").replace('"h1"', '"h2"'), encoding="utf-8")
    rows = heat_rows(ideas, None, reviewed)
    assert rows[0].trigger is None and "chart_reviewed_no_level:guess" in rows[0].notes


def test_exit_time_flattens_at_the_given_bar():
    from datetime import time as dtime
    closes = [100.0] * 5 + [101.0] + [102.0] * 384      # cross at 09:35, then flat at 102
    pol = Policy("exit12", exit_time=dtime(12, 0))
    r = simulate(_row(), _bars(closes), _bars(closes), pol, session=DAY, armed=True)
    assert r.exit_reason == "time" and r.exit_ts.endswith("12:00:00-04:00")
    assert r.pnl_pct == pytest.approx((102.0 - 101.0) / 101.0 * 100, abs=1e-3)


def test_trim_realises_half_at_the_trim_level_and_runs_the_rest():
    closes = [100.0] * 5 + [101.0] + [103.5] * 10 + [98.0] * 374    # +2.5% then through the -2% stop
    pol = Policy("trim", stop_pct=2.0, trailing=False, eod_tighten=False, trim_pct=2.0, trim_frac=0.5)
    r = simulate(_row(), _bars(closes), _bars(closes), pol, session=DAY, armed=True)
    trimmed = (101.0 * 1.02 - 101.0) / 101.0 * 100 * 0.5           # half sold at +2%
    rest = -2.0 * 0.5                                              # other half stopped at -2%
    assert r.pnl_pct == pytest.approx(trimmed + rest, abs=0.05)
    untrimmed = simulate(_row(), _bars(closes), _bars(closes), Policy("flat", 2.0, False, False, False), session=DAY, armed=True)
    assert untrimmed.pnl_pct == pytest.approx(-2.0, abs=0.05)


def test_milestone_override_changes_lock_level():
    closes = [100.0] * 5 + [101.0] + [102.5] * 5 + [101.6] * 379     # +1.5% then fade to +0.6%
    tight = Policy("tight", milestones=((0.5, -0.5), (1.0, 0.8)))
    r = simulate(_row(), _bars(closes), _bars(closes), tight, session=DAY, armed=True)
    assert r.exit_reason == "stop" and r.pnl_pct == pytest.approx(0.8, abs=0.05)
