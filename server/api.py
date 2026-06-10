"""
FastAPI backend for the trade-bot dashboard.

Endpoints
---------
GET  /api/health             -> {"ok": true, "...": ...}
GET  /api/signals            -> list of all parsed signals (history.jsonl + signals.jsonl, deduped)
GET  /api/signals/today      -> signals from "today" in the local market timezone (UTC for now)
GET  /api/stats              -> aggregate counts, distributions, top tickers
GET  /api/stream             -> Server-Sent Events: emits each new line written to signals.jsonl

Run
---
uvicorn server.api:app --reload --port 8787
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from fractions import Fraction
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

log = logging.getLogger("server.api")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = PROJECT_ROOT / "logs"

# Live-signal channel (structured PLAN/TRIGGER/PROFIT).
HISTORY_PATH = LOG_DIR / "history.jsonl"
LIVE_PATH = LOG_DIR / "signals.jsonl"

# Trade-plan channel (free-form swing-trade write-ups).
PLAN_HISTORY_PATH = LOG_DIR / "plans_history.jsonl"
PLAN_LIVE_PATH = LOG_DIR / "plans.jsonl"

# Swing-trade execution channel (structured trade actions).
SWING_HISTORY_PATH = LOG_DIR / "swings_history.jsonl"
SWING_LIVE_PATH = LOG_DIR / "swings.jsonl"

# Executor (DRY_RUN): virtual book snapshot + append-only decision log.
EXECUTOR_BOOK_PATH = LOG_DIR / "virtual_book.json"
EXECUTOR_ORDERS_PATH = LOG_DIR / "proposed_orders.jsonl"
SHADOW_REVIEWS_PATH = LOG_DIR / "robinhood_shadow_reviews.jsonl"


app = FastAPI(title="trade-bot dashboard", version="0.1.0")

# Open CORS for local development -- the React dev server runs on a different port.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# -- I/O helpers --------------------------------------------------------------

def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError as e:
                log.warning("Bad JSONL line in %s: %s", path, e)
    return out


def _load_dedup_sorted(history_path: Path, live_path: Path) -> list[dict[str, Any]]:
    """Load history + live, dedupe by Discord message_id, sort newest first."""
    combined = _read_jsonl(history_path) + _read_jsonl(live_path)

    seen: set[int] = set()
    deduped: list[dict[str, Any]] = []
    for r in combined:
        mid = r.get("discord", {}).get("message_id")
        if mid is None or mid in seen:
            if mid is None:
                deduped.append(r)
            continue
        seen.add(mid)
        deduped.append(r)

    def created_at(r: dict[str, Any]) -> str:
        return r.get("discord", {}).get("created_at") or r.get("received_at") or ""

    deduped.sort(key=created_at, reverse=True)
    return deduped


def _load_all_signals() -> list[dict[str, Any]]:
    return _load_dedup_sorted(HISTORY_PATH, LIVE_PATH)


def _load_all_plans() -> list[dict[str, Any]]:
    return _load_dedup_sorted(PLAN_HISTORY_PATH, PLAN_LIVE_PATH)


def _load_all_swings() -> list[dict[str, Any]]:
    return _load_dedup_sorted(SWING_HISTORY_PATH, SWING_LIVE_PATH)


# -- Swing-trade position folding -------------------------------------------

# Action kinds that change the "open positions" book.
_OPENING_KINDS = {"ENTRY", "ADD"}
_CLOSING_KINDS = {"CLOSE", "STOP_TRIGGER"}


def _action_ts(action: dict[str, Any]) -> str:
    return action.get("discord", {}).get("created_at") or action.get("received_at") or ""


_FRAC_RE = re.compile(r"^(\d+)/(\d+)$")


def _size_to_fraction(value: str | None) -> Fraction | None:
    """
    Parse a position-size string into an exact Fraction.

    Handles:
      "1/3"             -> 1/3
      "+1/4 -> 3/4"     -> 3/4   (right-hand side of arrow == new total)
      "+1/4 → 3/4"      -> 3/4
      "7/8 -> 3/4"      -> 3/4
      "1/8"             -> 1/8   (used for delta_size on REDUCE)
      ""/None/garbage   -> None
    """
    if not value:
        return None
    text = value.replace("→", "->").replace(" ", "")
    if "->" in text:
        text = text.rsplit("->", 1)[1]
    text = text.lstrip("+-")
    m = _FRAC_RE.match(text)
    if not m:
        return None
    num, den = int(m.group(1)), int(m.group(2))
    if den == 0:
        return None
    return Fraction(num, den)


def _format_fraction(f: Fraction) -> str:
    """Format a Fraction as a clean 'p/q' string."""
    return f"{f.numerator}/{f.denominator}"


def _derive_open_positions(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Walk actions in chronological (oldest -> newest) order and produce the
    current set of open positions.

    Algorithm:
      - On ENTRY:    create/replace the position with size, avg_cost, side, stop.
      - On ADD:      update size + avg_cost (we trust the message; the channel
                     emits the new totals).
      - On REDUCE:   update size if the new size is included; otherwise keep.
      - On CLOSE:    drop the position entirely.
      - On STOP_TRIGGER: drop the position (stop hit -> exited).
      - On STOP_UPDATE: update the stop in-place if the position is open.
      - On POSITION_UPDATE: refresh last_price and last_pnl_pct for the
                            position (informational; doesn't open one if
                            we never saw an entry).

    The result is sorted by `last_pnl_pct` desc so winners float to the top.
    """
    by_ticker: dict[str, dict[str, Any]] = {}

    chrono = sorted(actions, key=_action_ts)
    for a in chrono:
        ticker = (a.get("ticker") or "").upper()
        if not ticker:
            continue
        kind = a.get("kind")
        ts = _action_ts(a)

        if kind == "ENTRY":
            by_ticker[ticker] = {
                "ticker": ticker,
                "side": a.get("side"),
                "avg_cost": a.get("price") or a.get("avg_cost"),
                "position_size": a.get("position_size"),
                "position_fraction": a.get("position_fraction"),
                "stop_loss": a.get("stop_loss"),
                "stop_loss_label": a.get("stop_loss_label"),
                "opened_at": ts,
                "last_action_at": ts,
                "last_action_kind": kind,
                "last_price": a.get("price"),
                "last_pnl_pct": None,
            }
        elif kind == "ADD":
            pos = by_ticker.get(ticker)
            if pos is None:
                pos = by_ticker[ticker] = {
                    "ticker": ticker,
                    "side": a.get("side"),
                    "opened_at": ts,
                }
            pos["avg_cost"] = a.get("avg_cost") or pos.get("avg_cost") or a.get("price")
            pos["position_size"] = a.get("position_size") or pos.get("position_size")
            pos["position_fraction"] = a.get("position_fraction") or pos.get("position_fraction")
            pos["last_action_at"] = ts
            pos["last_action_kind"] = kind
            pos["last_price"] = a.get("price") or pos.get("last_price")
        elif kind == "REDUCE":
            # The channel posts the AMOUNT trimmed (delta_size, e.g. "1/8"),
            # not the new total. Compute the new total by subtracting the
            # delta from the running fraction. If we can't (delta missing,
            # or current size unparseable) we leave size as-is so we don't
            # silently lie.
            pos = by_ticker.get(ticker)
            if pos is not None:
                delta = _size_to_fraction(a.get("delta_size"))
                current = _size_to_fraction(pos.get("position_size"))
                if delta is not None and current is not None:
                    new_frac = current - delta
                    if new_frac <= 0:
                        # Trim crossed zero -> treat as fully closed.
                        by_ticker.pop(ticker, None)
                        continue
                    pos["position_size"] = _format_fraction(new_frac)
                    pos["position_fraction"] = float(new_frac)
                pos["last_action_at"] = ts
                pos["last_action_kind"] = kind
                pos["last_pnl_pct"] = a.get("profit_pct")
        elif kind in _CLOSING_KINDS:
            by_ticker.pop(ticker, None)
        elif kind == "STOP_UPDATE":
            pos = by_ticker.get(ticker)
            if pos is not None:
                pos["stop_loss"] = a.get("stop_loss") or pos.get("stop_loss")
                pos["stop_loss_label"] = a.get("stop_loss_label") or pos.get("stop_loss_label")
                pos["last_action_at"] = ts
                pos["last_action_kind"] = kind
        elif kind == "POSITION_UPDATE":
            pos = by_ticker.get(ticker)
            if pos is not None:
                if a.get("price") is not None:
                    pos["last_price"] = a.get("price")
                if a.get("avg_cost") is not None:
                    pos["avg_cost"] = a.get("avg_cost")
                if a.get("profit_pct") is not None:
                    pos["last_pnl_pct"] = a.get("profit_pct")
                pos["last_action_at"] = ts
                pos["last_action_kind"] = kind

    positions = list(by_ticker.values())
    positions.sort(
        key=lambda p: (p.get("last_pnl_pct") if p.get("last_pnl_pct") is not None else -1e9),
        reverse=True,
    )
    return positions


