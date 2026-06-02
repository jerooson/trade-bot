"""
One-shot Discord channel inspector.

Fetches the last N messages from a given channel ID and prints raw content
(plus embed text). Useful when building/updating a parser for a new channel,
because you can see exactly what we're dealing with.

This does NOT parse, dedupe, or persist anything -- it's a diagnostic.

Run:
    python -m bot.inspect_channel --channel 1121659854726103071 --limit 10
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os

import discord
from dotenv import load_dotenv

log = logging.getLogger("bot.inspect_channel")


def _setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("discord").setLevel(logging.WARNING)


def main() -> None:
    ap = argparse.ArgumentParser(description="Dump raw recent messages from a Discord channel.")
    ap.add_argument("--channel", type=int, required=True, help="Channel ID to inspect.")
    ap.add_argument("--limit", type=int, default=10, help="Max messages to fetch (default 10).")
    args = ap.parse_args()

    _setup_logging()
    load_dotenv()
    token = os.environ.get("DISCORD_USER_TOKEN", "").strip()
    if not token:
        raise SystemExit("DISCORD_USER_TOKEN missing in .env")

    client = discord.Client()

    @client.event
    async def on_ready() -> None:
        try:
            channel = client.get_channel(args.channel) or await client.fetch_channel(args.channel)
            name = getattr(channel, "name", "?")
            log.info("=" * 80)
            log.info("Inspecting channel: #%s (id=%s)", name, args.channel)
            log.info("=" * 80)

            count = 0
            async for msg in channel.history(limit=args.limit, oldest_first=False):
                count += 1
                log.info("")
                log.info("-" * 80)
                log.info("[%s] %s  (msg_id=%s)", msg.created_at.strftime("%Y-%m-%d %H:%M"),
                        msg.author, msg.id)
                log.info("-" * 80)
                if msg.content:
                    log.info("CONTENT:")
                    log.info(msg.content)
                for i, embed in enumerate(msg.embeds):
                    log.info("EMBED %d:", i)
                    if embed.title:
                        log.info("  title: %s", embed.title)
                    if embed.description:
                        log.info("  description: %s", embed.description)
                    for fld in embed.fields:
                        log.info("  field [%s]: %s", fld.name, fld.value)
                if msg.attachments:
                    log.info("ATTACHMENTS: %d", len(msg.attachments))

            log.info("")
            log.info("=" * 80)
            log.info("Done. %d messages dumped.", count)
        finally:
            await client.close()

    client.run(token)


if __name__ == "__main__":
    main()
