"""
Single-trigger intraday backtest.

For a given (ticker, trigger_price, date), pull intraday bars and simulate
how the trade would have performed under several stop/target strategies.

Useful to *retrospectively* judge a signal you already have, and to find
the stop-loss policy that fits your risk tolerance for this kind of setup.

Run:
    python -m bot.backtest AXTI 96.32 --date 2026-05-04
    python -m bot.backtest SOUN 9.64
    python -m bot.backtest LWLG 17.28 --interval 1m

Notes
-----
- Trigger is interpreted as a LONG breakout entry: the trade enters on the
  first bar where High >= trigger_price, at the trigger_price (assuming a
  resting stop-buy was placed there). This is the same model the channel
  appears to use.
- Strategies are evaluated bar-by-bar. Within a single bar we make a
  conservative assumption: if BOTH a stop and a target would have hit,
  the *stop* is honored first (worst-case for the trader).
- All percentages are intraday only -- positions exit at the regular-session
  close even if no other rule fires.
- Times in the output are US/Eastern (the exchange clock), since that's
  what intraday traders read price action against.
"""

from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterable

import pandas as pd
import yfinance as yf


@dataclass
class Bar:
    ts: pd.Timestamp
    open: float
    high: float
    low: float
    close: float


@dataclass
class StrategyResult:
    name: str
    detail: str
    entry: float
    exit: float
    exit_reason: str
    exit_ts: pd.Timestamp
    pnl_pct: float


# -- Data fetch ---------------------------------------------------------------

def fetch_intraday(ticker: str, target_date: date, interval: str) -> pd.DataFrame:
    """
    Pull intraday OHLCV for `target_date`. yfinance returns extended hours by
    default for short intervals; we keep extended hours so a 04:00-09:30 ET
    pre-market trigger still has data, but we only simulate inside the regular
    session 09:30-16:00 ET to mirror typical bot behavior.
    """
    days_back = (date.today() - target_date).days + 7
    period = f"{max(days_back, 5)}d"
    df = yf.Ticker(ticker).history(period=period, interval=interval, prepost=True)
    if df.empty:
        sys.exit(f"No intraday data for {ticker} (interval={interval}).")
    df = df.tz_convert("US/Eastern")
    day = df[df.index.date == target_date]
    if day.empty:
        sys.exit(f"No bars for {ticker} on {target_date}. Available range: "
                 f"{df.index.min()} → {df.index.max()}")
    return day


def regular_session(df: pd.DataFrame) -> pd.DataFrame:
    """09:30 -> 16:00 ET regular session only."""
    return df.between_time("09:30", "15:59")


def to_bars(df: pd.DataFrame) -> list[Bar]:
    return [
        Bar(ts=ts, open=float(r.Open), high=float(r.High), low=float(r.Low), close=float(r.Close))
        for ts, r in df.iterrows()
    ]


# -- Entry & MAE/MFE ----------------------------------------------------------

def find_entry(bars: list[Bar], trigger: float) -> tuple[int, Bar] | None:
    """First bar whose High crosses the trigger. Entry price = trigger."""
    for i, b in enumerate(bars):
        if b.high >= trigger:
            return i, b
    return None


def compute_excursions(bars: list[Bar], entry_idx: int, entry_px: float) -> dict:
    after = bars[entry_idx:]
    mfe_px = max(b.high for b in after)
    mae_px = min(b.low for b in after)
    mfe_ts = next(b.ts for b in after if b.high == mfe_px)
    mae_ts = next(b.ts for b in after if b.low == mae_px)
    return {
        "mfe_px": mfe_px,
        "mae_px": mae_px,
        "mfe_pct": (mfe_px / entry_px - 1) * 100,
        "mae_pct": (mae_px / entry_px - 1) * 100,
        "mfe_ts": mfe_ts,
        "mae_ts": mae_ts,
        "close_px": after[-1].close,
        "close_pct": (after[-1].close / entry_px - 1) * 100,
        "close_ts": after[-1].ts,
    }


# -- Strategy simulators ------------------------------------------------------

def _result(name: str, detail: str, entry: float, exit_px: float,
            exit_reason: str, exit_ts: pd.Timestamp) -> StrategyResult:
    return StrategyResult(
        name=name, detail=detail,
        entry=entry, exit=exit_px,
        exit_reason=exit_reason, exit_ts=exit_ts,
        pnl_pct=(exit_px / entry - 1) * 100,
    )


def sim_no_stop_hold_close(bars: list[Bar], entry_idx: int, entry_px: float) -> StrategyResult:
    last = bars[-1]
    return _result(
        "hold-to-close",
        "no stop | no target | exit at session close (the channel's apparent rule)",
        entry_px, last.close, "session close", last.ts,
    )


