"""
Per-strategy share ownership for one shared Robinhood account.

The swing executor and the day trader can hold the same symbol (for example
both can own SPXL when a Heat SPY watch routes into SPXL while Will posts a
SPXL swing).  Robinhood only knows the account-level quantity, so every SELL
must be capped at the quantity *this* strategy owns, never at what the account
happens to hold.

Sources of truth
----------------
- Swing: ``logs/virtual_book.json`` says which tickers the swing strategy is
  holding (presence).  ``logs/trade_pnl.jsonl`` holds the broker-confirmed
  fills for that holding, replayed from the book position's ``first_entry_at``
  so residue from earlier rounds never inflates the quantity.  When no fill has
  been recorded for the current holding the virtual share estimate is used.
- Day: ``logs/day_trade_positions.jsonl`` (last record per position id).  Open
  and exiting positions own ``fill_qty - exit_filled_qty - unreconciled_qty``;
  a pending entry owns whatever has partially filled so far.

Nothing here talks to the broker.  Callers pass the broker's actual quantity to
``sellable_quantity`` which decides how much can be sold without touching the
other strategy's shares.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("bot.position_ownership")

# Explicit overrides (tests set these).  When None, the path is resolved from
# the environment at call time, i.e. after the service has run load_dotenv().
DAY_POSITIONS_PATH: Path | None = None
SWING_BOOK_PATH: Path | None = None
SWING_PNL_PATH: Path | None = None


def day_positions_path() -> Path:
    return DAY_POSITIONS_PATH or Path(
        os.environ.get("DAY_TRADE_POSITIONS_PATH", "logs/day_trade_positions.jsonl")
    )


def swing_book_path() -> Path:
    return SWING_BOOK_PATH or Path(os.environ.get("EXECUTOR_BOOK_PATH", "logs/virtual_book.json"))


def swing_pnl_path() -> Path:
    return SWING_PNL_PATH or Path(os.environ.get("SHADOW_REVIEW_PNL_PATH", "logs/trade_pnl.jsonl"))


QTY_EPSILON = 0.000001

# Day-position statuses that still own shares at the broker.
_DAY_OWNING_STATUSES = {"open", "pending_exit"}


# ---------------------------------------------------------------------------
# Day trader
# ---------------------------------------------------------------------------

def read_latest_day_positions(path: Path | None = None) -> list[dict[str, Any]]:
    """Return the last record for every day-position id, in file order."""
    path = path or day_positions_path()
    if not path.exists():
        return []
    latest: dict[str, dict[str, Any]] = {}
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            pos_id = str(row.get("id") or "")
            if pos_id:
                latest[pos_id] = row
    return list(latest.values())


def day_position_symbol(position: dict[str, Any]) -> str:
    """Symbol the day trader actually holds (leveraged route or the ticker)."""
    return str(position.get("execution_ticker") or position.get("ticker") or "").upper()


def day_position_owned_qty(position: dict[str, Any]) -> float:
    status = str(position.get("status") or "")
    if status in _DAY_OWNING_STATUSES:
        held = float(position.get("fill_qty") or 0.0)
        sold = float(position.get("exit_filled_qty") or 0.0)
        lost = float(position.get("unreconciled_qty") or 0.0)
        return max(0.0, held - sold - lost)
    if status == "pending_entry":
        return max(0.0, float(position.get("entry_filled_qty") or 0.0))
    return 0.0


def day_owned(ticker: str, path: Path | None = None) -> float:
    """Shares of ``ticker`` currently owned by day-trade lifecycles."""
    symbol = ticker.upper()
    return sum(
        day_position_owned_qty(position)
        for position in read_latest_day_positions(path)
        if day_position_symbol(position) == symbol
    )


def day_holdings(path: Path | None = None) -> set[str]:
    """Execution symbols the day trader owns (or is buying) right now."""
    held: set[str] = set()
    for position in read_latest_day_positions(path):
        if (
            day_position_owned_qty(position) > QTY_EPSILON
            or position.get("status") == "pending_entry"
        ):
            held.add(day_position_symbol(position))
    return {symbol for symbol in held if symbol}


# ---------------------------------------------------------------------------
# Swing executor
# ---------------------------------------------------------------------------

def swing_book_position(ticker: str, book_path: Path | None = None) -> dict[str, Any] | None:
    """The swing virtual-book position for ``ticker`` or ``None``."""
    path = book_path or swing_book_path()
    if not path.exists():
        return None
    try:
        book = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    positions = book.get("positions") or {}
    position = positions.get(ticker.upper())
    return dict(position) if isinstance(position, dict) else None


def _parse_ts(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def swing_owned_from_ledger(
    ticker: str,
    *,
    since: Any = None,
    pnl_path: Path | None = None,
) -> float | None:
    """Replay broker-confirmed swing fills for ``ticker``.

    Only records at or after ``since`` (the current holding's first entry)
    count.  Returns ``None`` when the window holds no BUY fill, or when any
    BUY in it has no recorded fill quantity (fill poll timed out), so the
    caller falls back to the virtual estimate instead of undercounting.
    """
    path = pnl_path or swing_pnl_path()
    if not path.exists():
        return None
    cutoff = _parse_ts(since)
    symbol = ticker.upper()
    qty = 0.0
    saw_buy = False
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if str(rec.get("ticker") or "").upper() != symbol:
                continue
            if cutoff is not None:
                ts = _parse_ts(rec.get("timestamp"))
                if ts is not None and ts < cutoff:
                    continue
            fill_qty = float(rec.get("fill_qty") or 0.0)
            action = rec.get("action")
            if action == "BUY":
                if fill_qty <= 0:
                    return None  # unknown fill: the ledger cannot be trusted
                qty += fill_qty
                saw_buy = True
            elif action == "SELL" and fill_qty > 0:
                qty = max(0.0, qty - fill_qty)
    return qty if saw_buy else None


def swing_owned(
    ticker: str,
    book_position: dict[str, Any] | None,
    *,
    pnl_path: Path | None = None,
) -> float:
    """Shares of ``ticker`` the swing strategy owns for ``book_position``.

    ``book_position`` is the virtual-book entry (live file, or the immutable
    ``book_before.ticker_position`` snapshot carried by a proposal).  With no
    book position the swing strategy owns nothing, regardless of any residue
    left in the fill ledger by earlier rounds.
    """
    if not book_position:
        return 0.0
    from_fills = swing_owned_from_ledger(
        ticker, since=book_position.get("first_entry_at"), pnl_path=pnl_path
    )
    if from_fills is not None:
        return from_fills
    return max(0.0, float(book_position.get("shares") or 0.0))


def swing_holds(ticker: str, book_path: Path | None = None) -> bool:
    """True when the swing virtual book currently lists ``ticker``."""
    return swing_book_position(ticker, book_path) is not None


def swing_live_owned(ticker: str) -> float:
    """Swing-owned shares using the live virtual book and fill ledger."""
    return swing_owned(ticker, swing_book_position(ticker))


# ---------------------------------------------------------------------------
# Sell sizing
# ---------------------------------------------------------------------------

def sellable_quantity(
    requested: float,
    *,
    own: float,
    others: float,
    actual: float,
) -> tuple[float, str | None]:
    """How many shares one strategy may sell right now.

    ``requested`` is what the strategy wants to sell, ``own`` what its ledger
    says it holds, ``others`` what the other strategy's ledger says it holds,
    and ``actual`` the broker's account-level quantity.

    Normal case (broker holds at least everything both ledgers claim): sell
    ``min(requested, own)``.  Drift case (broker holds less): never touch the
    other strategy's shares, so the cap becomes ``actual - others``; the
    shortfall is reported in the note so it can be reconciled instead of
    silently sold or silently retried.
    """
    requested = max(0.0, float(requested))
    own = max(0.0, float(own))
    others = max(0.0, float(others))
    actual = max(0.0, float(actual))
    wanted = min(requested, own)
    if wanted <= QTY_EPSILON:
        return 0.0, None
    if actual + QTY_EPSILON >= own + others:
        return min(wanted, actual), None
    safe = max(0.0, actual - others)
    qty = min(wanted, safe)
    note = (
        f"ownership drift: broker holds {actual:.6f}, ledgers claim "
        f"own={own:.6f} others={others:.6f}; selling {qty:.6f} of {wanted:.6f}"
    )
    return qty, note
