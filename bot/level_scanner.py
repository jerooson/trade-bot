"""
Mechanical "prior-high" level scanner: can Heat's yellow lines be generated?

Reverse-engineered from the 21 yellow lines in the reviewed Heat charts
(docs/replay-findings-2026-09.md): every one sat on a prior daily high
(max error 0.24 %), 15 of 21 were pivot highs (higher than the 3 bars on
each side), the pivot was 4-65 sessions old (median 20), and the prior
close was 1-8 % below the line (median 3.4 %).

For every (symbol, session) the scanner emits at most one level: the nearest
pivot high above the prior close that is at most ``max_dist_pct`` away,
formed within the last ``lookback`` sessions, and not closed above since it
formed.  A level is emitted once per ``cooldown`` sessions so the 10-session
GTC replay window does not count the same line several times.

A control group ("random") places a level the same distance band above the
prior close at a random offset, on the same symbol-days, so the replay can
tell "prior highs carry information" from "buying strength above the close
works".

    python -m bot.level_scanner --out data/scan_dataset.jsonl \
        --start 2026-07-16 --end 2026-09-11 [--symbols SPY QQQ ...]
    python -m bot.level_scanner --recall data/heat_levels_reviewed.jsonl ...
"""

from __future__ import annotations

import argparse
import json
import random
from dataclasses import dataclass
from datetime import date, datetime, time as dtime
from pathlib import Path
from typing import Iterable

from bot.daily_data import DAILY_DIR, DailyBar, load_daily
from bot.market_data import ET, is_trading_day
from bot.signal_dataset import SignalRow, _route, write_dataset

LOOKBACK = 65          # sessions a pivot stays a candidate
PIVOT_WING = 3         # bars on each side that must be lower
MIN_DIST_PCT = 0.0
MAX_DIST_PCT = 10.0
COOLDOWN = 10          # sessions before the same level is emitted again
POST_TIME = dtime(9, 25)


@dataclass(frozen=True)
class Level:
    symbol: str
    level: float
    pivot_day: date
    age: int            # sessions since the pivot
    dist_pct: float     # (level - prior close) / prior close


def pivot_highs(bars: list[DailyBar], wing: int = PIVOT_WING) -> list[tuple[int, float]]:
    """(index, high) of every bar strictly higher than ``wing`` bars on each side."""
    out = []
    for i in range(wing, len(bars) - wing):
        h = bars[i].high
        if all(h > bars[j].high for j in range(i - wing, i + wing + 1) if j != i):
            out.append((i, h))
    return out


def candidate_level(bars: list[DailyBar], session_idx: int, *, lookback: int = LOOKBACK,
                    max_dist_pct: float = MAX_DIST_PCT, min_dist_pct: float = MIN_DIST_PCT) -> Level | None:
    """Nearest unbroken pivot high above the close before ``session_idx``."""
    if session_idx < lookback + PIVOT_WING + 1:
        return None
    prior = bars[:session_idx]
    close = prior[-1].close
    if close <= 0:
        return None
    best: Level | None = None
    for i, h in pivot_highs(prior[-(lookback + PIVOT_WING):]):
        idx = len(prior) - (lookback + PIVOT_WING) + i
        dist = (h - close) / close * 100
        if dist < min_dist_pct or dist > max_dist_pct:
            continue
        # unbroken: no close above the level after the pivot day
        if any(b.close > h for b in prior[idx + 1:]):
            continue
        age = len(prior) - 1 - idx
        if best is None or dist < best.dist_pct:
            best = Level("", h, prior[idx].day, age, dist)
    return best