# -- Endpoints ---------------------------------------------------------------

def _file_size(path: Path) -> int:
    return path.stat().st_size if path.exists() else 0


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "signals": {
            "history_exists": HISTORY_PATH.exists(),
            "live_exists": LIVE_PATH.exists(),
            "history_size_bytes": _file_size(HISTORY_PATH),
            "live_size_bytes": _file_size(LIVE_PATH),
        },
        "plans": {
            "history_exists": PLAN_HISTORY_PATH.exists(),
            "live_exists": PLAN_LIVE_PATH.exists(),
            "history_size_bytes": _file_size(PLAN_HISTORY_PATH),
            "live_size_bytes": _file_size(PLAN_LIVE_PATH),
        },
        "swings": {
            "history_exists": SWING_HISTORY_PATH.exists(),
            "live_exists": SWING_LIVE_PATH.exists(),
            "history_size_bytes": _file_size(SWING_HISTORY_PATH),
            "live_size_bytes": _file_size(SWING_LIVE_PATH),
        },
    }


@app.get("/api/signals")
def list_signals(
    limit: int = Query(default=500, ge=1, le=10_000),
    kind: str | None = Query(default=None, description="Filter by PLAN/TRIGGER/PROFIT"),
    ticker: str | None = Query(default=None, description="Filter by ticker (case-insensitive)"),
) -> dict[str, Any]:
    rows = _load_all_signals()
    if kind:
        rows = [r for r in rows if r.get("kind") == kind.upper()]
    if ticker:
        t = ticker.upper()
        rows = [r for r in rows if r.get("ticker", "").upper() == t]
    return {"count": len(rows), "signals": rows[:limit]}


