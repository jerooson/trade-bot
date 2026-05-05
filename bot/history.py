"""
One-shot historical message backfill.

Pulls the last N messages from each configured channel, parses them, prints
parsed signals, and writes them to a JSONL file. Then exits cleanly.

This is the same parsing pipeline the live listener uses; the only difference
is the source of messages (channel.history paginated fetch instead of
gateway message_create events).

Useful for:
  - Verifying the parser against REAL channel messages (not just unit fixtures)
  - Catching up after the listener was offline
  - Building a dataset for backtesting

Run:
    python -m bot.history                        # default 200 messages/channel
    python -m bot.history --limit 1000           # go further back
    python -m bot.history --out logs/dump.jsonl  # custom output path
    python -m bot.history --since 2026-05-01     # only messages on/after this date

Stop with Ctrl-C.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import discord

from bot.listener import _load_config, _setup_logging
from bot.parser import Signal, parse_message

log = logging.getLogger("bot.history")


def _extract_text_candidates(message: discord.Message) -> list[str]:
    """Same logic as the live listener: look at message.content AND embeds."""
    candidates: list[str] = []
    if message.content:
        candidates.append(message.content)
    for embed in message.embeds:
        chunks: list[str] = []
        if embed.title:
            chunks.append(str(embed.title))
        if embed.description:
            chunks.append(str(embed.description))
        for fld in embed.fields:
            chunks.append(f"{fld.name}: {fld.value}")
        if chunks:
            candidates.append("\n".join(chunks))
    return candidates


def _record(sig: Signal, message: discord.Message, channel: discord.abc.GuildChannel | discord.DMChannel) -> dict:
    record = sig.to_dict()
    record["discord"] = {
        "message_id": message.id,
        "channel_id": channel.id,
        "channel_name": getattr(channel, "name", None),
        "guild_id": getattr(message.guild, "id", None) if message.guild else None,
        "author_id": message.author.id,
        "author_name": str(message.author),
        "created_at": message.created_at.isoformat(),
    }
    return record


async def _backfill_channel(
    client: discord.Client,
    channel_id: int,
    limit: int,
    after: datetime | None,
    out_path: Path,
) -> tuple[int, int]:
    """Returns (messages_scanned, signals_parsed)."""
    channel = client.get_channel(channel_id)
    if channel is None:
        try:
            channel = await client.fetch_channel(channel_id)
        except Exception as e:
            log.error("Could not access channel %s: %s", channel_id, e)
            return 0, 0

    name = getattr(channel, "name", "?")
    log.info("Fetching up to %d messages from #%s (%s)%s",
             limit, name, channel_id,
             f" since {after.date()}" if after else "")

    scanned = 0
    parsed = 0
    # oldest_first=False is the default; we go newest-first and let "limit"
    # bound how far back we go. If "after" is set we let history filter for us.
    async for message in channel.history(limit=limit, after=after, oldest_first=False):
        scanned += 1
        for text in _extract_text_candidates(message):
            sig = parse_message(text)
            if sig is None:
                continue
            parsed += 1

            with out_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(_record(sig, message, channel), ensure_ascii=False) + "\n")

            log.info(
                "%-7s %-6s side=%-5s trigger=%-7s target=%-7s current=%-7s  [%s]",
                sig.kind.value,
                sig.ticker,
                sig.side.value if sig.side else "?",
                sig.trigger,
                sig.target,
                sig.current_price,
                message.created_at.strftime("%Y-%m-%d %H:%M"),
            )

    log.info("Channel %s done: %d messages scanned, %d signals parsed.", channel_id, scanned, parsed)
    return scanned, parsed


def _parse_since(value: str | None) -> datetime | None:
    if not value:
        return None
    # Accept YYYY-MM-DD or full ISO 8601.
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
    ap = argparse.ArgumentParser(description="Backfill historical signals from Discord channels.")
    ap.add_argument("--limit", type=int, default=200, help="Max messages per channel (default 200).")
    ap.add_argument("--since", type=str, default=None,
                    help="Only fetch messages on/after this date (YYYY-MM-DD or ISO 8601 UTC).")
    ap.add_argument("--out", type=Path, default=Path("./logs/history.jsonl"),
                    help="Output JSONL file (default ./logs/history.jsonl). Records are APPENDED.")
    args = ap.parse_args()

    _setup_logging()
    token, channel_ids, _ = _load_config()
    after = _parse_since(args.since)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    log.info("Writing parsed signals to %s", args.out)

    client = discord.Client()

    @client.event
    async def on_ready() -> None:
        log.info("Logged in as %s (id=%s)", client.user, client.user.id if client.user else "?")
        total_scanned = 0
        total_parsed = 0
        try:
            for cid in channel_ids:
                s, p = await _backfill_channel(client, cid, args.limit, after, args.out)
                total_scanned += s
                total_parsed += p
        finally:
            log.info("All channels done: %d messages scanned, %d signals parsed total.",
                     total_scanned, total_parsed)
            await client.close()

    try:
        client.run(token)
    except KeyboardInterrupt:
        log.info("Interrupted.")


if __name__ == "__main__":
    main()