def scan_symbol(symbol: str, bars: list[DailyBar], start: date, end: date, *,
                cooldown: int = COOLDOWN, seed: int = 0, **kw) -> tuple[list[SignalRow], list[SignalRow]]:
    """Emit scanner rows and matching random-control rows for one symbol."""
    rows: list[SignalRow] = []
    controls: list[SignalRow] = []
    last_emit: dict[float, int] = {}
    rng = random.Random(f"{symbol}:{seed}")
    for idx, bar in enumerate(bars):
        if bar.day < start or bar.day > end or not is_trading_day(bar.day):
            continue
        lvl = candidate_level(bars, idx, **kw)
        if lvl is None:
            continue
        key = round(lvl.level, 2)
        if idx - last_emit.get(key, -10**6) < cooldown:
            continue
        last_emit[key] = idx
        posted = datetime.combine(bar.day, POST_TIME, tzinfo=ET)
        execution, leverage = _route(symbol, "long")
        rows.append(SignalRow(
            id=f"scan:{symbol}:{bar.day.isoformat()}:{key}", source="scan", ticker=symbol,
            trigger=key, operator="above", direction="long", target=None,
            posted_at=posted.isoformat(), session=bar.day.isoformat(),
            execution=execution, leverage=leverage,
            setup=f"prior high {lvl.pivot_day.isoformat()} ({lvl.age}d ago, +{lvl.dist_pct:.1f}%)",
            notes=[f"pivot_age:{lvl.age}", f"dist_pct:{lvl.dist_pct:.2f}"],
        ))
        close = bars[idx - 1].close
        rnd = round(close * (1 + rng.uniform(max(0.5, kw.get("min_dist_pct", MIN_DIST_PCT)),
                                             kw.get("max_dist_pct", MAX_DIST_PCT)) / 100), 2)
        controls.append(SignalRow(
            id=f"random:{symbol}:{bar.day.isoformat()}:{rnd}", source="random", ticker=symbol,
            trigger=rnd, operator="above", direction="long", target=None,
            posted_at=posted.isoformat(), session=bar.day.isoformat(),
            execution=execution, leverage=leverage,
            setup=f"random level +{(rnd - close) / close * 100:.1f}% above prior close",
            notes=[f"dist_pct:{(rnd - close) / close * 100:.2f}"],
        ))
    return rows, controls


def scan(symbols: Iterable[str], start: date, end: date, root: Path | None = None,
         **kw) -> tuple[list[SignalRow], list[SignalRow]]:
    rows: list[SignalRow] = []
    controls: list[SignalRow] = []
    for symbol in dict.fromkeys(s.upper() for s in symbols):
        bars = load_daily(symbol, root)
        if not bars:
            continue
        r, c = scan_symbol(symbol, bars, start, end, **kw)
        rows += r
        controls += c
    return rows, controls


def recall(reviewed_path: Path, dataset_path: Path, root: Path | None = None, tol_pct: float = 0.3) -> dict:
    """How many of Heat's yellow lines the scanner reproduces on the same day."""
    from bot.signal_dataset import read_dataset
    reviewed = [json.loads(l) for l in reviewed_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    sessions = {r.id.split(":", 1)[1]: r.session for r in read_dataset(dataset_path)}
    hits, misses, total = [], [], 0
    for r in reviewed:
        if r.get("kind") != "yellow_line" or not r.get("level"):
            continue
        total += 1
        sess = sessions.get(r["id"])
        bars = load_daily(r["ticker"], root) or []
        idx = next((i for i, b in enumerate(bars) if sess and b.day >= date.fromisoformat(sess)), None)
        found = None
        if idx is not None:
            # any candidate (not just the nearest) within tolerance
            prior = bars[:idx]
            close = prior[-1].close
            for i, h in pivot_highs(prior[-(LOOKBACK + PIVOT_WING):]):
                if abs(h - r["level"]) / r["level"] * 100 <= tol_pct:
                    found = (h, (h - close) / close * 100)
            nearest = candidate_level(bars, idx)
        (hits if found else misses).append(
            {"ticker": r["ticker"], "level": r["level"], "session": sess,
             "found": found, "nearest": (nearest.level, round(nearest.dist_pct, 2)) if idx is not None and nearest else None})
    return {"total": total, "hits": len(hits), "misses": misses, "hit_rows": hits}


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="Prior-high level scanner")
    p.add_argument("--out", type=Path, default=Path("data/scan_dataset.jsonl"))
    p.add_argument("--start", type=date.fromisoformat, default=date(2026, 7, 16))
    p.add_argument("--end", type=date.fromisoformat, default=date(2026, 9, 11))
    p.add_argument("--symbols", nargs="*", help="default: every symbol with cached daily bars")
    p.add_argument("--max-dist", type=float, default=MAX_DIST_PCT)
    p.add_argument("--lookback", type=int, default=LOOKBACK)
    p.add_argument("--recall", type=Path, help="reviewed Heat levels to check recall against")
    p.add_argument("--dataset", type=Path, default=Path("data/signals_dataset.jsonl"))
    args = p.parse_args(argv)
    if args.recall:
        rep = recall(args.recall, args.dataset)
        print(f"yellow lines: {rep['total']}  reproduced by scanner: {rep['hits']}")
        for m in rep["misses"]:
            print("  miss:", m)
        return
    symbols = args.symbols or sorted(p.stem for p in DAILY_DIR.glob("*.csv"))
    rows, controls = scan(symbols, args.start, args.end, lookback=args.lookback, max_dist_pct=args.max_dist)
    write_dataset(rows + controls, args.out)
    print(f"wrote {len(rows)} scan rows + {len(controls)} random controls over {len(symbols)} symbols -> {args.out}")


if __name__ == "__main__":
    main()