@app.get("/api/plans")
def list_plans(
    limit: int = Query(default=500, ge=1, le=10_000),
    ticker: str | None = Query(default=None, description="Filter by ticker (case-insensitive)"),
) -> dict[str, Any]:
    rows = _load_all_plans()
    if ticker:
        t = ticker.upper()
        rows = [r for r in rows if (r.get("ticker") or "").upper() == t]
    return {"count": len(rows), "plans": rows[:limit]}


@app.get("/api/swings")
def list_swings(
    limit: int = Query(default=1000, ge=1, le=10_000),
    kind: str | None = Query(default=None, description="ENTRY/ADD/REDUCE/CLOSE/STOP_TRIGGER/STOP_UPDATE/POSITION_UPDATE"),
    ticker: str | None = Query(default=None, description="Filter by ticker (case-insensitive)"),
    actionable_only: bool = Query(default=False, description="Drop POSITION_UPDATE/STOP_UPDATE rows"),
) -> dict[str, Any]:
    rows = _load_all_swings()
    if kind:
        rows = [r for r in rows if r.get("kind") == kind.upper()]
    if ticker:
        t = ticker.upper()
        rows = [r for r in rows if (r.get("ticker") or "").upper() == t]
    if actionable_only:
        rows = [r for r in rows if r.get("kind") not in ("POSITION_UPDATE", "STOP_UPDATE")]

    # Open-positions snapshot is derived from the FULL history regardless of
    # filter, so the "current book" reflects reality even while the user
    # filters the action feed.
    positions = _derive_open_positions(_load_all_swings())

    return {
        "count": len(rows),
        "actions": rows[:limit],
        "open_positions": positions,
    }


