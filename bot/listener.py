"""
Discord selfbot listener.

Connects to Discord with a USER token (not a bot token), watches the configured
channel(s), and pipes each new message through the parser. Recognized signals
are appended to a JSONL file and printed to stdout.

Run:
    python -m bot.listener

Stop with Ctrl-C.

⚠️  Selfbots violate Discord's ToS. Use at your own risk; prefer a real bot or
    webhook forwarder if you can ever get one.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

import discord  # provided by `discord.py-self`
from dotenv import load_dotenv

from bot.parser import Signal, parse_message

log = logging.getLogger("bot.listener")


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # discord.py is chatty at INFO; quiet it down so our signal logs stand out.
    logging.getLogger("discord").setLevel(logging.WARNING)


def _load_config() -> tuple[str, set[int], Path]:
    load_dotenv()

    token = os.environ.get("DISCORD_USER_TOKEN", "").strip()
    if not token:
        sys.exit("DISCORD_USER_TOKEN is missing in .env")

    raw_channels = os.environ.get("DISCORD_CHANNEL_IDS", "").strip()
    if not raw_channels:
        sys.exit("DISCORD_CHANNEL_IDS is missing in .env (comma-separated channel IDs)")
    try:
        channels = {int(c.strip()) for c in raw_channels.split(",") if c.strip()}
    except ValueError as e:
        sys.exit(f"DISCORD_CHANNEL_IDS has a non-numeric entry: {e}")

    log_path = Path(os.environ.get("SIGNAL_LOG_PATH", "./logs/signals.jsonl"))
    log_path.parent.mkdir(parents=True, exist_ok=True)

    return token, channels, log_path


def _persist(signal: Signal, message: discord.Message, log_path: Path) -> None:
    """Append a parsed signal to the JSONL log, enriched with Discord metadata."""
    record = signal.to_dict()
    record["discord"] = {
        "message_id": message.id,
        "channel_id": message.channel.id,
        "channel_name": getattr(message.channel, "name", None),
        "guild_id": getattr(message.guild, "id", None),
        "author_id": message.author.id,
        "author_name": str(message.author),
        "created_at": message.created_at.isoformat(),
    }
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def build_client(channels: set[int], log_path: Path) -> discord.Client:
    """Construct the discord.py-self client with our event handlers wired up."""
    client = discord.Client()

    @client.event
    async def on_ready() -> None:
        log.info("Logged in as %s (id=%s)", client.user, client.user.id if client.user else "?")
        log.info("Watching %d channel(s): %s", len(channels), ", ".join(map(str, channels)))

    @client.event
    async def on_message(message: discord.Message) -> None:
        if message.channel.id not in channels:
            return

        # Discord messages can carry both `content` and rich embeds. The signal
        # bot we're listening to mainly posts plain bolded text, but we look at
        # both so we don't miss a future format change.
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

        for text in candidates:
            sig = parse_message(text)
            if sig is None:
                continue
            _persist(sig, message, log_path)
            actionable_marker = "  ◀ ACTIONABLE" if sig.is_actionable else ""
            log.info(
                "%s %s side=%s trigger=%s target=%s current=%s%s",
                sig.kind.value,
                sig.ticker,
                sig.side.value if sig.side else "?",
                sig.trigger,
                sig.target,
                sig.current_price,
                actionable_marker,
            )

    return client


def main() -> None:
    _setup_logging()
    token, channels, log_path = _load_config()
    log.info("Signals will be appended to %s", log_path)

    client = build_client(channels, log_path)
    try:
        client.run(token)
    except KeyboardInterrupt:
        log.info("Shutting down.")


if __name__ == "__main__":
    main()