def sim_fixed_stop(bars: list[Bar], entry_idx: int, entry_px: float, stop_pct: float) -> StrategyResult:
    stop_px = entry_px * (1 + stop_pct / 100)
    for b in bars[entry_idx:]:
        if b.low <= stop_px:
            # Pessimistic fill: at the stop price (no slippage). Real fills
            # often slip lower on volatile dumps, but that's broker-dependent.
            return _result(
                f"stop {stop_pct:+.1f}%",
                f"hard stop at {stop_px:.2f} | target none | session close fallback",
                entry_px, stop_px, f"stop hit @ {stop_px:.2f}", b.ts,
            )
    last = bars[-1]
    return _result(
        f"stop {stop_pct:+.1f}%",
        f"hard stop at {stop_px:.2f} | target none | session close fallback",
        entry_px, last.close, "session close (stop never hit)", last.ts,
    )


def sim_bracket(bars: list[Bar], entry_idx: int, entry_px: float,
                stop_pct: float, target_pct: float) -> StrategyResult:
    stop_px = entry_px * (1 + stop_pct / 100)
    target_px = entry_px * (1 + target_pct / 100)
    for b in bars[entry_idx:]:
        # Conservative tie-break: stop fires before target within the same bar.
        if b.low <= stop_px:
            return _result(
                f"bracket {stop_pct:+.1f}/{target_pct:+.1f}",
                f"stop {stop_px:.2f} | target {target_px:.2f}",
                entry_px, stop_px, f"stop hit @ {stop_px:.2f}", b.ts,
            )
        if b.high >= target_px:
            return _result(
                f"bracket {stop_pct:+.1f}/{target_pct:+.1f}",
                f"stop {stop_px:.2f} | target {target_px:.2f}",
                entry_px, target_px, f"target hit @ {target_px:.2f}", b.ts,
            )
    last = bars[-1]
    return _result(
        f"bracket {stop_pct:+.1f}/{target_pct:+.1f}",
        f"stop {stop_px:.2f} | target {target_px:.2f}",
        entry_px, last.close, "session close (neither hit)", last.ts,
    )


def sim_trailing_stop(bars: list[Bar], entry_idx: int, entry_px: float,
                       trail_pct: float) -> StrategyResult:
    """
    Trail by `trail_pct` (negative number) from the running peak high since entry.
    Once a peak is set, exit when low <= peak * (1 + trail_pct/100).
    """
    peak = entry_px
    for b in bars[entry_idx:]:
        peak = max(peak, b.high)
        stop_px = peak * (1 + trail_pct / 100)
        if b.low <= stop_px:
            return _result(
                f"trail {trail_pct:+.1f}%",
                f"trailing stop | {abs(trail_pct):.1f}% behind running peak",
                entry_px, stop_px, f"trail stop @ {stop_px:.2f} (peak {peak:.2f})", b.ts,
            )
    last = bars[-1]
    return _result(
        f"trail {trail_pct:+.1f}%",
        f"trailing stop | {abs(trail_pct):.1f}% behind running peak",
        entry_px, last.close, f"session close (peak {peak:.2f})", last.ts,
    )


# -- Output formatting --------------------------------------------------------

def fmt_money(x: float) -> str:
    return f"{x:>7.2f}"


def fmt_pct(x: float) -> str:
    sign = "+" if x >= 0 else ""
    return f"{sign}{x:>6.2f}%"


def print_price_arc(bars: list[Bar], trigger: float, entry_idx: int) -> None:
    """ASCII sparkline of price action with the trigger line + entry mark."""
    closes = [b.close for b in bars]
    if not closes:
        return
    lo, hi = min(closes), max(closes)
    height = 12
    width = min(len(bars), 96)
    if width == 0:
        return

    # Down-sample to width buckets if needed
    step = max(1, math.ceil(len(bars) / width))
    buckets = [bars[i:i + step] for i in range(0, len(bars), step)]
    bucket_close = [b[-1].close for b in buckets]

    def row_for_price(px: float) -> int:
        if hi == lo:
            return height // 2
        return int((1 - (px - lo) / (hi - lo)) * (height - 1))

    grid = [[" "] * len(buckets) for _ in range(height)]
    trig_row = row_for_price(trigger) if lo <= trigger <= hi else None

    for col, c in enumerate(bucket_close):
        r = row_for_price(c)
        grid[r][col] = "*"

    if trig_row is not None:
        for col in range(len(buckets)):
            if grid[trig_row][col] == " ":
                grid[trig_row][col] = "-"

    entry_col = entry_idx // step if step else None
    if entry_col is not None and 0 <= entry_col < len(buckets):
        for r in range(height):
            if grid[r][entry_col] == " ":
                grid[r][entry_col] = "|"
            elif grid[r][entry_col] == "-":
                grid[r][entry_col] = "+"

    print(f"\n  price arc  |  trigger line --- | entry | | top {hi:.2f}, bottom {lo:.2f}")
    for r, row in enumerate(grid):
        if r == 0:
            edge = f" {hi:>7.2f} |"
        elif r == height - 1:
            edge = f" {lo:>7.2f} |"
        else:
            edge = "         |"
        print(edge + "".join(row))
    print("         +" + "-" * len(buckets))


