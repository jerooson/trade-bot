"""
Discord-signal executor (DRY_RUN only).

Watches `logs/swings.jsonl` (and the historical `logs/swings_history.jsonl`)
for trade actions parsed by `bot/swing_parser.py`. For each ACTIONABLE action,
applies sizing rules and records a "proposed order" — what we WOULD place if
running in LIVE mode.

In DRY_RUN mode (the only mode supported) no real broker calls are
made. Proposed orders are written to `logs/proposed_orders.jsonl` and pushed
to your phone via `bot/notifier.py`.

Connecting Robinhood Trading MCP to Codex lets an active Codex session place
orders in a dedicated Robinhood Agentic account. It does not make the MCP
available as a callable API inside this long-running Python process. See
`docs/robinhood-mcp.md` before attempting live execution.

Sizing rules
------------
- Each ticker has a fixed *budget* (default $20, env: EXECUTOR_BUDGET_PER_TICKER).
- Position fractions in the signal are read as fractions of that budget:
  - ENTRY 1/3  → buy $20 × 1/3 ≈ $6.67
  - ADD   1/4  → add $20 × 1/4 = $5  (capped so total deployed ≤ budget)
  - REDUCE 1/8 → sell $20 × 1/8 = $2.50 (capped at what we hold)
  - CLOSE      → sell everything for that ticker
  - STOP_TRIGGER → sell everything for that ticker
- Max concurrent tickers (default 5, env: EXECUTOR_MAX_OPEN_TICKERS).
- SHORT signals are rejected (cash account, no shorting).
- ADD for tickers we don't hold yet is rejected (we missed the ENTRY).

State
-----
- `logs/virtual_book.json`  — current virtual holdings + summary stats.
  Rebuilt on every startup by replaying all swing actions chronologically.
- `logs/proposed_orders.jsonl` — append-only log of every decision made in
  live (post-startup) mode. Startup replay does NOT append here, so restarting
  the executor never re-spams orders or notifications.

Run
---
    python -m bot.executor
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from dotenv import load_dotenv

from bot.notifier import Notifier
from bot.swing_parser import ActionKind, Side

log = logging.getLogger("bot.executor")


# ---------------------------------------------------------------------------
# Configuration.
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = PROJECT_ROOT / "logs"

SWING_HISTORY_PATH = LOG_DIR / "swings_history.jsonl"
SWING_LIVE_PATH = LOG_DIR / "swings.jsonl"


@dataclass(frozen=True)
class ExecutorConfig:
    """Resolved executor configuration."""

    budget_per_ticker: float
    max_open_tickers: int
    mode: str  # "DRY_RUN" only; LIVE is intentionally rejected
    book_path: Path
    orders_path: Path
    poll_interval_s: float
    swing_live_path: Path
    swing_history_path: Path
    # Notify only for actions whose received_at is past this cutoff. Set at
    # boot to now() so historical replay is silent.
    notify_after: datetime
    # Skip any historical action whose received_at is <= this cutoff when
    # replaying at startup. None means replay everything (default).
    replay_from: datetime | None

    @property
    def is_dry_run(self) -> bool:
        return self.mode.upper() == "DRY_RUN"


def _parse_iso_cutoff(raw: str | None) -> datetime | None:
    """Parse an ISO 8601 timestamp from env. Empty/invalid → None."""
    if not raw or not raw.strip():
        return None
    s = raw.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        log.warning("invalid EXECUTOR_REPLAY_FROM=%r; ignoring", raw)
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def load_config() -> ExecutorConfig:
    load_dotenv()
    return ExecutorConfig(
        budget_per_ticker=float(os.environ.get("EXECUTOR_BUDGET_PER_TICKER", "20")),
        max_open_tickers=int(os.environ.get("EXECUTOR_MAX_OPEN_TICKERS", "5")),
        mode=(os.environ.get("EXECUTOR_MODE") or "DRY_RUN").upper(),
        book_path=Path(os.environ.get("EXECUTOR_BOOK_PATH", "./logs/virtual_book.json")),
        orders_path=Path(
            os.environ.get("EXECUTOR_ORDERS_PATH", "./logs/proposed_orders.jsonl")
        ),
        poll_interval_s=float(os.environ.get("EXECUTOR_POLL_INTERVAL_S", "1.0")),
        swing_live_path=SWING_LIVE_PATH,
        swing_history_path=SWING_HISTORY_PATH,
        notify_after=datetime.now(timezone.utc),
        replay_from=_parse_iso_cutoff(os.environ.get("EXECUTOR_REPLAY_FROM")),
    )


# ---------------------------------------------------------------------------
# Virtual book.
# ---------------------------------------------------------------------------

@dataclass
class VirtualPosition:
    """One open virtual position in the executor's book."""

    ticker: str
    side: str  # "LONG" (we don't open SHORTs)
    shares: float  # virtual quantity (estimated from signal prices)
    deployed_usd: float  # dollars currently allocated to this ticker
    budget_usd: float  # per-ticker cap (== config.budget_per_ticker at entry)
    avg_price: float | None  # share-weighted average entry price
    stop_loss: float | None  # last-seen numeric stop (informational)
    stop_loss_label: str | None  # raw stop string from signal
    last_signal_fraction: float | None  # Will's most recent position_fraction
    last_signal_size: str | None  # Will's raw position_size string ("1/3")
    first_entry_at: str
    last_action_at: str
    actions_count: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class VirtualBook:
    """Aggregate state across all virtual positions."""

    mode: str
    budget_per_ticker: float
    max_open_tickers: int
    positions: dict[str, VirtualPosition] = field(default_factory=dict)
    started_at: str = ""
    last_processed_at: str | None = None
    last_decision_at: str | None = None
    decisions_total: int = 0

    @property
    def open_count(self) -> int:
        return len(self.positions)

    @property
    def total_deployed_usd(self) -> float:
        return sum(p.deployed_usd for p in self.positions.values())

    @property
    def account_budget_usd(self) -> float:
        # Theoretical max if every ticker slot were fully deployed.
        return self.budget_per_ticker * self.max_open_tickers

    def snapshot(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "budget_per_ticker": self.budget_per_ticker,
            "max_open_tickers": self.max_open_tickers,
            "started_at": self.started_at,
            "last_processed_at": self.last_processed_at,
            "last_decision_at": self.last_decision_at,
            "decisions_total": self.decisions_total,
            "positions": {t: p.to_dict() for t, p in self.positions.items()},
            "summary": {
                "open_tickers": self.open_count,
                "max_tickers": self.max_open_tickers,
                "total_deployed_usd": round(self.total_deployed_usd, 4),
                "account_budget_usd": round(self.account_budget_usd, 2),
                "available_usd": round(
                    self.account_budget_usd - self.total_deployed_usd, 4
                ),
            },
        }


