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
import math
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from fractions import Fraction
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from bot.manual_day_plans import cancel_plan, create_plan, load_plans
from bot.heat_ideas import (
    append_jsonl as append_heat_jsonl,
    load_heat_settings,
    load_materialized_heat_ideas,
    save_heat_settings,
)
from bot.leveraged_etfs import candidate_symbols

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

# P&L ledger written by pnl_tracker after each live Robinhood order fill.
PNL_PATH = LOG_DIR / "trade_pnl.jsonl"

# Day trader state (written by bot/day_trader.py).
DAY_TRADE_POSITIONS_PATH = LOG_DIR / "day_trade_positions.jsonl"
MANUAL_DAY_TRADE_PLANS_PATH = PROJECT_ROOT / "state" / "manual_day_trade_plans.json"
HEAT_IDEAS_PATH = LOG_DIR / "heat_ideas.jsonl"
HEAT_ATTACHMENTS_PATH = LOG_DIR / "heat_attachments"
HEAT_DECISIONS_PATH = PROJECT_ROOT / "state" / "heat_idea_decisions.jsonl"
HEAT_SETTINGS_PATH = PROJECT_ROOT / "state" / "heat_settings.json"
# Service PID file written by the day_trader process.
DAY_TRADER_PID_PATH = LOG_DIR / "day_trader.pid"


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


@app.get("/api/pnl")
def list_pnl() -> dict[str, Any]:
    """All P&L records from trade_pnl.jsonl, newest first."""
    records: list[dict[str, Any]] = []
    if PNL_PATH.exists():
        with PNL_PATH.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    records.reverse()

    total_realized = sum(r.get("realized_pnl") or 0.0 for r in records)
    wins = [r for r in records if (r.get("realized_pnl") or 0) > 0]
    losses = [r for r in records if (r.get("realized_pnl") or 0) < 0]

    return {
        "count": len(records),
        "total_realized_pnl": round(total_realized, 4),
        "wins": len(wins),
        "losses": len(losses),
        "records": records,
    }


