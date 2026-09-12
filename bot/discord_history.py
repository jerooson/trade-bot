"""
One-shot Discord channel history export for research.

Fetches every message by the configured Heat authors in the Heat channels
(``DISCORD_HEAT_CHANNEL_IDS`` / ``DISCORD_HEAT_AUTHOR_IDS``) after ``--since``
and writes one JSON line per message: text, embeds, attachment names, the
message it replied to.  Nothing is parsed or executed; the output is a
private research file (keep it under ``logs/`` or ``data/``, both gitignored).

    python -m bot.discord_history --since 2026-05-01 --out logs/heat_history.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import discord  # discord.py-self
from dotenv import load_dotenv

from bot.listener import _embed_text, _parse_channel_ids


async def _export(token: str, channel_ids: set[int], author_ids: set[int],
                  since: datetime, out: Path, all_authors: bool) -> None:
    client = discord.Client()
    done = asyncio.Event()

    @client.event
    async def on_ready() -> None:
        count = 0
        try:
            with out.open("w", encoding="utf-8") as fh:
                for cid in sorted(channel_ids):
                    channel = client.get_channel(cid) or await client.fetch_channel(cid)
                    async for m in channel.history(limit=None, after=since, oldest_first=True):
                        if not all_authors and m.author.id not in author_ids:
                            continue
                        ref = m.reference.message_id if m.reference else None
                        rec = {
                            "id": str(m.id), "channel_id": cid, "author_id": m.author.id,
                            "author": str(m.author), "created_at": m.created_at.isoformat(),
                            "edited_at": m.edited_at.isoformat() if m.edited_at else None,
                            "content": m.content or "",
                            "embeds": [_embed_text(e) for e in m.embeds],
                            "attachments": [a.filename for a in m.attachments],
                            "attachment_urls": [a.url for a in m.attachments],
                            "reply_to": str(ref) if ref else None,
                        }
                        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                        count += 1
                        if count % 200 == 0:
                            print(f"  {count} messages...", flush=True)
            print(f"wrote {count} messages -> {out}")
        finally:
            done.set()

    async with client:
        await client.login(token)
        task = asyncio.create_task(client.connect())
        await done.wait()
        task.cancel()


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="Export Heat channel history")
    p.add_argument("--since", type=lambda s: datetime.fromisoformat(s).replace(tzinfo=timezone.utc),
                   default=datetime(2026, 5, 1, tzinfo=timezone.utc))
    p.add_argument("--out", type=Path, default=Path("logs/heat_history.jsonl"))
    p.add_argument("--all-authors", action="store_true", help="include replies from other members")
    args = p.parse_args(argv)
    load_dotenv()
    token = os.environ.get("DISCORD_USER_TOKEN", "").strip()
    channels = _parse_channel_ids(os.environ.get("DISCORD_HEAT_CHANNEL_IDS", ""), "DISCORD_HEAT_CHANNEL_IDS")
    authors = _parse_channel_ids(os.environ.get("DISCORD_HEAT_AUTHOR_IDS", ""), "DISCORD_HEAT_AUTHOR_IDS")
    if not token or not channels or not authors:
        sys.exit("DISCORD_USER_TOKEN, DISCORD_HEAT_CHANNEL_IDS and DISCORD_HEAT_AUTHOR_IDS are required")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    asyncio.run(_export(token, channels, authors, args.since, args.out, args.all_authors))


if __name__ == "__main__":
    main()