# ---------------------------------------------------------------------------
# Decision dataclass.
# ---------------------------------------------------------------------------

ACTIONABLE_KINDS: set[str] = {
    ActionKind.ENTRY.value,
    ActionKind.ADD.value,
    ActionKind.REDUCE.value,
    ActionKind.CLOSE.value,
    ActionKind.STOP_TRIGGER.value,
}


@dataclass
class Decision:
    """What the executor decided to do (or not do) in response to a signal."""

    id: str
    decided_at: str
    mode: str
    signal_kind: str
    ticker: str
    action: str  # "BUY" / "SELL" / "REJECT"
    usd_amount: float | None
    shares_estimate: float | None
    signal_price: float | None
    rationale: str
    book_before: dict[str, Any]
    book_after: dict[str, Any]
    signal: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Decision engine.
# ---------------------------------------------------------------------------

def _new_decision_id(action_kind: str, ticker: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return f"ord_{stamp}_{ticker}_{action_kind}_{uuid.uuid4().hex[:6]}"


def _shares_estimate(usd: float, price: float | None) -> float | None:
    """Estimate shares we'd buy/sell for `usd` at the signal price."""
    if price is None or price <= 0:
        return None
    return round(usd / price, 6)


def _book_snapshot(book: VirtualBook, ticker: str) -> dict[str, Any]:
    """Lightweight snapshot used for before/after diffs in the decision log."""
    pos = book.positions.get(ticker)
    return {
        "open_count": book.open_count,
        "total_deployed_usd": round(book.total_deployed_usd, 4),
        "ticker_position": pos.to_dict() if pos else None,
    }


def decide(action: dict[str, Any], book: VirtualBook, config: ExecutorConfig) -> Decision:
    """Pure(-ish) decision function: action + book → Decision.

    Note: this mutates `book` only for accepted BUY/SELL decisions (so the
    caller doesn't need a second "apply" step). Pass a copy if you want a
    no-side-effect dry run.
    """

    kind = action.get("kind", "")
    ticker = (action.get("ticker") or "").upper()
    side_raw = action.get("side")
    signal_price = action.get("price")
    fraction = action.get("position_fraction")
    pos_size_str = action.get("position_size")
    delta_size_str = action.get("delta_size")
    received_at = action.get("received_at") or datetime.now(timezone.utc).isoformat()

    book_before = _book_snapshot(book, ticker)

    def _reject(reason: str) -> Decision:
        d = Decision(
            id=_new_decision_id(kind or "UNK", ticker or "UNK"),
            decided_at=datetime.now(timezone.utc).isoformat(),
            mode=config.mode,
            signal_kind=kind,
            ticker=ticker,
            action="REJECT",
            usd_amount=None,
            shares_estimate=None,
            signal_price=signal_price,
            rationale=reason,
            book_before=book_before,
            book_after=book_before,
            signal=_compact_signal(action),
        )
        return d

    if kind not in ACTIONABLE_KINDS:
        return _reject(f"non-actionable kind: {kind}")

    if not ticker:
        return _reject("missing ticker")

    # Reject SHORT signals up-front: cash account, no shorting.
    if side_raw == Side.SHORT.value:
        return _reject("SHORT signal; cash account does not short")

    # --- ENTRY -----------------------------------------------------------
    if kind == ActionKind.ENTRY.value:
        if ticker in book.positions:
            return _reject(
                f"already holding {ticker}; ignore duplicate ENTRY "
                "(close first or wait for Will's CLOSE)"
            )
        if book.open_count >= config.max_open_tickers:
            return _reject(
                f"max {config.max_open_tickers} concurrent tickers reached "
                f"(currently holding: {sorted(book.positions)})"
            )
        if signal_price is None or signal_price <= 0:
            return _reject("ENTRY signal has no usable price")
        frac = fraction or 1.0
        usd = round(config.budget_per_ticker * frac, 4)
        if usd <= 0.01:
            return _reject(f"computed USD amount too small ({usd})")
        shares = _shares_estimate(usd, signal_price)

        pos = VirtualPosition(
            ticker=ticker,
            side="LONG",
            shares=shares or 0.0,
            deployed_usd=usd,
            budget_usd=config.budget_per_ticker,
            avg_price=signal_price,
            stop_loss=action.get("stop_loss"),
            stop_loss_label=action.get("stop_loss_label"),
            last_signal_fraction=frac,
            last_signal_size=pos_size_str,
            first_entry_at=received_at,
            last_action_at=received_at,
            actions_count=1,
        )
        book.positions[ticker] = pos

        return Decision(
            id=_new_decision_id(kind, ticker),
            decided_at=datetime.now(timezone.utc).isoformat(),
            mode=config.mode,
            signal_kind=kind,
            ticker=ticker,
            action="BUY",
            usd_amount=usd,
            shares_estimate=shares,
            signal_price=signal_price,
            rationale=(
                f"ENTRY {pos_size_str or frac} of ${config.budget_per_ticker:.2f} budget = ${usd:.2f}"
            ),
            book_before=book_before,
            book_after=_book_snapshot(book, ticker),
            signal=_compact_signal(action),
        )

    # --- ADD -------------------------------------------------------------
    if kind == ActionKind.ADD.value:
        pos = book.positions.get(ticker)
        if pos is None:
            return _reject(f"ADD for {ticker} but we don't hold it (missed ENTRY?)")
        if signal_price is None or signal_price <= 0:
            return _reject("ADD signal has no usable price")
        frac = fraction or 0.0
        if frac <= 0:
            return _reject(f"ADD with non-positive fraction {pos_size_str!r}")
        wanted_usd = round(config.budget_per_ticker * frac, 4)
        room = round(pos.budget_usd - pos.deployed_usd, 4)
        if room <= 0.01:
            return _reject(
                f"{ticker} already at budget cap (${pos.deployed_usd:.2f}/${pos.budget_usd:.2f}); skipping ADD"
            )
        usd = min(wanted_usd, room)
        shares = _shares_estimate(usd, signal_price)
        # Recompute share-weighted avg price.
        prev_shares = pos.shares
        new_shares = (shares or 0.0) + prev_shares
        if new_shares > 0 and shares is not None and pos.avg_price is not None:
            pos.avg_price = round(
                (pos.avg_price * prev_shares + signal_price * shares) / new_shares,
                6,
            )
        pos.shares = new_shares
        pos.deployed_usd = round(pos.deployed_usd + usd, 4)
        pos.last_signal_fraction = frac
        pos.last_signal_size = pos_size_str
        pos.last_action_at = received_at
        pos.actions_count += 1
        if action.get("stop_loss") is not None:
            pos.stop_loss = action.get("stop_loss")
            pos.stop_loss_label = action.get("stop_loss_label")

        rationale = f"ADD {pos_size_str or frac} of ${config.budget_per_ticker:.2f} budget = ${usd:.2f}"
        if usd < wanted_usd:
            rationale += f" (capped from ${wanted_usd:.2f} to fit remaining ${room:.2f} of budget)"

        return Decision(
            id=_new_decision_id(kind, ticker),
            decided_at=datetime.now(timezone.utc).isoformat(),
            mode=config.mode,
            signal_kind=kind,
            ticker=ticker,
            action="BUY",
            usd_amount=usd,
            shares_estimate=shares,
            signal_price=signal_price,
            rationale=rationale,
            book_before=book_before,
            book_after=_book_snapshot(book, ticker),
            signal=_compact_signal(action),
        )

    # --- REDUCE ----------------------------------------------------------
    if kind == ActionKind.REDUCE.value:
        pos = book.positions.get(ticker)
        if pos is None:
            return _reject(f"REDUCE for {ticker} but we don't hold it")
        # REDUCE's fraction lives in `delta_size` (e.g. "1/8") because the
        # parser stores it separately from `position_size`.
        frac = _fraction_of(delta_size_str)
        if frac is None or frac <= 0:
            return _reject(f"REDUCE with no usable delta fraction ({delta_size_str!r})")
        # REDUCE signals from Discord don't carry a price; fall back to avg_price.
        if signal_price is None or signal_price <= 0:
            signal_price = pos.avg_price if pos.avg_price and pos.avg_price > 0 else None
        if signal_price is None:
            return _reject("REDUCE signal has no usable price and position has no avg_price")
        wanted_usd = round(config.budget_per_ticker * frac, 4)
        usd = min(wanted_usd, pos.deployed_usd)  # never sell more than we hold
        if usd <= 0.01:
            return _reject(f"REDUCE amount ${usd:.2f} too small")
        shares = _shares_estimate(usd, signal_price)
        # Shave shares + deployed proportionally; avg_price unchanged.
        if pos.deployed_usd > 0:
            sell_ratio = usd / pos.deployed_usd
            sold_shares = pos.shares * sell_ratio
            pos.shares = max(0.0, pos.shares - sold_shares)
            pos.deployed_usd = round(pos.deployed_usd - usd, 4)
        pos.last_action_at = received_at
        pos.actions_count += 1
        # If we've sold down to dust, close the position.
        if pos.deployed_usd <= 0.01 or pos.shares <= 1e-6:
            book.positions.pop(ticker, None)

        rationale = (
            f"REDUCE {delta_size_str} of ${config.budget_per_ticker:.2f} budget = ${usd:.2f}"
        )
        if usd < wanted_usd:
            rationale += f" (capped at our holding of ${pos.deployed_usd + usd:.2f})"

        return Decision(
            id=_new_decision_id(kind, ticker),
            decided_at=datetime.now(timezone.utc).isoformat(),
            mode=config.mode,
            signal_kind=kind,
            ticker=ticker,
            action="SELL",
            usd_amount=usd,
            shares_estimate=shares,
            signal_price=signal_price,
            rationale=rationale,
            book_before=book_before,
            book_after=_book_snapshot(book, ticker),
            signal=_compact_signal(action),
        )

    # --- CLOSE or STOP_TRIGGER -------------------------------------------
    if kind in (ActionKind.CLOSE.value, ActionKind.STOP_TRIGGER.value):
        pos = book.positions.get(ticker)
        if pos is None:
            return _reject(f"{kind} for {ticker} but we don't hold it")
        usd = pos.deployed_usd
        shares = pos.shares
        # Even if signal has no price, we can still close — just can't estimate fill.
        sell_price = signal_price
        book.positions.pop(ticker, None)
        rationale = f"{kind} → exit full position (${usd:.2f}, {shares:.6f} sh)"

        return Decision(
            id=_new_decision_id(kind, ticker),
            decided_at=datetime.now(timezone.utc).isoformat(),
            mode=config.mode,
            signal_kind=kind,
            ticker=ticker,
            action="SELL",
            usd_amount=usd,
            shares_estimate=shares,
            signal_price=sell_price,
            rationale=rationale,
            book_before=book_before,
            book_after=_book_snapshot(book, ticker),
            signal=_compact_signal(action),
        )

    return _reject(f"unhandled kind: {kind}")


def _fraction_of(s: str | None) -> float | None:
    """Parse "1/8" → 0.125. Strict; returns None on non-fraction input."""
    if not s:
        return None
    parts = s.strip().split("/")
    if len(parts) != 2:
        return None
    try:
        num = int(parts[0])
        den = int(parts[1])
    except ValueError:
        return None
    if den == 0:
        return None
    return num / den


def _compact_signal(action: dict[str, Any]) -> dict[str, Any]:
    """Shrink the swing action down to what's useful in the decision log."""
    keep = (
        "kind", "ticker", "side", "price", "stop_loss", "stop_loss_label",
        "position_size", "position_fraction", "delta_size", "action_text",
        "received_at", "posted_by",
    )
    out = {k: action.get(k) for k in keep}
    if "discord" in action:
        d = action["discord"]
        out["message_id"] = d.get("message_id")
        out["channel_id"] = d.get("channel_id")
        out["created_at"] = d.get("created_at")
    return out


# ---------------------------------------------------------------------------
# Persistence + tailing.
# ---------------------------------------------------------------------------

def _read_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as e:
                log.warning("Bad JSONL line in %s: %s", path, e)


def _action_timestamp(action: dict[str, Any]) -> str:
    return (
        action.get("received_at")
        or action.get("discord", {}).get("created_at")
        or ""
    )


def _parse_action_dt(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def replay_history(config: ExecutorConfig, book: VirtualBook) -> None:
    """Read history + live files chronologically, apply each action silently.

    If ``config.replay_from`` is set, actions whose ``received_at`` is at or
    before that cutoff are skipped — used to start the virtual book fresh
    while preserving the underlying signal history on disk.
    """
    seen_ids: set[int] = set()
    actions: list[dict[str, Any]] = []
    for path in (config.swing_history_path, config.swing_live_path):
        for a in _read_jsonl(path):
            mid = a.get("discord", {}).get("message_id")
            if mid is not None:
                if mid in seen_ids:
                    continue
                seen_ids.add(mid)
            actions.append(a)
    actions.sort(key=_action_timestamp)

    applied = 0
    skipped_pre_cutoff = 0
    cutoff = config.replay_from
    for a in actions:
        if a.get("kind") not in ACTIONABLE_KINDS:
            continue
        if cutoff is not None:
            ts = _parse_action_dt(_action_timestamp(a))
            if ts is not None and ts <= cutoff:
                skipped_pre_cutoff += 1
                continue
        # Mutate the book; discard the Decision (we don't log replay decisions).
        decide(a, book, config)
        applied += 1
    book.last_processed_at = datetime.now(timezone.utc).isoformat()
    if cutoff is not None:
        log.info(
            "replay: cutoff=%s — skipped %d pre-cutoff actions, applied %d, "
            "book now has %d open positions: %s",
            cutoff.isoformat(),
            skipped_pre_cutoff,
            applied,
            book.open_count,
            sorted(book.positions),
        )
    else:
        log.info(
            "replay: applied %d actions (of %d total), book now has %d open positions: %s",
            applied,
            len(actions),
            book.open_count,
            sorted(book.positions),
        )


def write_book(book: VirtualBook, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write to a temp file then rename so the dashboard never reads a partial JSON.
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(book.snapshot(), ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def append_decision(decision: Decision, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(decision.to_dict(), ensure_ascii=False) + "\n")


class TailReader:
    """Byte-offset based tailer for an append-only JSONL file.

    Initialize once, then call `read_new_records()` in your poll loop.
    `start_at_end=True` seeks to EOF on first call so historical entries
    are skipped (we replay them separately).
    """

    def __init__(self, path: Path, *, start_at_end: bool):
        self.path = path
        self.offset = path.stat().st_size if (start_at_end and path.exists()) else 0
        self._buf = ""

    def read_new_records(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            self.offset = 0
            self._buf = ""
            return []
        size = self.path.stat().st_size
        if size < self.offset:
            # File rotated/truncated; start over.
            self.offset = 0
            self._buf = ""
        if size == self.offset:
            return []
        with self.path.open("r", encoding="utf-8") as fh:
            fh.seek(self.offset)
            chunk = fh.read()
            self.offset = fh.tell()
        self._buf += chunk

        out: list[dict[str, Any]] = []
        while "\n" in self._buf:
            line, _, self._buf = self._buf.partition("\n")
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError as e:
                log.warning("bad JSONL on tail: %s", e)
        return out


# ---------------------------------------------------------------------------
# Notification formatting.
# ---------------------------------------------------------------------------

def format_notification(decision: Decision) -> tuple[str, str, list[str]]:
    """Return (title, body, tags) for a phone push."""
    if decision.action == "BUY":
        title = f"[{decision.mode}] BUY {decision.ticker} ${decision.usd_amount:.2f}"
        tags = ["chart_with_upwards_trend"]
    elif decision.action == "SELL":
        title = f"[{decision.mode}] SELL {decision.ticker} ${(decision.usd_amount or 0):.2f}"
        tags = ["chart_with_downwards_trend"]
    else:
        title = f"[{decision.mode}] SKIP {decision.ticker} ({decision.signal_kind})"
        tags = ["no_entry"]

    body_lines = [
        f"signal: {decision.signal_kind}"
        + (f"  ${decision.signal_price:.2f}" if decision.signal_price else ""),
        f"reason: {decision.rationale}",
    ]
    if decision.shares_estimate is not None:
        body_lines.append(f"qty est: {decision.shares_estimate:.4f} sh")
    open_count = decision.book_after.get("open_count", 0)
    body_lines.append(f"book: {open_count} open")
    body = "\n".join(body_lines)

    return title, body, tags


# ---------------------------------------------------------------------------
# Main loop.
# ---------------------------------------------------------------------------

def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def run(config: ExecutorConfig) -> None:
    if not config.is_dry_run:
        log.error(
            "EXECUTOR_MODE=%s is not supported. This process cannot call Codex's "
            "Robinhood MCP connection, so only DRY_RUN is allowed. Follow "
            "docs/robinhood-mcp.md before attempting live execution. Exiting.",
            config.mode,
        )
        sys.exit(2)

    book = VirtualBook(
        mode=config.mode,
        budget_per_ticker=config.budget_per_ticker,
        max_open_tickers=config.max_open_tickers,
        started_at=datetime.now(timezone.utc).isoformat(),
    )

    log.info(
        "executor starting: mode=%s budget=$%.2f/ticker max_tickers=%d",
        config.mode,
        config.budget_per_ticker,
        config.max_open_tickers,
    )
    log.info("book        -> %s", config.book_path)
    log.info("decisions   -> %s", config.orders_path)
    log.info("watching    -> %s", config.swing_live_path)

    replay_history(config, book)
    write_book(book, config.book_path)

    tail = TailReader(config.swing_live_path, start_at_end=True)
    log.info("now live; tailing for new swing actions every %.2fs", config.poll_interval_s)

    stop = False

    def _shutdown(*_a) -> None:
        nonlocal stop
        stop = True
        log.info("shutdown signal received; finishing current poll then exiting.")

    signal.signal(signal.SIGINT, _shutdown)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _shutdown)

    with Notifier() as notifier:
        if not notifier.config.enabled:
            log.info("ntfy disabled (NTFY_TOPIC not set); decisions still written to disk.")

        while not stop:
            new_records = tail.read_new_records()
            if new_records:
                book_dirty = False
                for action in new_records:
                    if action.get("kind") not in ACTIONABLE_KINDS:
                        # Still update last_processed_at so dashboard heartbeat moves.
                        book.last_processed_at = datetime.now(timezone.utc).isoformat()
                        continue
                    decision = decide(action, book, config)
                    book.decisions_total += 1
                    book.last_decision_at = decision.decided_at
                    book.last_processed_at = decision.decided_at
                    append_decision(decision, config.orders_path)
                    book_dirty = True

                    log.info(
                        "%s %s %s usd=%s shares=%s :: %s",
                        decision.action,
                        decision.ticker,
                        decision.signal_kind,
                        f"${decision.usd_amount:.2f}" if decision.usd_amount else "—",
                        f"{decision.shares_estimate:.4f}" if decision.shares_estimate else "—",
                        decision.rationale,
                    )

                    title, body, tags = format_notification(decision)
                    notifier.push(title, body, tags=tags)

                if book_dirty:
                    write_book(book, config.book_path)
            time.sleep(config.poll_interval_s)

    write_book(book, config.book_path)
    log.info("executor stopped.")


def main() -> None:
    _setup_logging()
    config = load_config()
    run(config)


if __name__ == "__main__":
    main()
