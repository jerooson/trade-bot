"""Chat agent for the trade-bot dashboard.

Builds prompts with live context (swing positions, day trades, recent signals)
and streams Codex CLI responses back as async text chunks.

Codex CLI handles all MCP tool calls internally (get_equity_quotes,
get_equity_positions, place_equity_order, etc.) — we just stream its output.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = PROJECT_ROOT / "logs"

PROPOSED_ORDER_RE = re.compile(r"PROPOSED_ORDER=(\{[^\n]+\})")
BROKER_ORDER_RE = re.compile(r"BROKER_ORDER_ID=([a-f0-9\-]{36})")


# ---------------------------------------------------------------------------
# Context loader
# ---------------------------------------------------------------------------

def _load_context(log_dir: Path = LOG_DIR) -> dict[str, Any]:
    """Load live context: swing positions, day trades, recent signals."""
    ctx: dict[str, Any] = {}

    # Swing open positions from virtual book
    book_path = log_dir / "virtual_book.json"
    if book_path.exists():
        try:
            book = json.loads(book_path.read_text())
            positions = book.get("positions", {})
            ctx["swing_positions"] = {
                k: v for k, v in positions.items()
                if v.get("status") == "open"
            }
        except Exception:
            ctx["swing_positions"] = {}
    else:
        ctx["swing_positions"] = {}

    # Active day trade positions
    dt_path = log_dir / "day_trade_positions.jsonl"
    all_recs: dict[str, Any] = {}
    if dt_path.exists():
        for line in dt_path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                all_recs[rec["id"]] = rec
            except Exception:
                pass
    ctx["day_trades"] = [
        v for v in all_recs.values()
        if v.get("status") in ("open", "watching")
    ]

    # Recent signals today (last 10)
    today = datetime.now(timezone.utc).date().isoformat()
    sig_path = log_dir / "signals.jsonl"
    recent: list[dict[str, Any]] = []
    if sig_path.exists():
        for line in sig_path.read_text().splitlines()[-100:]:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                if rec.get("received_at", "").startswith(today):
                    recent.append(rec)
            except Exception:
                pass
    ctx["recent_signals"] = recent[-10:]

    return ctx


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------

def build_prompt(
    user_message: str,
    history: list[dict[str, str]],
    confirmed: bool = False,
) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    ctx = _load_context()

    # Swing positions block
    swing_text = "  (none)"
    if ctx["swing_positions"]:
        lines = []
        for ticker, pos in ctx["swing_positions"].items():
            shares = pos.get("shares", 0)
            avg = pos.get("avg_cost") or pos.get("average_cost") or 0
            lines.append(f"  {ticker}: {shares} shares @ ${float(avg):.2f} avg cost")
        swing_text = "\n".join(lines)

    # Day trades block
    dt_text = "  (none)"
    if ctx["day_trades"]:
        lines = []
        for dt in ctx["day_trades"]:
            ticker = dt.get("ticker", "?")
            status = dt.get("status", "?")
            current = dt.get("current_price") or dt.get("fill_price") or "?"
            stop = dt.get("stop_price") or "?"
            pnl = dt.get("unrealized_pnl")
            pnl_str = f"  unrealized=${pnl:.2f}" if isinstance(pnl, (int, float)) else ""
            lines.append(f"  {ticker}: {status}, price=${current}, stop=${stop}{pnl_str}")
        dt_text = "\n".join(lines)

    # Recent signals block
    sig_text = "  (none today)"
    if ctx["recent_signals"]:
        sigs = []
        for s in ctx["recent_signals"][-5:]:
            ticker = s.get("ticker", "?")
            kind = s.get("kind", "?")
            price = s.get("price") or s.get("trigger_price") or "?"
            sigs.append(f"  {ticker} {kind} @ {price}")
        sig_text = "\n".join(sigs)

    # Conversation history block
    history_lines: list[str] = []
    for msg in history[-12:]:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        prefix = "User" if role == "user" else "Assistant"
        history_lines.append(f"{prefix}: {content}")
    history_text = "\n".join(history_lines) if history_lines else "(new conversation)"

    confirmed_note = ""
    if confirmed:
        confirmed_note = (
            "\n[CONFIRMED] The user has explicitly confirmed the proposed order above. "
            "You must now call place_equity_order to execute it. Do not ask again."
        )

    return f"""You are a trading assistant for a personal Robinhood account managed by a Discord-following trade bot running on a VPS.

You have Robinhood MCP access with these tools:
- get_accounts           → buying power, account value
- get_equity_positions   → current Robinhood positions
- get_equity_quotes      → live price for any ticker
- place_equity_order     → place market/limit/stop orders
- get_equity_orders      → recent order history

CURRENT TIME: {now}

OPEN SWING POSITIONS (from virtual book):
{swing_text}

ACTIVE DAY TRADES:
{dt_text}

RECENT SIGNALS TODAY:
{sig_text}

CONVERSATION SO FAR:
{history_text}{confirmed_note}

User: {user_message}

RULES:
1. Always use MCP tools to get live data before answering price or account questions.
2. For any proposed trade action, first use get_equity_quotes to verify the current price,
   then describe the order clearly, then emit EXACTLY on its own line:
   PROPOSED_ORDER={{"ticker":"X","action":"BUY","dollar_amount":50.00,"rationale":"brief reason"}}
3. ONLY call place_equity_order when [CONFIRMED] appears above. Never execute without it.
4. After a successful placement, emit EXACTLY on its own line:
   BROKER_ORDER_ID=<the uuid returned by Robinhood>
5. Be concise and factual. Prefer numbers over prose.
6. For debug questions, use get_equity_orders and get_equity_positions to check real state.
"""


# ---------------------------------------------------------------------------
# Codex subprocess streamer
# ---------------------------------------------------------------------------

_CODEX_JS = "/usr/local/lib/node_modules/@openai/codex/bin/codex.js"


async def stream_codex_response(
    prompt: str,
    codex_command: str = "codex",
    timeout: float = 120.0,
) -> AsyncIterator[str]:
    """Run `codex exec --ephemeral --profile trade-bot <prompt>` and yield stdout chunks."""
    # Build the core codex command. We call the JS file directly via node so
    # Node.js resolves package.json with "type":"module" (avoids symlink issues
    # in Docker). We also need --skip-git-repo-check (no git repo at /app) and
    # --dangerously-bypass-approvals-and-sandbox (auto-approve MCP tool calls).
    codex_args = [
        "exec", "--ephemeral", "--profile", "trade-bot",
        "--skip-git-repo-check",
        "--dangerously-bypass-approvals-and-sandbox",
        prompt,
    ]
    node_cmd = ["node", _CODEX_JS] if os.path.exists(_CODEX_JS) else [codex_command]

    # `unbuffer` (from the `expect` package) forces line-buffered stdout so
    # chunks stream in real-time even though stdout is a pipe, not a TTY.
    unbuffer = shutil.which("unbuffer")
    if unbuffer:
        cmd = [unbuffer, *node_cmd, *codex_args]
    else:
        cmd = [*node_cmd, *codex_args]

    env = {**os.environ, "HOME": os.path.expanduser("~")}

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.DEVNULL,   # prevent codex from waiting on stdin
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,   # merge stderr so we see tool errors too
        env=env,
    )

    assert proc.stdout is not None

    try:
        async with asyncio.timeout(timeout):
            while True:
                chunk = await proc.stdout.read(256)
                if not chunk:
                    break
                yield chunk.decode("utf-8", errors="replace")
    except asyncio.TimeoutError:
        proc.kill()
        yield "\n\n[agent timed out after 120s]"
    finally:
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            proc.kill()