def _latest_day_trade_positions() -> list[dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    if not DAY_TRADE_POSITIONS_PATH.exists():
        return []
    with DAY_TRADE_POSITIONS_PATH.open("r", encoding="utf-8") as fh:
        for line in fh:
            try:
                rec = json.loads(line)
                latest[rec["id"]] = rec
            except Exception:
                continue
    return list(latest.values())


def _manual_plan_views(positions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    plans = load_plans(MANUAL_DAY_TRADE_PLANS_PATH)
    views: list[dict[str, Any]] = []
    for plan in plans:
        view = dict(plan)
        related = [p for p in positions if p.get("manual_plan_id") == plan.get("id")]
        active = next(
            (p for p in reversed(related) if p.get("status") in {
                "watching", "pending_entry", "open", "pending_exit"
            }),
            None,
        )
        filled = next((p for p in reversed(related) if p.get("fill_qty")), None)
        if plan.get("status") == "cancelled":
            derived_status = "cancelled"
        elif filled:
            derived_status = "executed"
        elif active and active.get("status") == "pending_entry":
            derived_status = "entry_pending"
        elif active and active.get("status") == "watching":
            derived_status = "armed" if active.get("armed") else "waiting_rearm"
        else:
            ticker = str(plan.get("ticker", "")).upper()
            conflict = any(
                p.get("ticker") == ticker
                and p.get("manual_plan_id") != plan.get("id")
                and p.get("status") in {"watching", "pending_entry", "open", "pending_exit"}
                for p in positions
            )
            derived_status = "blocked_conflict" if conflict else "queued"
        view["derived_status"] = derived_status
        view["position_id"] = (active or filled or {}).get("id")
        views.append(view)
    return sorted(views, key=lambda item: item.get("created_at") or "", reverse=True)


def _heat_idea_views(positions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ideas = load_materialized_heat_ideas(HEAT_IDEAS_PATH, HEAT_DECISIONS_PATH)
    enabled = bool(load_heat_settings(HEAT_SETTINGS_PATH).get("auto_trading_enabled"))
    for idea in ideas:
        related = [p for p in positions if p.get("heat_idea_id") == idea.get("id")]
        position = next((p for p in reversed(related) if p.get("status") in {
            "watching", "pending_entry", "open", "pending_exit", "closed", "expired"
        }), None)
        has_execution = any(
            p.get("fill_qty") or p.get("status") in {"open", "pending_exit", "closed"}
            for p in related
        )
        if idea.get("decision") == "rejected" and not has_execution:
            idea["derived_status"] = "rejected"
            if position:
                idea["position_id"] = position.get("id")
                idea["position_status"] = position.get("status")
        elif position:
            idea["position_id"] = position.get("id")
            idea["position_status"] = position.get("status")
            if position.get("fill_qty") or position.get("status") in {"open", "pending_exit", "closed"}:
                idea["derived_status"] = "executed"
            elif position.get("status") == "expired":
                idea["derived_status"] = position.get("exit_reason") or "expired"
            elif position.get("status") == "pending_entry":
                idea["derived_status"] = "entry_pending"
            else:
                idea["derived_status"] = "armed" if position.get("armed") else "waiting_rearm"
        elif idea.get("decision") == "approved":
            idea["derived_status"] = (
                "unsupported_mapping"
                if not idea.get("mapping_supported")
                else ("queued" if enabled else "paused")
            )
        else:
            classification = str(idea.get("classification") or "needs_level")
            idea["derived_status"] = (
                "needs_review"
                if classification == "actionable_setup"
                else classification
            )
        idea["attachment_urls"] = [
            f"/api/daytrader/heat-attachments/{name}"
            for name in idea.get("attachments") or []
        ]

    latest_tickers: set[str] = set()
    for index, idea in enumerate(ideas):
        ticker = str(idea.get("ticker") or "").upper()
        idea["is_latest_for_ticker"] = ticker not in latest_tickers
        latest_tickers.add(ticker)
        earlier = [
            previous
            for previous in ideas[index + 1:]
            if str(previous.get("ticker") or "").upper() == ticker
        ]
        idea["ticker_history"] = [
            {
                "id": previous.get("id"),
                "ticker": previous.get("ticker"),
                "text": previous.get("text"),
                "classification": previous.get("classification"),
                "trigger_price": previous.get("trigger_price"),
                "derived_status": previous.get("derived_status"),
                "created_at": previous.get("created_at"),
                "attachment_urls": previous.get("attachment_urls") or [],
            }
            for previous in earlier[:5]
        ]
        idea["history_count"] = len(earlier)
    return ideas


class ManualDayPlanRequest(BaseModel):
    ticker: str
    trigger_price: float
    target_price: float | None = None
    setup: str | None = None


class HeatIdeaDecisionRequest(BaseModel):
    ticker: str
    trigger_price: float
    target_price: float | None = None
    setup: str | None = None
    direction: str = "long"
    trigger_operator: str = "above"


class HeatSettingsRequest(BaseModel):
    auto_trading_enabled: bool


@app.post("/api/daytrader/manual-plans")
def add_manual_day_plan(request: ManualDayPlanRequest) -> dict[str, Any]:
    ticker = request.ticker.strip().upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9.-]{0,9}", ticker):
        raise HTTPException(status_code=422, detail="invalid ticker")
    if not math.isfinite(request.trigger_price) or request.trigger_price <= 0:
        raise HTTPException(status_code=422, detail="trigger price must be positive")
    if request.target_price is not None and (
        not math.isfinite(request.target_price) or request.target_price <= 0
    ):
        raise HTTPException(status_code=422, detail="target price must be positive")
    if request.setup is not None and len(request.setup.strip()) > 500:
        raise HTTPException(status_code=422, detail="setup must be 500 characters or less")
    positions = _latest_day_trade_positions()
    consumed_plan_ids = {
        str(p.get("manual_plan_id"))
        for p in positions
        if p.get("manual_plan_id") and p.get("fill_qty")
    }
    try:
        plan = create_plan(
            ticker,
            request.trigger_price,
            request.target_price,
            request.setup,
            path=MANUAL_DAY_TRADE_PLANS_PATH,
            consumed_plan_ids=consumed_plan_ids,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return next(
        view for view in _manual_plan_views(positions) if view.get("id") == plan["id"]
    )


@app.delete("/api/daytrader/manual-plans/{plan_id}")
def remove_manual_day_plan(plan_id: str) -> dict[str, Any]:
    positions = _latest_day_trade_positions()
    if any(
        p.get("manual_plan_id") == plan_id
        and (p.get("fill_qty") or p.get("status") in {"open", "pending_exit", "closed"})
        for p in positions
    ):
        raise HTTPException(
            status_code=409,
            detail="watch already executed; manage the position instead",
        )
    plan = cancel_plan(plan_id, path=MANUAL_DAY_TRADE_PLANS_PATH)
    if plan is None:
        raise HTTPException(status_code=404, detail="manual watch not found")
    return plan


def _find_heat_idea(idea_id: str) -> dict[str, Any] | None:
    return next(
        (item for item in load_materialized_heat_ideas(
            HEAT_IDEAS_PATH, HEAT_DECISIONS_PATH
        ) if str(item.get("id")) == idea_id),
        None,
    )


@app.post("/api/daytrader/heat-ideas/{idea_id}/approve")
def approve_heat_idea(idea_id: str, request: HeatIdeaDecisionRequest) -> dict[str, Any]:
    idea = _find_heat_idea(idea_id)
    if idea is None:
        raise HTTPException(status_code=404, detail="Heat idea not found")
    ticker = request.ticker.strip().upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9.-]{0,9}", ticker):
        raise HTTPException(status_code=422, detail="invalid ticker")
    if not math.isfinite(request.trigger_price) or request.trigger_price <= 0:
        raise HTTPException(status_code=422, detail="trigger price must be positive")
    if request.target_price is not None and (
        not math.isfinite(request.target_price) or request.target_price <= 0
    ):
        raise HTTPException(status_code=422, detail="target price must be positive")
    setup = request.setup.strip() if request.setup else None
    if setup and len(setup) > 500:
        raise HTTPException(status_code=422, detail="setup must be 500 characters or less")
    direction = request.direction.strip().lower()
    trigger_operator = request.trigger_operator.strip().lower()
    if direction not in {"long", "short"}:
        raise HTTPException(status_code=422, detail="direction must be long or short")
    if trigger_operator not in {"above", "below"}:
        raise HTTPException(status_code=422, detail="trigger operator must be above or below")
    candidates = candidate_symbols(ticker, direction)
    if not candidates:
        raise HTTPException(
            status_code=422,
            detail=f"no supported execution route for {ticker} {direction}",
        )
    append_heat_jsonl(HEAT_DECISIONS_PATH, {
        "idea_id": idea_id,
        "decision": "approved",
        "ticker": ticker,
        "trigger_price": request.trigger_price,
        "target_price": request.target_price,
        "setup": setup or idea.get("setup") or "Heat breakout watch",
        "direction": direction,
        "trigger_operator": trigger_operator,
        "good_til_cancelled": True,
        "decided_at": datetime.now(timezone.utc).isoformat(),
    })
    return _find_heat_idea(idea_id) or {}


@app.post("/api/daytrader/heat-ideas/{idea_id}/reject")
def reject_heat_idea(idea_id: str) -> dict[str, Any]:
    idea = _find_heat_idea(idea_id)
    if idea is None:
        raise HTTPException(status_code=404, detail="Heat idea not found")
    positions = _latest_day_trade_positions()
    if any(
        p.get("heat_idea_id") == idea_id
        and (p.get("fill_qty") or p.get("status") in {"open", "pending_exit", "closed"})
        for p in positions
    ):
        raise HTTPException(status_code=409, detail="Heat idea already executed")
    append_heat_jsonl(HEAT_DECISIONS_PATH, {
        "idea_id": idea_id,
        "decision": "rejected",
        "decided_at": datetime.now(timezone.utc).isoformat(),
    })
    return _find_heat_idea(idea_id) or {}


@app.put("/api/daytrader/heat-settings")
def update_heat_settings(request: HeatSettingsRequest) -> dict[str, Any]:
    return save_heat_settings(request.auto_trading_enabled, HEAT_SETTINGS_PATH)


@app.get("/api/daytrader/heat-attachments/{filename}")
def get_heat_attachment(filename: str) -> FileResponse:
    if filename != Path(filename).name:
        raise HTTPException(status_code=404, detail="attachment not found")
    path = HEAT_ATTACHMENTS_PATH / filename
    if not path.is_file() or path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
        raise HTTPException(status_code=404, detail="attachment not found")
    return FileResponse(path)


@app.get("/api/daytrader")
def get_daytrader_state() -> dict[str, Any]:
    """Return all day-trade positions (watching/open/closed) and P&L summary."""

    # Check if day_trader service is running.
    # The day_trader writes a heartbeat timestamp to day_trader.heartbeat every
    # poll cycle. If the heartbeat is < 5 min old the service is considered live.
    # Fall back to PID file existence if heartbeat is absent.
    import time as _time
    HEARTBEAT_PATH = DAY_TRADE_POSITIONS_PATH.parent / "day_trader.heartbeat"
    service_running = False
    if HEARTBEAT_PATH.exists():
        try:
            age_s = _time.time() - HEARTBEAT_PATH.stat().st_mtime
            service_running = age_s < 300  # alive if touched within 5 min
        except Exception:
            pass
    elif DAY_TRADER_PID_PATH.exists():
        # Heartbeat not written yet (first startup); trust PID file existence
        service_running = True

    positions = _latest_day_trade_positions()

    # Build P&L summary from closed positions
    closed = [p for p in positions if p.get("status") == "closed" and p.get("realized_pnl") is not None]
    today_str = datetime.now(timezone.utc).date().isoformat()
    closed_today = [p for p in closed if (p.get("closed_at") or "").startswith(today_str)]
    total_pnl = sum(p.get("realized_pnl") or 0 for p in closed_today)
    wins = len([p for p in closed_today if (p.get("realized_pnl") or 0) > 0])
    losses = len([p for p in closed_today if (p.get("realized_pnl") or 0) < 0])

    pnl_records = [
        {
            "id": p.get("id"),
            "ticker": p.get("ticker"),
            "setup": p.get("setup"),
            "fill_price": p.get("fill_price"),
            "exit_price": p.get("exit_price"),
            "realized_pnl": p.get("realized_pnl"),
            "realized_pnl_pct": p.get("realized_pnl_pct"),
            "exit_reason": p.get("exit_reason"),
            "closed_at": p.get("closed_at"),
        }
        for p in sorted(closed, key=lambda x: x.get("closed_at") or "", reverse=True)
    ]

    return {
        "positions": positions,
        "pnl": {
            "total_realized_pnl": round(total_pnl, 4),
            "wins": wins,
            "losses": losses,
            "trades_today": len(closed_today),
            "records": pnl_records,
        },
        "service_running": service_running,
        "manual_plans": _manual_plan_views(positions),
        "heat_ideas": _heat_idea_views(positions),
        "heat_settings": load_heat_settings(HEAT_SETTINGS_PATH),
    }


# ── Chat agent ────────────────────────────────────────────────────────────────

# In-memory session store: session_id -> list of {role, content} dicts.
# Lives in the API process; sessions are lost on API restart (acceptable for now).
_CHAT_SESSIONS: dict[str, list[dict[str, str]]] = {}


_NOISE_LINE_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T|ERROR rmcp|\x1b\[|^tokens used$|^reading additional"
)


def _extract_clean_answer(raw: str) -> str:
    """Return only the final answer text from raw codex exec output.

    With unbuffer codex outputs:
        codex
        <FINAL ANSWER>
        tokens used
        <count>
        [optional session-delete error line]

    We grab the last "codex\\n" section, cut at "tokens used", and strip noise.
    """
    # Primary: last "\ncodex\n" block, cut before "tokens used"
    marker = "\ncodex\n"
    idx = raw.rfind(marker)
    if idx != -1:
        block = raw[idx + len(marker):]
        tu = block.find("\ntokens used")
        if tu != -1:
            block = block[:tu]
        # Strip any trailing noise lines (timestamps, errors)
        lines = [
            ln for ln in block.split("\n")
            if not _NOISE_LINE_RE.search(ln.strip())
        ]
        result = "\n".join(lines).strip()
        if result:
            return result

    # Fallback: everything after "tokens used\n<count>\n", minus noise
    tu_idx = raw.find("tokens used\n")
    if tu_idx != -1:
        after = raw[tu_idx + len("tokens used\n"):]
        lines = after.split("\n")
        # Skip the digit token-count line(s) then collect clean content
        past_count = False
        clean: list[str] = []
        for ln in lines:
            cleaned = ln.strip().replace(",", "")
            if not past_count and (cleaned.isdigit() or cleaned == ""):
                continue
            past_count = True
            if _NOISE_LINE_RE.search(ln.strip()):
                continue
            clean.append(ln)
        result = "\n".join(clean).strip()
        if result:
            return result

    return raw.strip()


class _ChatRequest(BaseModel):
    session_id: str
    message: str
    confirmed: bool = False


@app.post("/api/chat/message")
async def chat_message(req: _ChatRequest) -> StreamingResponse:
    """Stream a Codex CLI response as SSE.

    Each SSE event has ``event: chat`` and a JSON ``data`` payload with shape:
      {"type": "chunk",         "text": "..."}  -- streamed text fragment
      {"type": "proposed_order","order": {...}}  -- parsed PROPOSED_ORDER tag
      {"type": "order_placed",  "order_id": "..."} -- parsed BROKER_ORDER_ID tag
      {"type": "done"}                           -- stream finished
    """
    from bot.chat_agent import build_prompt, stream_codex_response  # lazy import

    session = _CHAT_SESSIONS.setdefault(req.session_id, [])
    # Append user turn to history before building prompt
    session.append({"role": "user", "content": req.message})

    prompt = build_prompt(req.message, session[:-1], confirmed=req.confirmed)

    async def event_gen() -> AsyncIterator[bytes]:
        full_chunks: list[str] = []

        try:
            async for chunk in stream_codex_response(prompt):
                full_chunks.append(chunk)
                yield _sse_event({"type": "chunk", "text": chunk}, event="chat")
        except Exception as exc:
            yield _sse_event({"type": "chunk", "text": f"\n[error: {exc}]"}, event="chat")

        full_text = "".join(full_chunks)
        # Extract only the clean answer to save in history.  Saving the raw
        # codex output (which includes the full prompt echo) would cause the
        # conversation history to snowball exponentially with each turn.
        session.append({"role": "assistant", "content": _extract_clean_answer(full_text)})

        # Surface any structured tags — only look at the assistant's reply (after
        # the last "User:" echo in the codex output) to avoid matching the
        # PROPOSED_ORDER example line inside the prompt template.
        response_text = full_text
        last_user = full_text.rfind("User: ")
        if last_user != -1:
            response_text = full_text[last_user:]

        proposed_match = re.search(r"PROPOSED_ORDER=(\{[^\n]+\})", response_text)
        if proposed_match:
            try:
                order = json.loads(proposed_match.group(1))
                # Skip the placeholder example from the RULES section
                if order.get("ticker") not in ("X", ""):
                    yield _sse_event({"type": "proposed_order", "order": order}, event="chat")
            except Exception:
                pass

        broker_match = re.search(r"BROKER_ORDER_ID=([a-f0-9\-]{36})", full_text)
        if broker_match:
            yield _sse_event(
                {"type": "order_placed", "order_id": broker_match.group(1)}, event="chat"
            )

        yield _sse_event({"type": "done"}, event="chat")

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


@app.get("/api/chat/history/{session_id}")
async def chat_history(session_id: str) -> dict[str, Any]:
    """Return the full message history for a session."""
    return {"messages": _CHAT_SESSIONS.get(session_id, [])}


@app.delete("/api/chat/history/{session_id}")
async def chat_clear(session_id: str) -> dict[str, Any]:
    """Clear a session's history."""
    _CHAT_SESSIONS.pop(session_id, None)
    return {"ok": True}


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
