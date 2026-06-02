"""
One-shot historical message backfill.

Pulls the last N messages from each configured channel, parses them with the
right parser for that channel's role (signal vs plan), and appends recognized
records to JSONL files. Then exits cleanly.

Output files:
  - signal channels  -> ./logs/history.jsonl        (overridable via --signal-out)
  - plan channels    -> ./logs/plans_history.jsonl  (overridable via --plan-out)
  - swing channels   -> ./logs/swings_history.jsonl (overridable via --swing-out)

Useful for:
  - Catching up after the listener was offline
  - Building a dataset for backtesting
  - Verifying parsers against REAL channel messages

Run:
    python -m bot.history                           # all configured channels
    python -m bot.history --limit 1000              # go further back
    python -m bot.history --since 2026-05-07        # only on/after this date
    python -m bot.history --kind signal             # only signal channels
    python -m bot.history --kind plan               # only plan channels
    python -m bot.history --kind swing              # only swing channels
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import discord

from bot.listener import _embed_text, _load_config, _setup_logging
from bot.parser import parse_message
from bot.plan_parser import parse_plan
from bot.swing_parser import parse_swing

log = logging.getLogger("bot.history")


def _discord_metadata(message: discord.Message, channel) -> dict:
    return {
        "message_id": message.id,
        "channel_id": channel.id,
        "channel_name": getattr(channel, "name", None),
        "guild_id": getattr(message.guild, "id", None) if message.guild else None,
        "author_id": message.author.id,
        "author_name": str(message.author),
        "created_at": message.created_at.isoformat(),
    }


async def _backfill_signal_channel(
    client: discord.Client,
    channel_id: int,
    limit: int,
    after: datetime | None,
    out_path: Path,
) -> tuple[int, int]:
    """Returns (messages_scanned, signals_parsed) for a SIGNAL channel."""
    channel = client.get_channel(channel_id) or await _safe_fetch(client, channel_id)
    if channel is None:
        return 0, 0

    log.info("[signal] Fetching up to %d msgs from #%s (%s)%s",
             limit, getattr(channel, "name", "?"), channel_id,
             f" since {after.date()}" if after else "")

    scanned = parsed = 0
    async for message in channel.history(limit=limit, after=after, oldest_first=False):
        scanned += 1
        candidates: list[str] = []
        if message.content:
            candidates.append(message.content)
        for embed in message.embeds:
            text = _embed_text(embed)
            if text:
                candidates.append(text)

        for text in candidates:
            sig = parse_message(text)
            if sig is None:
                continue
            parsed += 1
            record = sig.to_dict()
            record["discord"] = _discord_metadata(message, channel)
            with out_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")

            log.info(
                "[signal] %-7s %-6s side=%-5s trigger=%-7s target=%-7s current=%-7s  [%s]",
                sig.kind.value,
                sig.ticker,
                sig.side.value if sig.side else "?",
                sig.trigger,
                sig.target,
                sig.current_price,
                message.created_at.strftime("%Y-%m-%d %H:%M"),
            )

    log.info("[signal] Channel %s done: %d msgs scanned, %d signals parsed.",
             channel_id, scanned, parsed)
    return scanned, parsed


async def _backfill_plan_channel(
    client: discord.Client,
    channel_id: int,
    limit: int,
    after: datetime | None,
    out_path: Path,
) -> tuple[int, int]:
    """Returns (messages_scanned, plans_parsed) for a PLAN channel."""
    channel = client.get_channel(channel_id) or await _safe_fetch(client, channel_id)
    if channel is None:
        return 0, 0

    log.info("[plan]   Fetching up to %d msgs from #%s (%s)%s",
             limit, getattr(channel, "name", "?"), channel_id,
             f" since {after.date()}" if after else "")

    scanned = parsed = 0
    async for message in channel.history(limit=limit, after=after, oldest_first=False):
        scanned += 1

        embed_title: str | None = None
        if message.embeds and message.embeds[0].title:
            embed_title = str(message.embeds[0].title)

        plan = parse_plan(message.content or "", embed_title=embed_title)
        if plan is None:
            continue
        parsed += 1
        record = plan.to_dict()
        record["discord"] = _discord_metadata(message, channel)
        with out_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

        log.info(
            "[plan]   %-6s levels=%s  [%s]",
            plan.ticker,
            plan.watch_levels,
            message.created_at.strftime("%Y-%m-%d %H:%M"),
        )

    log.info("[plan]   Channel %s done: %d msgs scanned, %d plans parsed.",
             channel_id, scanned, parsed)
    return scanned, parsed


async def _backfill_swing_channel(
    client: discord.Client,
    channel_id: int,
    limit: int,
    after: datetime | None,
    out_path: Path,
) -> tuple[int, int]:
    """Returns (messages_scanned, actions_parsed) for a SWING channel."""
    channel = client.get_channel(channel_id) or await _safe_fetch(client, channel_id)
    if channel is None:
        return 0, 0

    log.info("[swing]  Fetching up to %d msgs from #%s (%s)%s",
             limit, getattr(channel, "name", "?"), channel_id,
             f" since {after.date()}" if after else "")

    scanned = parsed = 0
    async for message in channel.history(limit=limit, after=after, oldest_first=False):
        scanned += 1
        action = parse_swing(message.content or "")
        if action is None:
            continue
        parsed += 1
        record = action.to_dict()
        record["discord"] = _discord_metadata(message, channel)
        with out_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

        log.info(
            "[swing]  %-16s %-6s side=%-5s price=%-8s size=%-12s stop=%-12s [%s]",
            action.kind.value,
            action.ticker,
            action.side.value if action.side else "?",
            action.price,
            action.position_size or "",
            action.stop_loss_label or "",
            message.created_at.strftime("%Y-%m-%d %H:%M"),
        )

    log.info("[swing]  Channel %s done: %d msgs scanned, %d actions parsed.",
             channel_id, scanned, parsed)
    return scanned, parsed


async def _safe_fetch(client: discord.Client, channel_id: int):
    try:
        return await client.fetch_channel(channel_id)
    except Exception as e:
        log.error("Could not access channel %s: %s", channel_id, e)
        return None


def _parse_since(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        if len(value) == 10:
            dt = datetime.strptime(value, "%Y-%m-%d")
        else:
            dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError as e:
        raise SystemExit(f"--since must be YYYY-MM-DD or ISO 8601: {e}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Backfill historical messages from Discord channels.")
    ap.add_argument("--limit", type=int, default=200, help="Max messages per channel (default 200).")
    ap.add_argument("--since", type=str, default=None,
                    help="Only fetch messages on/after this date (YYYY-MM-DD or ISO 8601 UTC).")
    ap.add_argument("--kind", choices=("all", "signal", "plan", "swing"), default="all",
                    help="Limit backfill to a specific channel role (default all).")
    ap.add_argument("--signal-out", type=Path, default=Path("./logs/history.jsonl"),
                    help="Output JSONL for signal channels (default ./logs/history.jsonl).")
    ap.add_argument("--plan-out", type=Path, default=Path("./logs/plans_history.jsonl"),
                    help="Output JSONL for plan channels (default ./logs/plans_history.jsonl).")
    ap.add_argument("--swing-out", type=Path, default=Path("./logs/swings_history.jsonl"),
                    help="Output JSONL for swing channels (default ./logs/swings_history.jsonl).")
    args = ap.parse_args()

    _setup_logging()
    config = _load_config()
    after = _parse_since(args.since)

    args.signal_out.parent.mkdir(parents=True, exist_ok=True)
    args.plan_out.parent.mkdir(parents=True, exist_ok=True)
    args.swing_out.parent.mkdir(parents=True, exist_ok=True)

    do_signals = args.kind in ("all", "signal") and bool(config.signal_channels)
    do_plans = args.kind in ("all", "plan") and bool(config.plan_channels)
    do_swings = args.kind in ("all", "swing") and bool(config.swing_channels)

    if not (do_signals or do_plans or do_swings):
        sys_exit_msg = "Nothing to backfill -- "
        if args.kind != "all":
            sys_exit_msg += f"no {args.kind} channels are configured."
        else:
            sys_exit_msg += "no channels configured in .env."
        raise SystemExit(sys_exit_msg)

    if do_signals:
        log.info("Signal backfill -> %s", args.signal_out)
    if do_plans:
        log.info("Plan   backfill -> %s", args.plan_out)
    if do_swings:
        log.info("Swing  backfill -> %s", args.swing_out)

    client = discord.Client()

    @client.event
    async def on_ready() -> None:
        log.info("Logged in as %s (id=%s)", client.user, client.user.id if client.user else "?")
        total_scanned = 0
        total_parsed = 0
        try:
            if do_signals:
                for cid in config.signal_channels:
                    s, p = await _backfill_signal_channel(client, cid, args.limit, after, args.signal_out)
                    total_scanned += s
                    total_parsed += p
            if do_plans:
                for cid in config.plan_channels:
                    s, p = await _backfill_plan_channel(client, cid, args.limit, after, args.plan_out)
                    total_scanned += s
                    total_parsed += p
            if do_swings:
                for cid in config.swing_channels:
                    s, p = await _backfill_swing_channel(client, cid, args.limit, after, args.swing_out)
                    total_scanned += s
                    total_parsed += p
        finally:
            log.info("All channels done: %d msgs scanned, %d records parsed total.",
                     total_scanned, total_parsed)
            await client.close()

    try:
        client.run(config.token)
    except KeyboardInterrupt:
        log.info("Interrupted.")


if __name__ == "__main__":
    main()
