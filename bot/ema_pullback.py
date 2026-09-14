"""
Research: does "buy the pullback to the 20 EMA in an uptrend" work?

Two flavours on cached data, each against a random control on the same
symbol-days so "the tape was up" is separated from "the pullback matters":

  4H   two regular-session bars per day (09:30-13:30, 13:30-16:00) built from
       cached 1-minute bars; EMA20 on those bars (~10 sessions).
  D    daily bars; EMA10 (the same horizon as 4H EMA20) and EMA20.

Setup (long only):
  trend    daily close > SMA21 and SMA21 higher than 5 sessions ago
  pullback the previous ``clear`` bars closed above the EMA, this bar's low
           touches or dips below the EMA, and the bar closes back above it
  entry    this bar's close
  stop     this bar's low (the pullback low)
  exit     first bar that closes below the EMA, or ``hold`` bars, or the stop

Control: on every symbol-day that passes the trend filter, a random bar with
the same stop / exit rules but no pullback condition.

    python -m bot.ema_pullback [--start 2026-07-16 --end 2026-09-11]
"""

from __future__ import annotations

import argparse
import random
import statistics as st
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Iterable

from bot.daily_data import DAILY_DIR, DailyBar, load_daily
from bot.market_data import BARS_DIR, ET, Bar, is_trading_day, load_bars
from bot.signal_dataset import read_dataset


@dataclass(frozen=True)
class OHLC:
    ts: datetime
    open: float
    high: float
    low: float
    close: float


@dataclass
class Trade:
    symbol: str
    kind: str          # pullback | random
    entry_ts: datetime
    entry: float
    exit_ts: datetime
    exit: float
    reason: str
    bars_held: int

    @property
    def pnl_pct(self) -> float:
        return (self.exit / self.entry - 1) * 100


# ---------------------------------------------------------------------------
# Bars
# ---------------------------------------------------------------------------

def four_hour_bars(symbol: str, sessions: Iterable[date], root: Path | None = None) -> list[OHLC]:
    """Two bars per session from minute bars; sessions without bars are skipped."""
    out: list[OHLC] = []
    for day in sessions:
        bars = load_bars(symbol, day, root)
        if not bars:
            continue
        am = [b for b in bars if b.ts.time() < dtime(13, 30)]
        pm = [b for b in bars if b.ts.time() >= dtime(13, 30)]
        for chunk in (am, pm):
            if len(chunk) < 30:
                continue
            out.append(OHLC(chunk[0].ts, chunk[0].open, max(b.high for b in chunk), min(b.low for b in chunk), chunk[-1].close))
    return out


def daily_ohlc(bars: list[DailyBar]) -> list[OHLC]:
    return [OHLC(datetime.combine(b.day, dtime(16, 0), tzinfo=ET), b.open, b.high, b.low, b.close) for b in bars]