@app.get("/api/stats")
def stats() -> dict[str, Any]:
    rows = _load_all_signals()

    by_kind: Counter[str] = Counter(r.get("kind", "?") for r in rows)
    by_side: Counter[str] = Counter(r.get("side") or "UNK" for r in rows)
    by_ticker_kind: dict[str, Counter[str]] = defaultdict(Counter)
    triggers_per_ticker: dict[str, list[float]] = defaultdict(list)
    timestamps: list[datetime] = []
    by_hour: Counter[int] = Counter()
    by_day: Counter[str] = Counter()

    for r in rows:
        ticker = r.get("ticker", "?")
        kind = r.get("kind", "?")
        by_ticker_kind[ticker][kind] += 1
        if kind == "TRIGGER" and r.get("trigger") is not None:
            triggers_per_ticker[ticker].append(r["trigger"])
        ts_raw = r.get("discord", {}).get("created_at")
        if ts_raw:
            try:
                ts = datetime.fromisoformat(ts_raw)
                timestamps.append(ts)
                by_hour[ts.hour] += 1
                by_day[ts.date().isoformat()] += 1
            except ValueError:
                pass

    has_target = sum(
        1 for r in rows if r.get("kind") in ("PLAN", "TRIGGER") and r.get("target") is not None
    )
    no_target = sum(
        1 for r in rows if r.get("kind") in ("PLAN", "TRIGGER") and r.get("target") is None
    )

    earliest = min(timestamps).isoformat() if timestamps else None
    latest = max(timestamps).isoformat() if timestamps else None

    # Today (UTC) -- the listener stamps Discord-side UTC times.
    today_utc = datetime.now(timezone.utc).date().isoformat()
    today_count = by_day.get(today_utc, 0)

    return {
        "total": len(rows),
        "by_kind": dict(by_kind),
        "by_side": dict(by_side),
        "earliest": earliest,
        "latest": latest,
        "today_count": today_count,
        "today_date_utc": today_utc,
        "has_target": has_target,
        "no_target": no_target,
        "by_hour_utc": [{"hour": h, "count": by_hour.get(h, 0)} for h in range(24)],
        "by_day": [{"date": d, "count": c} for d, c in sorted(by_day.items())],
        "top_tickers": [
            {
                "ticker": t,
                "total": sum(c.values()),
                "trigger": c.get("TRIGGER", 0),
                "plan": c.get("PLAN", 0),
                "profit": c.get("PROFIT", 0),
            }
            for t, c in sorted(
                by_ticker_kind.items(), key=lambda kv: -sum(kv[1].values())
            )[:15]
        ],
        "trigger_prices": [
            {"ticker": t, "prices": sorted(set(prices))}
            for t, prices in sorted(
                triggers_per_ticker.items(), key=lambda kv: -len(kv[1])
            )[:10]
        ],
    }


# -- Live stream (SSE over a tailed JSONL file) -------------------------------

async def _tail_jsonl(path: Path, poll_interval: float = 0.5) -> AsyncIterator[dict[str, Any]]:
    """
    Yield each new JSON record appended to `path`.

    Implementation: track byte offset, on each tick read from offset to EOF,
    parse complete lines, yield. Robust to file rotation (offset reset to 0
    when file shrinks). No external dependencies.
    """
    offset = path.stat().st_size if path.exists() else 0
    buf = ""
    while True:
        await asyncio.sleep(poll_interval)
        if not path.exists():
            offset = 0
            continue
        size = path.stat().st_size
        if size < offset:
            # File was truncated/rotated -- start over.
            offset = 0
            buf = ""
        if size == offset:
            continue
        with path.open("r", encoding="utf-8") as fh:
            fh.seek(offset)
            chunk = fh.read()
            offset = fh.tell()
        buf += chunk
        while "\n" in buf:
            line, _, buf = buf.partition("\n")
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as e:
                log.warning("Bad JSONL line on tail: %s", e)


def _sse_event(data: dict[str, Any], event: str | None = None) -> bytes:
    """Format a single SSE event."""
    parts: list[str] = []
    if event:
        parts.append(f"event: {event}")
    parts.append("data: " + json.dumps(data, ensure_ascii=False))
    parts.append("")  # trailing blank line
    return ("\n".join(parts) + "\n").encode("utf-8")


