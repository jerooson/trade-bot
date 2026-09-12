"""Prior-high level scanner on synthetic daily bars."""

from __future__ import annotations

from datetime import date, timedelta

from bot.daily_data import DailyBar, save_daily
from bot.level_scanner import candidate_level, pivot_highs, scan_symbol


def _daily(closes, highs=None, start=date(2026, 3, 2)):
    bars = []
    d = start
    for i, c in enumerate(closes):
        while d.weekday() >= 5:
            d += timedelta(days=1)
        h = highs[i] if highs and highs[i] is not None else c + 0.5
        bars.append(DailyBar(d, c, h, c - 0.5, c, 1000.0))
        d += timedelta(days=1)
    return bars


def test_pivot_highs_need_three_lower_bars_each_side():
    closes = [100.0] * 100
    highs = [100.5] * 100
    highs[50] = 110.0            # clean pivot
    highs[60] = 108.0
    highs[62] = 109.0            # 60 is not a pivot: 62 is higher within the wing
    bars = _daily(closes, highs)
    idx = [i for i, _ in pivot_highs(bars)]
    assert 50 in idx and 60 not in idx and 62 in idx


def test_candidate_is_nearest_unbroken_pivot_within_distance():
    closes = [100.0] * 100
    highs = [100.5] * 100
    highs[70] = 104.0            # 4% above, unbroken
    highs[80] = 102.0            # 2% above, but closed above afterwards -> broken
    closes[85] = 103.0
    bars = _daily(closes, highs)
    lvl = candidate_level(bars, 99)
    assert lvl is not None and lvl.level == 104.0
    assert 18 <= lvl.age <= 30
    # Too far away is not a candidate.
    highs[70] = 115.0
    assert candidate_level(_daily(closes, highs), 99) is None


def test_scan_emits_once_per_level_per_cooldown_with_matching_control(tmp_path):
    closes = [100.0] * 110
    highs = [100.5] * 110
    highs[80] = 105.0
    bars = _daily(closes, highs)
    start, end = bars[90].day, bars[109].day
    rows, controls = scan_symbol("TEST", bars, start, end)
    assert len(rows) == 2            # sessions 90 and 100 (cooldown 10)
    assert all(r.trigger == 105.0 and r.source == "scan" and r.operator == "above" for r in rows)
    assert len(controls) == len(rows)
    for c in controls:
        assert c.source == "random" and 100.5 <= c.trigger <= 110.0
    # Deterministic controls.
    _, again = scan_symbol("TEST", bars, start, end)
    assert [c.trigger for c in again] == [c.trigger for c in controls]
