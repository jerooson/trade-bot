"""Chat agent for the trade-bot dashboard.

Builds prompts with live context (swing positions, day-trade plans, recent
signals) and streams Codex CLI responses back as async text chunks.

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

from bot.manual_day_plans import load_plans

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = PROJECT_ROOT / "logs"
MANUAL_DAY_TRADE_PLANS_PATH = PROJECT_ROOT / "state" / "manual_day_trade_plans.json"

PROPOSED_ORDER_RE = re.compile(r"PROPOSED_ORDER=(\{[^\n]+\})")
BROKER_ORDER_RE = re.compile(r"BROKER_ORDER_ID=([a-f0-9\-]{36})")


# ---------------------------------------------------------------------------
# Context loader
# ---------------------------------------------------------------------------

def _load_context(
    log_dir: Path = LOG_DIR,
    manual_plans_path: Path = MANUAL_DAY_TRADE_PLANS_PATH,
) -> dict[str, Any]:
    """Load VPS context: swing positions, day-trade plans, and signals."""
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
    active_statuses = {"open", "watching", "pending_entry", "pending_exit"}
    active_day_trades = [
        v for v in all_recs.values()
        if v.get("status") in active_statuses
    ]

    # Manual plans live in the VPS registry, not at Robinhood.  Merge the
    # registry with runtime positions so an unsynced/blocked watch is still
    # visible and a synced watch exposes its current execution state.
    manual_plans = load_plans(manual_plans_path)
    manual_plan_ids = {
        str(plan.get("id")) for plan in manual_plans if plan.get("id")
    }
    day_trade_plans: list[dict[str, Any]] = [
        dict(position)
        for position in active_day_trades
        if str(position.get("manual_plan_id") or "") not in manual_plan_ids
    ]

    all_positions = list(all_recs.values())
    for plan in manual_plans:
        plan_id = str(plan.get("id") or "")
        related = [
            position for position in all_positions
            if str(position.get("manual_plan_id") or "") == plan_id
        ]
        active = next(
            (position for position in reversed(related)
             if position.get("status") in active_statuses),
            None,
        )
        filled = next(
            (position for position in reversed(related) if position.get("fill_qty")),
            None,
        )

        if plan.get("status") == "cancelled":
            derived_status = "cancelled"
        elif filled:
            derived_status = "executed"
        elif active and active.get("status") == "pending_entry":
            derived_status = "entry_pending"
        elif active and active.get("status") == "watching":
            derived_status = "armed" if active.get("armed") else "waiting_rearm"
        elif active:
            derived_status = str(active.get("status"))
        else:
            ticker = str(plan.get("ticker", "")).upper()
            conflict = any(
                position.get("ticker") == ticker
                and str(position.get("manual_plan_id") or "") != plan_id
                and position.get("status") in active_statuses
                for position in all_positions
            )
            derived_status = "blocked_conflict" if conflict else "queued"

        # Keep current plans and any position that is still active during the
        # short cancellation/synchronization window.
        if plan.get("status") != "active" and active is None:
            continue

        runtime = active or filled or {}
        day_trade_plans.append({
            **runtime,
            **plan,
            "id": runtime.get("id") or plan_id,
            "manual_plan_id": plan_id,
            "source": "manual",
            "status": derived_status,
            "runtime_status": runtime.get("status"),
            "current_price": runtime.get("current_price"),
            "stop_price": runtime.get("stop_price"),
            "armed": runtime.get("armed"),
        })

    ctx["day_trades"] = active_day_trades
    ctx["day_trade_plans"] = day_trade_plans

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

    # Day-trade plan block. This VPS state is authoritative for triggers;
    # Robinhood has no broker order for a watch that has not fired yet.
    dt_text = "  (none)"
    if ctx["day_trade_plans"]:
        lines = []
        for dt in ctx["day_trade_plans"]:
            ticker = dt.get("ticker", "?")
            status = dt.get("status", "?")
            source = dt.get("source") or "discord"
            trigger = dt.get("trigger_price")
            target = dt.get("target_price")
            current = dt.get("current_price") or dt.get("fill_price")
            stop = dt.get("stop_price")
            setup = dt.get("setup") or "N/A"
            armed = dt.get("armed")
            pnl = dt.get("unrealized_pnl")
            pnl_str = f"  unrealized=${pnl:.2f}" if isinstance(pnl, (int, float)) else ""
            trigger_str = f"${float(trigger):.2f}" if isinstance(trigger, (int, float)) else "N/A"
            target_str = f"${float(target):.2f}" if isinstance(target, (int, float)) else "N/A"
            current_str = f"${float(current):.4f}" if isinstance(current, (int, float)) else "N/A"
            stop_str = f"${float(stop):.4f}" if isinstance(stop, (int, float)) else "N/A"
            armed_str = str(armed).lower() if isinstance(armed, bool) else "N/A"
            lines.append(
                f"  {ticker}: source={source}, status={status}, LONG trigger > {trigger_str}, "
                f"target={target_str}, current={current_str}, stop={stop_str}, "
                f"armed={armed_str}, setup={setup}{pnl_str}"
            )
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

    return f"""You are a trading assistant for a personal Robinhood account.
It is managed by a Discord-following trade bot running on a VPS.

You have Robinhood MCP access with these tools:
- get_accounts           → buying power, account value
- get_equity_positions   → current Robinhood positions
- get_equity_quotes      → live price for any ticker
- place_equity_order     → place market/limit/stop orders
- get_equity_orders      → recent order history

CURRENT TIME: {now}

OPEN SWING POSITIONS (from virtual book):
{swing_text}

DAY-TRADE PLANS AND POSITIONS (VPS state; authoritative for plans, watches, and triggers):
{dt_text}

RECENT SIGNALS TODAY:
{sig_text}

CONVERSATION SO FAR:
{history_text}{confirmed_note}

User: {user_message}

RULES:
1. For questions about day-trade plans, watches, trigger prices, setups, or the
   day-trade channel, answer from DAY-TRADE PLANS AND POSITIONS above. Do not
   call Robinhood tools solely to discover these plans. A watch is VPS state
   and normally has no Robinhood position or order until its trigger fires.
2. Use MCP tools for current broker account, buying power, real positions,
   broker orders, and live-price questions. Never treat a lack of Robinhood
   orders as evidence that a VPS plan or watch does not exist.
3. For any proposed trade action, first use get_equity_quotes to verify the current price,
   then describe the order clearly, then emit EXACTLY on its own line:
   PROPOSED_ORDER={{"ticker":"X","action":"BUY","dollar_amount":50.00,"rationale":"brief reason"}}
4. ONLY call place_equity_order when [CONFIRMED] appears above. Never execute without it.
5. After a successful placement, emit EXACTLY on its own line:
   BROKER_ORDER_ID=<the uuid returned by Robinhood>
6. Be concise and factual. Prefer numbers over prose.
7. For broker execution debug questions, use get_equity_orders and
   get_equity_positions to check real state; use VPS context for plan-registry questions.
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
