"""Feature table on synthetic daily bars."""

from __future__ import annotations

from datetime import date, timedelta

from bot import features
from bot.daily_data import DailyBar, save_daily


def _daily(closes, start=date(2026, 5, 1), vol=1000.0):
    bars, d = [], start
    for c in closes:
        while d.weekday() >= 5:
            d += timedelta(days=1)
        bars.append(DailyBar(d, c, c + 1, c - 1, c, vol))
        d += timedelta(days=1)
    return bars


def test_ticker_and_market_features(tmp_path, monkeypatch):
    monkeypatch.setattr("bot.daily_data.DAILY_DIR", tmp_path)
    # ticker drifts up 1%/day for 60 days; SPY flat
    save_daily("XYZ", _daily([100 * 1.01 ** i for i in range(60)]), tmp_path)
    save_daily("SPY", _daily([500.0] * 60), tmp_path)
    save_daily("QQQ", _daily([400.0] * 60), tmp_path)
    day = _daily([0] * 60)[-1].day + timedelta(days=1)
    f = features.ticker_features("XYZ", day, level=200.0)
    assert f["ret20"] > 20 and f["above_sma21"] == 1 and f["above_sma50"] == 1
    assert f["vol_ratio20"] == 1.0
    assert 0 < f["atr14_pct"] < 3
    m = features.market_features(day)
    assert m["mkt_spy_vs_8d"] == 0.0 and m["mkt_spy_vs_21d"] == 0.0 and m["spy_ret20"] == 0.0


def test_compare_reports_effect_size():
    table = [
        {"source": "heat", "pnl_pct": 1.0, "ret20": -1.0, "hour_of_entry": 10.5},
        {"source": "heat", "pnl_pct": 1.0, "ret20": -0.5, "hour_of_entry": 10.5},
    ] * 4 + [
        {"source": "scan", "pnl_pct": 0.0, "ret20": 5.0, "hour_of_entry": 9.9},
        {"source": "scan", "pnl_pct": 0.0, "ret20": 6.0, "hour_of_entry": 9.9},
    ] * 4
    text = features.compare(table, groups=("heat", "scan"))
    line = next(l for l in text.splitlines() if l.startswith("ret20"))
    assert line.strip().endswith(("-1.99", "-2.00", "-2.01")) or float(line.split()[-1]) < -1.5
    assert "scan: ret20" not in text
    assert features.conditional_pnl(table, "ret20", 2.0, "scan").startswith("scan ret20 < 2.0: n=0")
