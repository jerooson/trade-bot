"""
Intraday bar cache backed by IB Gateway historical data.

The IBKR paper gateway serves regular-hours 1-minute bars for US equities
going back more than a year without a market-data subscription, and it
returns a full session in well under a second.  Bars are cached as CSV under
``data/bars/<SYMBOL>/<YYYY-MM-DD>.csv`` so replays never hit the gateway
twice for the same session.

    python -m bot.market_data SPXL 2026-09-02 [--port 4002]
    python -m bot.market_data --dataset data/signals_dataset.jsonl   # prefetch

Bars are US/Eastern, regular session only (09:30-15:59), one row per minute:
ts,open,high,low,close,volume.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

log = logging.getLogger("bot.market_data")

ET = ZoneInfo("America/New_York")
BARS_DIR = Path(os.environ.get("MARKET_DATA_DIR", "data/bars"))
_MIN_REQUEST_GAP_S = 0.25   # IBKR pacing: stay far below 60 requests / 10 min bursts
_SESSION_OPEN = (9, 30)
_SESSION_CLOSE = (16, 0)


@dataclass(frozen=True)
class Bar:
    ts: datetime          # bar start, US/Eastern
    open: float
    high: float
    low: float
    close: float
    volume: float


def bar_path(symbol: str, day: date, root: Path | None = None) -> Path:
    return (root or BARS_DIR) / symbol.upper() / f"{day.isoformat()}.csv"


def load_bars(symbol: str, day: date, root: Path | None = None) -> list[Bar] | None:
    path = bar_path(symbol, day, root)
    if not path.exists():
        return None
    bars: list[Bar] = []
    with path.open("r", encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            bars.append(Bar(
                ts=datetime.fromisoformat(row["ts"]),
                open=float(row["open"]), high=float(row["high"]),
                low=float(row["low"]), close=float(row["close"]),
                volume=float(row["volume"] or 0),
            ))
    return bars


def save_bars(symbol: str, day: date, bars: Iterable[Bar], root: Path | None = None) -> Path:
    path = bar_path(symbol, day, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["ts", "open", "high", "low", "close", "volume"])
        for b in bars:
            writer.writerow([b.ts.isoformat(), b.open, b.high, b.low, b.close, b.volume])
    tmp.replace(path)
    return path


def is_trading_day(day: date) -> bool:
    return day.weekday() < 5


def next_trading_day(day: date) -> date:
    day = day + timedelta(days=1)
    while not is_trading_day(day):
        day += timedelta(days=1)
    return day


def session_date_for(posted_at: datetime) -> date:
    """Session a signal posted at ``posted_at`` is first tradable in.

    Posted before the close -> that session (or the next weekday); posted at
    or after 16:00 ET -> the next trading day.
    """
    local = posted_at.astimezone(ET)
    day = local.date()
    if (local.hour, local.minute) >= _SESSION_CLOSE or not is_trading_day(day):
        day = next_trading_day(day)
    return day


# ---------------------------------------------------------------------------
# IBKR fetch
# ---------------------------------------------------------------------------

class BarFetcher:
    """Fetch-and-cache wrapper around one ib_async connection."""

    def __init__(self, ib: Any, root: Path | None = None) -> None:
        self._ib = ib
        self._root = root
        self._contracts: dict[str, Any] = {}
        self._last_request = 0.0
        self.requests = 0

    @classmethod
    def connect(cls, host: str | None = None, port: int | None = None, client_id: int | None = None,
                root: Path | None = None) -> "BarFetcher":
        from ib_async import IB
        ib = IB()
        ib.connect(
            host or os.environ.get("IBKR_HOST", "127.0.0.1"),
            port or int(os.environ.get("IBKR_PORT", "4002")),
            clientId=client_id or int(os.environ.get("IBKR_CLIENT_ID_RESEARCH", "29")),
            timeout=20,
            readonly=True,
        )
        ib.reqMarketDataType(3)
        return cls(ib, root)

    def close(self) -> None:
        try:
            self._ib.disconnect()
        except Exception:  # pragma: no cover
            pass

    def _contract(self, symbol: str) -> Any:
        symbol = symbol.upper()
        if symbol not in self._contracts:
            from ib_async import Stock
            qualified = self._ib.qualifyContracts(Stock(symbol, "SMART", "USD"))
            if not qualified or getattr(qualified[0], "conId", 0) in (0, None):
                # Ambiguous or unknown symbols (e.g. "OK") come back unqualified.
                raise LookupError(f"IBKR cannot qualify {symbol}")
            self._contracts[symbol] = qualified[0]
        return self._contracts[symbol]

    def get(self, symbol: str, day: date) -> list[Bar]:
        cached = load_bars(symbol, day, self._root)
        if cached is not None:
            return cached
        gap = time.monotonic() - self._last_request
        if gap < _MIN_REQUEST_GAP_S:
            time.sleep(_MIN_REQUEST_GAP_S - gap)
        contract = self._contract(symbol)
        end = f"{day.strftime('%Y%m%d')} 16:00:00 US/Eastern"
        raw = self._ib.reqHistoricalData(
            contract, endDateTime=end, durationStr="1 D", barSizeSetting="1 min",
            whatToShow="TRADES", useRTH=True, formatDate=1, timeout=60,
        )
        self._last_request = time.monotonic()
        self.requests += 1
        bars = []
        for b in raw:
            ts = b.date if isinstance(b.date, datetime) else datetime.combine(b.date, datetime.min.time())
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=ET)
            ts = ts.astimezone(ET)
            if ts.date() != day:
                continue
            bars.append(Bar(ts, float(b.open), float(b.high), float(b.low), float(b.close), float(b.volume)))
        save_bars(symbol, day, bars, self._root)
        log.info("fetched %s %s: %d bars", symbol, day, len(bars))
        return bars


def prefetch(pairs: Iterable[tuple[str, date]], fetcher: BarFetcher) -> dict[str, int]:
    """Fetch every (symbol, day) not yet cached; return a summary."""
    wanted = sorted(set(pairs))
    summary = {"pairs": len(wanted), "fetched": 0, "cached": 0, "empty": 0, "errors": 0}
    for symbol, day in wanted:
        if load_bars(symbol, day, fetcher._root) is not None:
            summary["cached"] += 1
            continue
        try:
            bars = fetcher.get(symbol, day)
        except Exception as exc:  # noqa: BLE001
            log.warning("fetch failed %s %s: %s", symbol, day, exc)
            summary["errors"] += 1
            continue
        summary["fetched"] += 1
        if not bars:
            summary["empty"] += 1
    return summary


def main(argv: list[str] | None = None) -> None:
    from dotenv import load_dotenv
    load_dotenv()
    parser = argparse.ArgumentParser(description="IBKR 1-minute bar cache")
    parser.add_argument("symbol", nargs="?")
    parser.add_argument("day", nargs="?")
    parser.add_argument("--dataset", type=Path, help="prefetch every (symbol, session) a signals dataset needs")
    parser.add_argument("--sessions", type=int, default=None,
                        help="sessions per signal to prefetch (default: the replay's per-source spans)")
    parser.add_argument("--port", type=int)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    fetcher = BarFetcher.connect(port=args.port)
    try:
        if args.dataset:
            from bot.signal_dataset import bar_requirements, read_dataset
            summary = prefetch(bar_requirements(read_dataset(args.dataset), args.sessions), fetcher)
            print(json.dumps(summary))
        elif args.symbol and args.day:
            bars = fetcher.get(args.symbol, date.fromisoformat(args.day))
            print(f"{args.symbol} {args.day}: {len(bars)} bars")
            for b in bars[:3] + bars[-3:]:
                print(f"  {b.ts:%H:%M} o={b.open} h={b.high} l={b.low} c={b.close} v={b.volume:.0f}")
        else:
            parser.error("give SYMBOL DAY or --dataset")
    finally:
        fetcher.close()


if __name__ == "__main__":
    main()
