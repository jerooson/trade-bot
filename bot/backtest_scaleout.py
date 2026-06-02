"""
Scale-out ladder backtest.

Question this answers: "If I bought 100% at the trigger, what's the best
ladder of partial-profit-takes vs. holding for trail-stop runners?"

Approach: for every TRIGGER signal in logs/history.jsonl, we walk the
intraday bars and simulate a position that:
  1) starts 100% long at the trigger price
  2) sells fixed fractions at each rung as the price clears it
  3) trails the remainder with a 2% peak-trail
  4) flat-by-close fallback if neither stop nor full ladder fired

Compares several ladder profiles head-to-head.

Run:
    python -m bot.backtest_scaleout
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from bot.backtest import (
    Bar,
    fetch_intraday,
    find_entry,
    regular_session,
    to_bars,
)


# A ladder is a list of (trim_pct_above_entry, fraction_of_position_to_sell).
# Plus a final trail_pct that runs the residual.
@dataclass
class Ladder:
    name: str
    rungs: list[tuple[float, float]]  # (price_pct_above_entry, fraction_to_sell)
    trail_pct: float                  # trail behind running peak for the residual
    hard_stop_pct: float              # hard floor as fraction below entry, e.g. -0.02


# -- Simulator ----------------------------------------------------------------

def simulate(bars: list[Bar], entry_idx: int, entry_px: float, ladder: Ladder) -> dict:
    """
    Walk forward, tracking remaining fraction. Returns weighted exit price &
    P/L %.
    """
    # Sort rungs by ascending price level so we hit lower rungs first.
    rungs = sorted(ladder.rungs, key=lambda x: x[0])
    rung_levels = [(entry_px * (1 + lvl / 100), frac) for lvl, frac in rungs]
    rung_idx = 0
    remaining = 1.0
    proceeds_pct = 0.0           # weighted price * fraction sold (in entry-relative units)
    peak = entry_px
    last_bar = bars[-1]

    for b in bars[entry_idx:]:
        # Update peak from this bar's high BEFORE checking stops, so the trail
        # naturally provides a floor at peak * (1+trail_pct/100), which on the
        # entry bar is at most entry * 0.98 = the same floor a hard stop would.
        peak = max(peak, b.high)

        # Within the bar, conservative ordering: rungs (limits at fixed levels)
        # fire on the way up before the low is reached. Most rungs are below
        # the bar's high if we're above them, so this matters only when a bar
        # straddles a rung. Sell partials here.
        while rung_idx < len(rung_levels) and remaining > 0 and b.high >= rung_levels[rung_idx][0]:
            level_px, frac = rung_levels[rung_idx]
            sell = min(frac, remaining)
            proceeds_pct += sell * (level_px / entry_px - 1) * 100
            remaining -= sell
            rung_idx += 1

        # Then the trail can fire on the bar's low. (If price runs straight up
        # without ever touching peak * 0.98 again, this never fires.)
        trail_px = peak * (1 + ladder.trail_pct / 100)
        if b.low <= trail_px and remaining > 0:
            proceeds_pct += remaining * (trail_px / entry_px - 1) * 100
            return {
                "exit_reason": f"trail {abs(ladder.trail_pct):.1f}% (peak {peak:.2f})",
                "exit_ts": b.ts,
                "remaining_at_exit": 0.0,
                "pnl_pct": proceeds_pct,
            }

    # End of session -- flatten residual at close.
    if remaining > 0:
        proceeds_pct += remaining * (last_bar.close / entry_px - 1) * 100
    return {
        "exit_reason": "session close",
        "exit_ts": last_bar.ts,
        "remaining_at_exit": remaining,
        "pnl_pct": proceeds_pct,
    }


# -- MFE distribution helper --------------------------------------------------

def mfe_pct(bars: list[Bar], entry_idx: int, entry_px: float) -> float:
    return (max(b.high for b in bars[entry_idx:]) / entry_px - 1) * 100


# -- Trigger loading (small dup of backtest_all to avoid import cycle) -------

@dataclass
class TriggerSignal:
    ticker: str
    trigger: float
    trade_date: date


def load_triggers(jsonl_path: Path) -> list[TriggerSignal]:
    out: list[TriggerSignal] = []
    with jsonl_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            r = json.loads(line)
            if r.get("kind") != "TRIGGER":
                continue
            ticker = r.get("ticker")
            trigger = r.get("trigger")
            ts_iso = r.get("discord", {}).get("created_at") or r.get("received_at")
            if not (ticker and trigger is not None and ts_iso):
                continue
            ts = datetime.fromisoformat(ts_iso)
            out.append(TriggerSignal(ticker, float(trigger), ts.date()))
    return out


# -- Ladder catalogue ---------------------------------------------------------

LADDERS: list[Ladder] = [
    Ladder("none (full trail)",
           rungs=[],
           trail_pct=-2.0, hard_stop_pct=-0.02),

    Ladder("half @ +2%",
           rungs=[(2.0, 0.50)],
           trail_pct=-2.0, hard_stop_pct=-0.02),

    Ladder("half @ +3%",
           rungs=[(3.0, 0.50)],
           trail_pct=-2.0, hard_stop_pct=-0.02),

    Ladder("half @ +5%",
           rungs=[(5.0, 0.50)],
           trail_pct=-2.0, hard_stop_pct=-0.02),

    Ladder("third @ 2/4/6",
           rungs=[(2.0, 0.33), (4.0, 0.33), (6.0, 0.34)],
           trail_pct=-2.0, hard_stop_pct=-0.02),

    Ladder("quarter @ 2/4/6/8",
           rungs=[(2.0, 0.25), (4.0, 0.25), (6.0, 0.25), (8.0, 0.25)],
           trail_pct=-2.0, hard_stop_pct=-0.02),

    Ladder("half @ +3, half-of-rest @ +6",
           rungs=[(3.0, 0.50), (6.0, 0.25)],
           trail_pct=-2.0, hard_stop_pct=-0.02),

    Ladder("third @ 3, third @ 6, runner",
           rungs=[(3.0, 0.33), (6.0, 0.33)],
           trail_pct=-2.0, hard_stop_pct=-0.02),
]


# -- Driver -------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--jsonl", type=Path, default=Path("logs/history.jsonl"))
    ap.add_argument("--interval", choices=["1m", "5m"], default="5m")
    args = ap.parse_args()

    triggers = load_triggers(args.jsonl)
    # Dedupe on (ticker, trigger, date)
    seen: set[tuple[str, float, date]] = set()
    trigs: list[TriggerSignal] = []
    for t in triggers:
        key = (t.ticker, t.trigger, t.trade_date)
        if key not in seen:
            seen.add(key)
            trigs.append(t)

    today = date.today()
    max_age = 60 if args.interval == "5m" else 7
    in_window = [t for t in trigs if (today - t.trade_date).days <= max_age]

    print(f"\n  TRIGGER signals (deduped): {len(trigs)}    in {args.interval} window: {len(in_window)}\n")

    # Walk each trigger, compute MFE and run all ladders against it.
    per_trigger: list[dict] = []
    mfes: list[float] = []
    for t in in_window:
        try:
            df = fetch_intraday(t.ticker, t.trade_date, args.interval)
        except SystemExit:
            continue
        bars = to_bars(regular_session(df))
        if not bars:
            continue
        entry = find_entry(bars, t.trigger)
        if entry is None:
            continue
        idx, _ = entry
        mfe = mfe_pct(bars, idx, t.trigger)
        mfes.append(mfe)
        per_trigger.append({
            "ticker": t.ticker, "date": t.trade_date.isoformat(),
            "trigger": t.trigger, "mfe_pct": mfe,
            "results": {l.name: simulate(bars, idx, t.trigger, l) for l in LADDERS},
        })

    n = len(per_trigger)
    if n == 0:
        sys.exit("No tradable signals.")

    print(f"  signals simulated: {n}\n")

    # -- MFE distribution -----------------------------------------------------
    print("  MFE DISTRIBUTION (max favorable excursion before trade ended)")
    print("  -------------------------------------------------------------")
    buckets = [0, 1, 2, 3, 5, 8, 10, 15, 20, 100]
    counts: Counter[str] = Counter()
    for m in mfes:
        for i in range(len(buckets) - 1):
            if buckets[i] <= m < buckets[i + 1]:
                label = f">={buckets[i]:>2}% & <{buckets[i+1]:>2}%"
                counts[label] += 1
                break

    print(f"  reached at least N% intraday before reversing/closing:")
    for level in [1, 2, 3, 5, 8, 10]:
        hit = sum(1 for m in mfes if m >= level)
        pct = hit / n * 100
        bar = "#" * int(pct / 2)
        print(f"    >= +{level}%   {hit:>2}/{n} ({pct:>4.1f}%)  {bar}")
    print()
    print(f"    median MFE: {statistics.median(mfes):>+5.2f}%")
    print(f"    mean   MFE: {statistics.mean(mfes):>+5.2f}%")
    print(f"    max    MFE: {max(mfes):>+5.2f}%")
    print(f"    min    MFE: {min(mfes):>+5.2f}%")
    print()

    # -- Ladder summary -------------------------------------------------------
    print("  LADDER PERFORMANCE")
    print("  ------------------")
    print(f"  {'ladder':<32}  {'avg':>7}  {'median':>7}  {'win%':>6}  "
          f"{'best':>7}  {'worst':>7}  {'sum':>8}")
    print(f"  {'-'*32}  {'-'*7}  {'-'*7}  {'-'*6}  {'-'*7}  {'-'*7}  {'-'*8}")
    summaries = []
    for ladder in LADDERS:
        pnls = [pt["results"][ladder.name]["pnl_pct"] for pt in per_trigger]
        wins = [p for p in pnls if p > 0]
        avg = statistics.mean(pnls)
        med = statistics.median(pnls)
        win_rate = len(wins) / len(pnls) * 100
        best = max(pnls); worst = min(pnls); total = sum(pnls)
        summaries.append((ladder.name, avg, med, win_rate, best, worst, total))
        print(f"  {ladder.name:<32}  {avg:>+6.2f}%  {med:>+6.2f}%  "
              f"{win_rate:>5.1f}%  {best:>+6.2f}%  {worst:>+6.2f}%  {total:>+7.2f}%")

    # -- Ranking
    print()
    print("  RANKING (by total simulated P/L over all signals):")
    for name, avg, _, win_rate, _, _, total in sorted(summaries, key=lambda r: -r[6]):
        print(f"    {name:<32}  total {total:>+7.2f}%   avg {avg:>+5.2f}%   wins {win_rate:>4.1f}%")
    print()


if __name__ == "__main__":
    main()
