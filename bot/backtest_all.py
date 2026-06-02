"""
Multi-signal backtest.

Replays every TRIGGER signal in a JSONL log against real intraday data,
under several stop/target strategies. Reports the distribution of outcomes
so you can pick a stop-loss policy with eyes open.

Run:
    python -m bot.backtest_all                                   # uses logs/history.jsonl
    python -m bot.backtest_all --jsonl logs/signals.jsonl
    python -m bot.backtest_all --interval 5m --tickers AXTI,SOUN
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import yfinance as yf

from bot.backtest import (
    fetch_intraday,
    find_entry,
    regular_session,
    sim_bracket,
    sim_fixed_stop,
    sim_no_stop_hold_close,
    sim_trailing_stop,
    to_bars,
)


@dataclass
class TriggerSignal:
    ticker: str
    side: str
    trigger: float
    target: float | None
    trade_date: date
    raw_ts: str


def load_triggers(jsonl_path: Path) -> list[TriggerSignal]:
    out: list[TriggerSignal] = []
    if not jsonl_path.exists():
        sys.exit(f"JSONL not found: {jsonl_path}")
    with jsonl_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            r = json.loads(line)
            if r.get("kind") != "TRIGGER":
                continue
            ticker = r.get("ticker")
            trigger = r.get("trigger")
            if not ticker or trigger is None:
                continue
            side = r.get("side") or "LONG"
            ts_iso = r.get("discord", {}).get("created_at") or r.get("received_at")
            if not ts_iso:
                continue
            ts = datetime.fromisoformat(ts_iso)
            out.append(
                TriggerSignal(
                    ticker=ticker, side=side,
                    trigger=float(trigger),
                    target=r.get("target"),
                    trade_date=ts.date(),
                    raw_ts=ts_iso,
                )
            )
    return out


# yfinance only serves 1-minute bars for the past ~7 days, and 5-minute for ~60.
# History is dispatch-aware: we ask for the bar set, and bail per-trade if the
# data is too old to retrieve.

def safe_fetch(ticker: str, target_date: date, interval: str) -> pd.DataFrame | None:
    try:
        df = fetch_intraday(ticker, target_date, interval)
        return df
    except SystemExit:
        return None
    except Exception as e:
        print(f"  [warn] fetch error for {ticker} {target_date}: {e}", file=sys.stderr)
        return None


def yfinance_max_age_days(interval: str) -> int:
    return {"1m": 7, "5m": 60, "15m": 60}[interval]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--jsonl", type=Path, default=Path("logs/history.jsonl"))
    ap.add_argument("--interval", choices=["1m", "5m", "15m"], default="5m")
    ap.add_argument("--tickers", type=str, default=None,
                    help="Optional CSV ticker filter, e.g. AXTI,SOUN")
    args = ap.parse_args()

    triggers = load_triggers(args.jsonl)
    if args.tickers:
        wanted = {t.strip().upper() for t in args.tickers.split(",")}
        triggers = [t for t in triggers if t.ticker in wanted]

    if not triggers:
        sys.exit("No TRIGGER signals to backtest.")

    today = date.today()
    max_age = yfinance_max_age_days(args.interval)

    # Dedup: same (ticker, trigger, date) only once.
    seen: set[tuple[str, float, date]] = set()
    deduped: list[TriggerSignal] = []
    for t in triggers:
        key = (t.ticker, t.trigger, t.trade_date)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(t)

    in_window = [t for t in deduped if (today - t.trade_date).days <= max_age]
    too_old = len(deduped) - len(in_window)

    print()
    print(f"  TRIGGER signals loaded     : {len(triggers)}")
    print(f"  unique (ticker,price,date) : {len(deduped)}")
    print(f"  within {args.interval} data window  : {len(in_window)}")
    if too_old:
        print(f"  too old for yfinance {args.interval} : {too_old} (skipped)")
    print()

    # Strategies to evaluate per signal -- same set as the single-trigger tool.
    strategy_runs: dict[str, list[float]] = defaultdict(list)
    skipped_no_data = 0
    skipped_no_entry = 0
    rows: list[dict] = []

    for t in in_window:
        df = safe_fetch(t.ticker, t.trade_date, args.interval)
        if df is None:
            skipped_no_data += 1
            continue
        bars = to_bars(regular_session(df))
        if not bars:
            skipped_no_data += 1
            continue
        entry = find_entry(bars, t.trigger)
        if entry is None:
            skipped_no_entry += 1
            continue
        idx, _ = entry

        results = {
            "hold-close": sim_no_stop_hold_close(bars, idx, t.trigger),
            "stop -1%":   sim_fixed_stop(bars, idx, t.trigger, -1.0),
            "stop -2%":   sim_fixed_stop(bars, idx, t.trigger, -2.0),
            "stop -3%":   sim_fixed_stop(bars, idx, t.trigger, -3.0),
            "stop -5%":   sim_fixed_stop(bars, idx, t.trigger, -5.0),
            "br -2/+5":   sim_bracket(bars, idx, t.trigger, -2.0, 5.0),
            "br -2/+10":  sim_bracket(bars, idx, t.trigger, -2.0, 10.0),
            "br -3/+8":   sim_bracket(bars, idx, t.trigger, -3.0, 8.0),
            "trail -2%":  sim_trailing_stop(bars, idx, t.trigger, -2.0),
            "trail -3%":  sim_trailing_stop(bars, idx, t.trigger, -3.0),
            "trail -5%":  sim_trailing_stop(bars, idx, t.trigger, -5.0),
        }
        for name, r in results.items():
            strategy_runs[name].append(r.pnl_pct)
        row = {"ticker": t.ticker, "date": t.trade_date.isoformat(),
               "trigger": t.trigger}
        row.update({name: r.pnl_pct for name, r in results.items()})
        rows.append(row)

    n = len(rows)
    if n == 0:
        sys.exit("No tradable signals after fetching market data. "
                 "Try a wider --interval or older signals.")

    print(f"  signals successfully simulated: {n}")
    if skipped_no_data:
        print(f"  skipped (no market data)      : {skipped_no_data}")
    if skipped_no_entry:
        print(f"  skipped (trigger never hit)   : {skipped_no_entry}")
    print()

    # -- Aggregate per strategy --
    print(f"  STRATEGY PERFORMANCE  ({n} signals)")
    print()
    hdr = (f"  {'strategy':<14}  {'avg':>7}  {'median':>7}  "
           f"{'win%':>6}  {'best':>7}  {'worst':>7}  {'sum':>8}  {'avg|win':>8}  {'avg|loss':>8}")
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    summary = []
    for name, pnls in strategy_runs.items():
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        win_rate = len(wins) / len(pnls) * 100 if pnls else 0
        avg = statistics.mean(pnls) if pnls else 0
        med = statistics.median(pnls) if pnls else 0
        avg_win = statistics.mean(wins) if wins else 0
        avg_loss = statistics.mean(losses) if losses else 0
        best = max(pnls) if pnls else 0
        worst = min(pnls) if pnls else 0
        total = sum(pnls)
        summary.append((name, avg, med, win_rate, best, worst, total, avg_win, avg_loss))
        print(f"  {name:<14}  {avg:>+6.2f}%  {med:>+6.2f}%  "
              f"{win_rate:>5.1f}%  {best:>+6.2f}%  {worst:>+6.2f}%  "
              f"{total:>+7.2f}%  {avg_win:>+7.2f}%  {avg_loss:>+7.2f}%")

    # -- Ranking by total simple-sum P/L --
    print()
    print("  RANKING (by sum of all simulated trades, all signals):")
    for name, avg, _, win_rate, _, _, total, _, _ in sorted(summary, key=lambda r: -r[6]):
        print(f"    {name:<14}  total {total:>+7.2f}%   avg {avg:>+5.2f}%   wins {win_rate:>4.1f}%")

    # Optional: dump per-trade details to CSV for later analysis
    out_csv = args.jsonl.parent / "backtest_trades.csv"
    pd.DataFrame(rows).to_csv(out_csv, index=False)
    print(f"\n  per-trade detail written to {out_csv}")
    print()


if __name__ == "__main__":
    main()
