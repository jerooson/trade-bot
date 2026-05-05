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
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

log = logging.getLogger("server.api")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = PROJECT_ROOT / "logs"
HISTORY_PATH = LOG_DIR / "history.jsonl"
LIVE_PATH = LOG_DIR / "signals.jsonl"


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


def _load_all_signals() -> list[dict[str, Any]]:
    """Load history + live, dedupe by Discord message_id, sort newest first."""
    combined = _read_jsonl(HISTORY_PATH) + _read_jsonl(LIVE_PATH)

    seen: set[int] = set()
    deduped: list[dict[str, Any]] = []
    for r in combined:
        mid = r.get("discord", {}).get("message_id")
        if mid is None or mid in seen:
            if mid is None:
                # Records without a message_id: keep them, but they can't be deduped.
                deduped.append(r)
            continue
        seen.add(mid)
        deduped.append(r)

    def created_at(r: dict[str, Any]) -> str:
        return r.get("discord", {}).get("created_at") or r.get("received_at") or ""

    deduped.sort(key=created_at, reverse=True)
    return deduped


# -- Endpoints ---------------------------------------------------------------

@app.get("/api/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "history_exists": HISTORY_PATH.exists(),
        "live_exists": LIVE_PATH.exists(),
        "history_size_bytes": HISTORY_PATH.stat().st_size if HISTORY_PATH.exists() else 0,
        "live_size_bytes": LIVE_PATH.stat().st_size if LIVE_PATH.exists() else 0,
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


# -- Live stream (SSE over a tail of signals.jsonl) ---------------------------

async def _tail_signals(poll_interval: float = 0.5) -> AsyncIterator[dict[str, Any]]:
    """
    Yield each new JSON record appended to signals.jsonl.

    Implementation: track byte offset, on each tick read from offset to EOF,
    parse complete lines, yield. Robust to file rotation (offset reset to 0
    when file shrinks). No external dependencies.
    """
    offset = LIVE_PATH.stat().st_size if LIVE_PATH.exists() else 0
    buf = ""
    while True:
        await asyncio.sleep(poll_interval)
        if not LIVE_PATH.exists():
            offset = 0
            continue
        size = LIVE_PATH.stat().st_size
        if size < offset:
            # File was truncated/rotated -- start over.
            offset = 0
            buf = ""
        if size == offset:
            continue
        with LIVE_PATH.open("r", encoding="utf-8") as fh:
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


@app.get("/api/stream")
async def stream() -> StreamingResponse:
    async def event_source() -> AsyncIterator[bytes]:
        # Initial hello so the client knows the connection is alive.
        yield _sse_event(
            {"connected_at": datetime.now(timezone.utc).isoformat()},
            event="hello",
        )
        last_heartbeat = asyncio.get_event_loop().time()
        async for record in _tail_signals():
            yield _sse_event(record, event="signal")
            now = asyncio.get_event_loop().time()
            if now - last_heartbeat > 15:
                yield _sse_event({"ts": datetime.now(timezone.utc).isoformat()}, event="heartbeat")
                last_heartbeat = now

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",  # disable buffering on nginx-style proxies
            "Connection": "keep-alive",
        },
    )


def main() -> None:
    """Convenience runner: `python -m server.api`."""
    import uvicorn

    uvicorn.run("server.api:app", host="127.0.0.1", port=8787, reload=False)


if __name__ == "__main__":
    main()