# -- Main ---------------------------------------------------------------------

def run(ticker: str, trigger: float, target_date: date, interval: str) -> int:
    df = fetch_intraday(ticker, target_date, interval)
    df_reg = regular_session(df)
    bars = to_bars(df_reg)
    if not bars:
        sys.exit(f"No regular-session bars for {ticker} on {target_date}.")

    print()
    print(f"  +" + "=" * 74 + "+")
    print(f"  |  RETRO BACKTEST  |  {ticker:>6}  trigger {trigger:>7.2f}   {target_date}            |")
    print(f"  +" + "=" * 74 + "+")
    print(f"  bars: {len(bars)}   interval: {interval}   session: 09:30-16:00 ET")
    print(f"  day open:  {bars[0].open:.2f} @ {bars[0].ts.strftime('%H:%M')} ET")
    print(f"  day high:  {max(b.high for b in bars):.2f}")
    print(f"  day low:   {min(b.low for b in bars):.2f}")
    print(f"  day close: {bars[-1].close:.2f} @ {bars[-1].ts.strftime('%H:%M')} ET")

    entry = find_entry(bars, trigger)
    if entry is None:
        print(f"\n  TRIGGER {trigger:.2f} NEVER HIT today. Day high was "
              f"{max(b.high for b in bars):.2f}. No trade taken.")
        return 0
    entry_idx, entry_bar = entry
    entry_px = trigger

    print(f"\n  ENTRY    {entry_px:.2f} @ {entry_bar.ts.strftime('%H:%M')} ET  "
          f"(bar {entry_idx + 1}/{len(bars)})")

    ex = compute_excursions(bars, entry_idx, entry_px)
    print(f"\n  EXCURSIONS POST-ENTRY")
    print(f"    max favorable (MFE): {ex['mfe_px']:>7.2f}  ({fmt_pct(ex['mfe_pct'])})  "
          f"@ {ex['mfe_ts'].strftime('%H:%M')} ET")
    print(f"    max adverse   (MAE): {ex['mae_px']:>7.2f}  ({fmt_pct(ex['mae_pct'])})  "
          f"@ {ex['mae_ts'].strftime('%H:%M')} ET")
    print(f"    session close:       {ex['close_px']:>7.2f}  ({fmt_pct(ex['close_pct'])})  "
          f"@ {ex['close_ts'].strftime('%H:%M')} ET")

    print_price_arc(bars, trigger, entry_idx)

    # -- Strategy comparison
    strategies: list[StrategyResult] = []
    strategies.append(sim_no_stop_hold_close(bars, entry_idx, entry_px))
    for stop in (-1.0, -2.0, -3.0, -5.0):
        strategies.append(sim_fixed_stop(bars, entry_idx, entry_px, stop))
    for stop, target in ((-2.0, 5.0), (-2.0, 10.0), (-3.0, 8.0)):
        strategies.append(sim_bracket(bars, entry_idx, entry_px, stop, target))
    for trail in (-2.0, -3.0, -5.0):
        strategies.append(sim_trailing_stop(bars, entry_idx, entry_px, trail))

    print(f"\n  STRATEGY OUTCOMES")
    print(f"  {'strategy':<22}  {'p/l':>9}  {'exit':>9}  {'@ time':>8}  reason")
    print(f"  {'-'*22}  {'-'*9}  {'-'*9}  {'-'*8}  {'-'*40}")
    for s in strategies:
        print(f"  {s.name:<22}  {fmt_pct(s.pnl_pct):>9}  "
              f"{fmt_money(s.exit):>9}  {s.exit_ts.strftime('%H:%M'):>8}  {s.exit_reason}")

    # Quick top-3 ranking
    top = sorted(strategies, key=lambda r: r.pnl_pct, reverse=True)[:3]
    bottom = sorted(strategies, key=lambda r: r.pnl_pct)[:1]
    print(f"\n  best:   {top[0].name} | {fmt_pct(top[0].pnl_pct)}")
    print(f"  worst:  {bottom[0].name} | {fmt_pct(bottom[0].pnl_pct)}")
    print()
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description="Single-trigger intraday backtest.")
    ap.add_argument("ticker", help="Stock ticker, e.g. AXTI")
    ap.add_argument("trigger", type=float, help="Trigger price for breakout entry, e.g. 96.32")
    ap.add_argument("--date", type=str, default=None,
                    help="Trade date YYYY-MM-DD (default: today)")
    ap.add_argument("--interval", choices=["1m", "5m", "15m"], default="5m",
                    help="Bar interval (default 5m). Use 1m for full granularity, but yfinance "
                         "only serves 1m for the past 7 days.")
    args = ap.parse_args()

    target_date = (
        datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else date.today()
    )
    run(args.ticker.upper(), args.trigger, target_date, args.interval)


if __name__ == "__main__":
    main()
