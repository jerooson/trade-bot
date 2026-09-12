"""
Per-trade feature table: what does Heat's selection look like next to the
scanner's?

For every replayed entry (Heat, scanner, random) compute features that were
knowable at the time of the signal, then compare the distributions.  The
point is to let Heat's choices reveal his filters instead of guessing
indicators: a feature whose distribution differs sharply between his trades
and the scanner's is a candidate filter worth testing; one that does not
differ is noise.

Features (all from cached daily bars, prior to the session, plus the
entry-minute volume from the cached 1-minute bars):

    mkt_spy_vs_8d, mkt_spy_vs_21d   SPY close / SMA - 1 (%), prior day
    mkt_qqq_vs_21d
    rs20                            ticker 20-day return minus SPY 20-day return (%)
    ret5, ret20                     ticker 5/20-day return (%)
    vol_ratio20                     prior-day volume / 20-day average volume
    atr14_pct                       14-day ATR / close (%)
    dist_pct                        (level - prior close) / prior close (%)
    above_sma21, above_sma50        prior close above the SMA (0/1)
    entry_minute_vol_ratio          volume of the entry minute / average minute volume that day so far
    hour_of_entry                   fractional ET hour of the entry bar

    python -m bot.features --out data/features.jsonl
"""

from __future__ import annotations

import argparse
import json
import statistics as st
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

from bot.daily_data import DailyBar, load_daily
from bot.market_data import ET, load_bars
from bot.signal_dataset import read_dataset


def _sma(vals: list[float], n: int) -> float | None:
    return sum(vals[-n:]) / n if len(vals) >= n else None


def _daily_before(symbol: str, day: date) -> list[DailyBar]:
    return [b for b in (load_daily(symbol) or []) if b.day < day]


def market_features(day: date) -> dict[str, float | None]:
    out: dict[str, float | None] = {}
    for sym, keys in (("SPY", ("mkt_spy_vs_8d", 8, "mkt_spy_vs_21d", 21)), ("QQQ", (None, 8, "mkt_qqq_vs_21d", 21))):
        bars = _daily_before(sym, day)
        closes = [b.close for b in bars]
        if not closes:
            continue
        for key, n in ((keys[0], keys[1]), (keys[2], keys[3])):
            if key is None:
                continue
            sma = _sma(closes, n)
            out[key] = round((closes[-1] / sma - 1) * 100, 3) if sma else None
    spy = [b.close for b in _daily_before("SPY", day)]
    out["spy_ret20"] = round((spy[-1] / spy[-21] - 1) * 100, 3) if len(spy) > 21 else None
    return out


def ticker_features(symbol: str, day: date, level: float) -> dict[str, float | None]:
    bars = _daily_before(symbol, day)
    if len(bars) < 22:
        return {}
    closes = [b.close for b in bars]
    vols = [b.volume for b in bars]
    c = closes[-1]
    sma21, sma50 = _sma(closes, 21), _sma(closes, 50)
    trs = [max(b.high - b.low, abs(b.high - p.close), abs(b.low - p.close)) for p, b in zip(bars[-15:-1], bars[-14:])]
    return {
        "ret5": round((c / closes[-6] - 1) * 100, 3),
        "ret20": round((c / closes[-21] - 1) * 100, 3),
        "vol_ratio20": round(vols[-1] / (sum(vols[-21:-1]) / 20), 3) if sum(vols[-21:-1]) > 0 else None,
        "atr14_pct": round(sum(trs) / len(trs) / c * 100, 3) if trs else None,
        "dist_pct": round((level - c) / c * 100, 3),
        "above_sma21": int(c > sma21) if sma21 else None,
        "above_sma50": int(c > sma50) if sma50 else None,
        "dist_sma21_pct": round((c / sma21 - 1) * 100, 3) if sma21 else None,
    }


def entry_features(symbol: str, entry_ts: str | None) -> dict[str, float | None]:
    if not entry_ts:
        return {}
    ts = datetime.fromisoformat(entry_ts).astimezone(ET)
    bars = load_bars(symbol, ts.date()) or []
    idx = next((i for i, b in enumerate(bars) if b.ts == ts), None)
    if idx is None or idx == 0:
        return {"hour_of_entry": round(ts.hour + ts.minute / 60, 2)}
    avg = sum(b.volume for b in bars[:idx]) / idx
    return {
        "entry_minute_vol_ratio": round(bars[idx].volume / avg, 3) if avg > 0 else None,
        "hour_of_entry": round(ts.hour + ts.minute / 60, 2),
    }


