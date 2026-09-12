"""
Daily bar cache backed by IB Gateway historical data.

Companion to ``bot.market_data`` (1-minute bars).  One CSV per symbol under
``data/daily/<SYMBOL>.csv`` holding regular-hours daily bars, refreshed by
re-fetching the whole span (cheap: one request per symbol).

    python -m bot.daily_data SPXL DELL --years 2
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger("bot.daily_data")

DAILY_DIR = Path(os.environ.get("DAILY_DATA_DIR", "data/daily"))
_MIN_REQUEST_GAP_S = 0.25


@dataclass(frozen=True)
class DailyBar:
    day: date
    open: float
    high: float
    low: float
    close: float
    volume: float


def daily_path(symbol: str, root: Path | None = None) -> Path:
    return (root or DAILY_DIR) / f"{symbol.upper()}.csv"


def load_daily(symbol: str, root: Path | None = None) -> list[DailyBar] | None:
    path = daily_path(symbol, root)
    if not path.exists():
        return None
    bars: list[DailyBar] = []
    with path.open("r", encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            bars.append(DailyBar(date.fromisoformat(row["day"]), float(row["open"]), float(row["high"]),
                                 float(row["low"]), float(row["close"]), float(row["volume"] or 0)))
    return bars


def save_daily(symbol: str, bars: Iterable[DailyBar], root: Path | None = None) -> Path:
    path = daily_path(symbol, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["day", "open", "high", "low", "close", "volume"])
        for b in bars:
            w.writerow([b.day.isoformat(), b.open, b.high, b.low, b.close, b.volume])
    tmp.replace(path)
    return path


class DailyFetcher:
    def __init__(self, ib: Any, root: Path | None = None) -> None:
        self._ib = ib
        self._root = root
        self._contracts: dict[str, Any] = {}
        self._last = 0.0

    @classmethod
    def connect(cls, root: Path | None = None) -> "DailyFetcher":
        from ib_async import IB
        ib = IB()
        ib.connect(os.environ.get("IBKR_HOST", "127.0.0.1"), int(os.environ.get("IBKR_PORT", "4002")),
                   clientId=int(os.environ.get("IBKR_CLIENT_ID_RESEARCH", "29")) + 1, timeout=20, readonly=True)
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
            q = self._ib.qualifyContracts(Stock(symbol, "SMART", "USD"))
            if not q or not getattr(q[0], "conId", 0):
                raise LookupError(f"IBKR cannot qualify {symbol}")
            self._contracts[symbol] = q[0]
        return self._contracts[symbol]

    def get(self, symbol: str, years: int = 2, *, refresh: bool = False) -> list[DailyBar]:
        cached = None if refresh else load_daily(symbol, self._root)
        if cached is not None:
            return cached
        gap = time.monotonic() - self._last
        if gap < _MIN_REQUEST_GAP_S:
            time.sleep(_MIN_REQUEST_GAP_S - gap)
        raw = self._ib.reqHistoricalData(self._contract(symbol), endDateTime="", durationStr=f"{years} Y",
                                         barSizeSetting="1 day", whatToShow="TRADES", useRTH=True,
                                         formatDate=1, timeout=60)
        self._last = time.monotonic()
        bars = []
        for b in raw:
            d = b.date if isinstance(b.date, date) and not isinstance(b.date, datetime) else b.date.date()
            bars.append(DailyBar(d, float(b.open), float(b.high), float(b.low), float(b.close), float(b.volume)))
        save_daily(symbol, bars, self._root)
        log.info("fetched %s: %d daily bars", symbol, len(bars))
        return bars


def main(argv: list[str] | None = None) -> None:
    from dotenv import load_dotenv
    load_dotenv()
    p = argparse.ArgumentParser(description="IBKR daily bar cache")
    p.add_argument("symbols", nargs="*")
    p.add_argument("--from-dataset", type=Path, help="fetch every ticker in a signals dataset")
    p.add_argument("--years", type=int, default=2)
    p.add_argument("--refresh", action="store_true")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    symbols = list(args.symbols)
    if args.from_dataset:
        from bot.signal_dataset import read_dataset
        symbols += sorted({r.ticker for r in read_dataset(args.from_dataset)})
    f = DailyFetcher.connect()
    ok = err = 0
    try:
        for s in dict.fromkeys(x.upper() for x in symbols):
            try:
                f.get(s, args.years, refresh=args.refresh); ok += 1
            except Exception as exc:  # noqa: BLE001
                log.warning("skip %s: %s", s, exc); err += 1
    finally:
        f.close()
    print(f"daily bars cached: {ok} symbols, {err} errors")


if __name__ == "__main__":
    main()