def ema(values: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if len(values) < n:
        return out
    k = 2 / (n + 1)
    e = sum(values[:n]) / n
    out[n - 1] = e
    for i in range(n, len(values)):
        e = values[i] * k + e * (1 - k)
        out[i] = e
    return out


def sma(values: list[float], n: int) -> list[float | None]:
    return [sum(values[i - n + 1:i + 1]) / n if i >= n - 1 else None for i in range(len(values))]


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------

def uptrend_days(daily: list[DailyBar]) -> set[date]:
    closes = [b.close for b in daily]
    s21 = sma(closes, 21)
    ok = set()
    for i, b in enumerate(daily):
        if i >= 26 and s21[i] and s21[i - 5] and closes[i] > s21[i] and s21[i] > s21[i - 5]:
            ok.add(b.day)
    return ok


def simulate_exit(bars: list[OHLC], e: list[float | None], i: int, hold: int,
                  stop: float | None = None) -> tuple[int, float, str]:
    """From entry bar i (entered at its close): stop at bar i low (or ``stop``), exit on close < EMA or after hold bars."""
    stop = bars[i].low if stop is None else stop
    for j in range(i + 1, min(len(bars), i + 1 + hold)):
        b = bars[j]
        if b.low <= stop:
            px = b.open if b.open <= stop else stop
            return j, px, "stop"
        if e[j] is not None and b.close < e[j]:
            return j, b.close, "ema_break"
    j = min(len(bars) - 1, i + hold)
    return j, bars[j].close, "hold"


def scan(symbol: str, bars: list[OHLC], ema_n: int, trend_days: set[date], *, clear: int = 3, hold: int = 10,
         cooldown: int = 5, rng: random.Random) -> tuple[list[Trade], list[Trade]]:
    closes = [b.close for b in bars]
    e = ema(closes, ema_n)
    trades: list[Trade] = []
    controls: list[Trade] = []
    last = -10**6
    for i in range(ema_n + clear, len(bars) - 1):
        if bars[i].ts.date() not in trend_days or e[i] is None:
            continue
        prior_clear = all(bars[k].close > (e[k] or 0) and bars[k].low > (e[k] or 0) for k in range(i - clear, i))
        touched = bars[i].low <= e[i] <= bars[i].high * 1.002
        reclaimed = bars[i].close > e[i]
        if prior_clear and touched and reclaimed and i - last >= cooldown:
            last = i
            j, px, reason = simulate_exit(bars, e, i, hold)
            trades.append(Trade(symbol, "pullback", bars[i].ts, bars[i].close, bars[j].ts, px, reason, j - i))
            # control: a random trend-day bar of this symbol within +-15 bars, no
            # pullback condition, with the SAME stop distance (in %) as the
            # pullback trade so the comparison is about timing, not stop width.
            stop_dist = 1 - bars[i].low / bars[i].close
            lo, hi = max(ema_n + clear, i - 15), min(len(bars) - 2, i + 15)
            cands = [k for k in range(lo, hi + 1) if bars[k].ts.date() in trend_days and e[k] is not None and k != i]
            if cands:
                k = rng.choice(cands)
                j, px, reason = simulate_exit(bars, e, k, hold, stop=bars[k].close * (1 - stop_dist))
                controls.append(Trade(symbol, "random", bars[k].ts, bars[k].close, bars[j].ts, px, reason, j - k))
    return trades, controls


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def summarize(trades: list[Trade]) -> str:
    if not trades:
        return "n=0"
    v = [t.pnl_pct for t in trades]
    sd = st.pstdev(v) or 1e-9
    wins = [x for x in v if x > 0]
    losses = [x for x in v if x < 0]
    pf = sum(wins) / abs(sum(losses)) if losses else float("inf")
    se = sd / len(v) ** 0.5
    reasons = {}
    for t in trades:
        reasons[t.reason] = reasons.get(t.reason, 0) + 1
    return (f"n={len(v):<4} avg={st.mean(v):+.2f}% ±{se:.2f} med={st.median(v):+.2f}% win={len(wins) / len(v) * 100:3.0f}% "
            f"PF={pf:.2f} Sh/tr={st.mean(v) / sd:.2f} held={st.mean(t.bars_held for t in trades):.1f} bars exits={reasons}")


def heat_universe(dataset: Path) -> set[str]:
    return {r.ticker for r in read_dataset(dataset) if r.source == "heat"}


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="EMA pullback research")
    p.add_argument("--start", type=date.fromisoformat, default=date(2026, 7, 16))
    p.add_argument("--end", type=date.fromisoformat, default=date(2026, 9, 11))
    p.add_argument("--hold", type=int, default=10, help="max bars held")
    p.add_argument("--clear", type=int, default=3, help="bars that must sit above the EMA before the touch")
    p.add_argument("--dataset", type=Path, default=Path("data/signals_dataset.jsonl"))
    args = p.parse_args(argv)
    rng = random.Random(0)
    heat = heat_universe(args.dataset) if args.dataset.exists() else set()
    symbols = sorted(x.stem for x in DAILY_DIR.glob("*.csv"))
    sessions = [args.start + timedelta(days=i) for i in range((args.end - args.start).days + 1)]
    sessions = [d for d in sessions if d.weekday() < 5 and is_trading_day(d)]

    results: dict[str, dict[str, list[Trade]]] = {}
    for label, builder, ema_n in (("4H EMA20", "4h", 20), ("D EMA10", "d", 10), ("D EMA20", "d", 20)):
        results[label] = {"pullback": [], "random": []}
        for sym in symbols:
            daily = load_daily(sym) or []
            if len(daily) < 60:
                continue
            trend = uptrend_days(daily)
            if builder == "4h":
                # need continuous minute coverage; warm the EMA with ~12 sessions before start
                warm = [d for d in (args.start - timedelta(days=k) for k in range(1, 40)) if d.weekday() < 5 and is_trading_day(d)]
                bars = four_hour_bars(sym, sorted(warm) + sessions)
                if len(bars) < 2 * (ema_n + 8):
                    continue
            else:
                bars = daily_ohlc([b for b in daily if b.day <= args.end])
            t, c = scan(sym, bars, ema_n, trend, clear=args.clear, hold=args.hold, rng=rng)
            # keep only entries inside the window
            t = [x for x in t if args.start <= x.entry_ts.date() <= args.end]
            c = [x for x in c if args.start <= x.entry_ts.date() <= args.end]
            results[label]["pullback"] += t
            results[label]["random"] += c

    for label, groups in results.items():
        print(f"\n=== {label}  (window {args.start}..{args.end}, hold<={args.hold} bars, clear={args.clear})")
        for kind in ("pullback", "random"):
            allt = groups[kind]
            print(f"  {kind:<9} all   {summarize(allt)}")
            if heat:
                print(f"  {kind:<9} heat  {summarize([t for t in allt if t.symbol in heat])}")
                print(f"  {kind:<9} other {summarize([t for t in allt if t.symbol not in heat])}")


if __name__ == "__main__":
    main()