def build(dataset_paths: list[Path], replay_paths: list[Path], policy: str = "live") -> list[dict[str, Any]]:
    rows = {}
    for p in dataset_paths:
        for r in read_dataset(p):
            rows[r.id] = r
    out = []
    for p in replay_paths:
        for line in p.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            if r["policy"] != policy or not r["entered"]:
                continue
            sig = rows.get(r["signal_id"])
            if sig is None:
                continue
            day = date.fromisoformat(r["session"])
            feats = {"signal_id": r["signal_id"], "source": r["source"], "ticker": r["ticker"],
                     "session": r["session"], "pnl_pct": r["pnl_pct"], "exit_reason": r["exit_reason"],
                     "mfe_pct": r["mfe_pct"], "mae_pct": r["mae_pct"], "leverage": r["leverage"]}
            feats.update(market_features(day))
            feats.update(ticker_features(sig.ticker, day, float(sig.trigger)))
            spy = [b.close for b in _daily_before("SPY", day)]
            if feats.get("ret20") is not None and len(spy) > 21:
                feats["rs20"] = round(feats["ret20"] - (spy[-1] / spy[-21] - 1) * 100, 3)
            feats.update(entry_features(sig.ticker, r["entry_ts"]))
            out.append(feats)
    return out


FEATURES = ["mkt_spy_vs_8d", "mkt_spy_vs_21d", "mkt_qqq_vs_21d", "spy_ret20", "rs20", "ret5", "ret20",
            "vol_ratio20", "atr14_pct", "dist_pct", "dist_sma21_pct", "above_sma21", "above_sma50",
            "entry_minute_vol_ratio", "hour_of_entry"]


def compare(table: list[dict[str, Any]], groups: tuple[str, ...] = ("heat", "scan", "random")) -> str:
    """Median and mean per group, plus a crude effect size heat-vs-scan."""
    lines = [f"{'feature':<24}" + "".join(f"{g + ' med':>12}{g + ' mean':>12}" for g in groups) + f"{'heat-scan (sd)':>16}"]
    for f in FEATURES:
        cells = []
        vals = {}
        for g in groups:
            v = [float(r[f]) for r in table if r["source"] == g and r.get(f) is not None]
            vals[g] = v
            cells.append(f"{st.median(v):>12.2f}{st.mean(v):>12.2f}" if v else f"{'-':>12}{'-':>12}")
        h, s = vals.get("heat", []), vals.get("scan", [])
        eff = ""
        if len(h) > 5 and len(s) > 5:
            pooled = st.pstdev(h + s) or 1.0
            eff = f"{(st.mean(h) - st.mean(s)) / pooled:>+16.2f}"
        lines.append(f"{f:<24}" + "".join(cells) + eff)
    return "\n".join(lines)


def conditional_pnl(table: list[dict[str, Any]], feature: str, cut: float, source: str = "scan") -> str:
    lo = [r["pnl_pct"] for r in table if r["source"] == source and r.get(feature) is not None and float(r[feature]) < cut]
    hi = [r["pnl_pct"] for r in table if r["source"] == source and r.get(feature) is not None and float(r[feature]) >= cut]
    def s(v):
        return f"n={len(v):<4} avg={st.mean(v):+.3f} win={sum(1 for x in v if x > 0) / len(v) * 100:4.1f}%" if v else "n=0"
    return f"{source} {feature} < {cut}: {s(lo)} | >= {cut}: {s(hi)}"


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="Per-trade feature table")
    p.add_argument("--datasets", type=Path, nargs="+", default=[Path("data/signals_dataset.jsonl"), Path("data/scan_dataset.jsonl")])
    p.add_argument("--replays", type=Path, nargs="+", default=[Path("data/replay_touch.jsonl"), Path("data/replay_scan.jsonl")])
    p.add_argument("--out", type=Path, default=Path("data/features.jsonl"))
    args = p.parse_args(argv)
    table = build(args.datasets, args.replays)
    args.out.write_text("".join(json.dumps(r) + "\n" for r in table), encoding="utf-8")
    counts = defaultdict(int)
    for r in table:
        counts[r["source"]] += 1
    print("rows:", dict(counts))
    print(compare(table))


if __name__ == "__main__":
    main()
