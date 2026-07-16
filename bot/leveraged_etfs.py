"""Curated leveraged-ETF execution routes for Heat day-trade ideas.

The source ticker remains the signal instrument.  These routes only decide
which liquid leveraged ETF may be used for execution after the source trigger
has fired and a live quote/tradability preflight has passed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class LeveragedETF:
    ticker: str
    leverage: float


P0_LEVERAGED_ETFS: dict[str, dict[str, tuple[LeveragedETF, ...]]] = {
    "SPY": {
        "long": (LeveragedETF("SPXL", 3.0),),
        "short": (LeveragedETF("SPXS", 3.0),),
    },
    "QQQ": {
        "long": (LeveragedETF("TQQQ", 3.0),),
        "short": (LeveragedETF("SQQQ", 3.0),),
    },
    "IWM": {
        "long": (LeveragedETF("TNA", 3.0),),
        "short": (LeveragedETF("TZA", 3.0),),
    },
    "SMH": {
        "long": (LeveragedETF("SOXL", 3.0),),
        "short": (LeveragedETF("SOXS", 3.0),),
    },
    "SOXX": {
        "long": (LeveragedETF("SOXL", 3.0),),
        "short": (LeveragedETF("SOXS", 3.0),),
    },
    "NVDA": {
        "long": (
            LeveragedETF("NVDL", 2.0),
            LeveragedETF("NVDX", 2.0),
            LeveragedETF("NVDU", 2.0),
        ),
        "short": (
            LeveragedETF("NVD", 2.0),
            LeveragedETF("NVDQ", 2.0),
        ),
    },
    "TSLA": {
        "long": (LeveragedETF("TSLL", 2.0),),
        "short": (LeveragedETF("TSLZ", 2.0),),
    },
    "MSTR": {
        "long": (
            LeveragedETF("MSTU", 2.0),
            LeveragedETF("MSTX", 2.0),
        ),
        "short": (LeveragedETF("MSTZ", 2.0),),
    },
}


def leveraged_candidates(ticker: str, direction: str) -> tuple[LeveragedETF, ...]:
    return P0_LEVERAGED_ETFS.get(ticker.upper(), {}).get(direction.lower(), ())


def candidate_symbols(ticker: str, direction: str) -> list[str]:
    return [candidate.ticker for candidate in leveraged_candidates(ticker, direction)]


def quote_symbol(item: dict[str, Any], fallback: str = "") -> str:
    quote = item.get("quote") or item
    return str(
        item.get("symbol")
        or quote.get("symbol")
        or quote.get("instrument_symbol")
        or fallback
    ).upper()


def result_by_symbol(
    results: Iterable[dict[str, Any]],
    requested_symbols: list[str],
) -> dict[str, dict[str, Any]]:
    rows = list(results)
    mapped: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(rows):
        fallback = requested_symbols[index] if len(rows) == len(requested_symbols) else ""
        symbol = quote_symbol(item, fallback)
        if symbol:
            mapped[symbol] = item
    return mapped