def _sse_response(path: Path, event_name: str) -> StreamingResponse:
    async def event_source() -> AsyncIterator[bytes]:
        yield _sse_event(
            {"connected_at": datetime.now(timezone.utc).isoformat(), "source": str(path.name)},
            event="hello",
        )
        last_heartbeat = asyncio.get_event_loop().time()
        async for record in _tail_jsonl(path):
            yield _sse_event(record, event=event_name)
            now = asyncio.get_event_loop().time()
            if now - last_heartbeat > 15:
                yield _sse_event(
                    {"ts": datetime.now(timezone.utc).isoformat()}, event="heartbeat"
                )
                last_heartbeat = now

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.get("/api/stream")
async def stream_signals() -> StreamingResponse:
    """SSE feed for new live signals (signals.jsonl)."""
    return _sse_response(LIVE_PATH, event_name="signal")


@app.get("/api/plans/stream")
async def stream_plans() -> StreamingResponse:
    """SSE feed for new trade plans (plans.jsonl)."""
    return _sse_response(PLAN_LIVE_PATH, event_name="plan")


@app.get("/api/swings/stream")
async def stream_swings() -> StreamingResponse:
    """SSE feed for new swing-trade actions (swings.jsonl)."""
    return _sse_response(SWING_LIVE_PATH, event_name="swing")


# -- Executor endpoints ------------------------------------------------------

@app.get("/api/executor/book")
def executor_book() -> dict[str, Any]:
    """Current virtual book snapshot written by `bot.executor`.

    If the executor hasn't run yet, returns a stub indicating "no book".
    """
    if not EXECUTOR_BOOK_PATH.exists():
        return {
            "present": False,
            "reason": "executor has not run yet — start it with `python -m bot.executor`",
        }
    try:
        text = EXECUTOR_BOOK_PATH.read_text(encoding="utf-8")
        data = json.loads(text)
    except (OSError, json.JSONDecodeError) as e:
        return {"present": False, "reason": f"failed to read book: {e}"}
    data["present"] = True
    return data


@app.get("/api/executor/orders")
def executor_orders(
    limit: int = Query(default=500, ge=1, le=10_000),
    action: str | None = Query(
        default=None,
        description="Filter by action: BUY / SELL / REJECT",
    ),
    ticker: str | None = Query(default=None, description="Filter by ticker (case-insensitive)"),
) -> dict[str, Any]:
    """List proposed orders, newest first."""
    rows = _read_jsonl(EXECUTOR_ORDERS_PATH)
    rows.sort(key=lambda r: r.get("decided_at") or "", reverse=True)
    if action:
        a = action.upper()
        rows = [r for r in rows if (r.get("action") or "").upper() == a]
    if ticker:
        t = ticker.upper()
        rows = [r for r in rows if (r.get("ticker") or "").upper() == t]
    return {"count": len(rows), "orders": rows[:limit]}


@app.get("/api/executor/orders/stream")
async def stream_executor_orders() -> StreamingResponse:
    """SSE feed for new proposed orders (proposed_orders.jsonl)."""
    return _sse_response(EXECUTOR_ORDERS_PATH, event_name="order")


@app.get("/api/executor/shadow-reviews")
def executor_shadow_reviews(
    limit: int = Query(default=500, ge=1, le=10_000),
    status: str | None = Query(default=None, description="REVIEWED / SKIPPED / FAILED"),
    ticker: str | None = Query(default=None, description="Filter by ticker (case-insensitive)"),
) -> dict[str, Any]:
    """List Robinhood shadow-review records, newest first."""
    rows = _read_jsonl(SHADOW_REVIEWS_PATH)
    rows.sort(key=lambda r: r.get("reviewed_at") or "", reverse=True)
    if status:
        s = status.upper()
        rows = [r for r in rows if (r.get("status") or "").upper() == s]
    if ticker:
        t = ticker.upper()
        rows = [r for r in rows if (r.get("ticker") or "").upper() == t]
    return {"count": len(rows), "reviews": rows[:limit]}


@app.get("/api/executor/shadow-reviews/stream")
async def stream_executor_shadow_reviews() -> StreamingResponse:
    """SSE feed for new Robinhood shadow reviews."""
    return _sse_response(SHADOW_REVIEWS_PATH, event_name="shadow-review")


def main() -> None:
    """Convenience runner: `python -m server.api`.

    `reload=True` auto-restarts the API when any file under server/ changes,
    so backend edits show up without restarting the whole dashboard.
    """
    import uvicorn

    uvicorn.run(
        "server.api:app",
        host="127.0.0.1",
        port=8787,
        reload=True,
        reload_dirs=[str(PROJECT_ROOT / "server")],
    )


if __name__ == "__main__":
    main()
