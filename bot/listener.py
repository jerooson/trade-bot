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
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import discord  # provided by `discord.py-self`
from dotenv import load_dotenv

from bot.parser import Signal, parse_message
from bot.heat_ideas import append_jsonl as append_heat_jsonl, parse_heat_idea
from bot.plan_parser import TradePlan, parse_plan
from bot.swing_parser import TradeAction, parse_swing

log = logging.getLogger("bot.listener")


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # discord.py is chatty at INFO; quiet it down so our signal logs stand out.
    logging.getLogger("discord").setLevel(logging.WARNING)


@dataclass
class ListenerConfig:
    """Resolved configuration for the listener.

    Each watched channel falls into one of four roles, each with its own
    parser and JSONL log:

    - `signal_channels`  -> bot.parser       (PLAN/TRIGGER/PROFIT signals)
    - `plan_channels`    -> bot.plan_parser  (free-form swing-trade write-ups)
    - `swing_channels`   -> bot.swing_parser (structured execution actions)

    All three sets must be pairwise disjoint.
    """

    token: str
    signal_channels: set[int]
    plan_channels: set[int]
    swing_channels: set[int]
    heat_channels: set[int]
    heat_author_ids: set[int]
    signal_log_path: Path
    plan_log_path: Path
    swing_log_path: Path
    heat_log_path: Path
    heat_attachments_dir: Path
    heat_max_attachment_bytes: int

    @property
    def all_channels(self) -> set[int]:
        return (
            self.signal_channels
            | self.plan_channels
            | self.swing_channels
            | self.heat_channels
        )


def _parse_channel_ids(raw: str, var_name: str) -> set[int]:
    if not raw.strip():
        return set()
    try:
        return {int(c.strip()) for c in raw.split(",") if c.strip()}
    except ValueError as e:
        sys.exit(f"{var_name} has a non-numeric entry: {e}")


def _load_config() -> ListenerConfig:
    load_dotenv()

    token = os.environ.get("DISCORD_USER_TOKEN", "").strip()
    if not token:
        sys.exit("DISCORD_USER_TOKEN is missing in .env")

    signal_channels = _parse_channel_ids(
        os.environ.get("DISCORD_CHANNEL_IDS", ""), "DISCORD_CHANNEL_IDS"
    )
    plan_channels = _parse_channel_ids(
        os.environ.get("DISCORD_PLAN_CHANNEL_IDS", ""), "DISCORD_PLAN_CHANNEL_IDS"
    )
    swing_channels = _parse_channel_ids(
        os.environ.get("DISCORD_SWING_CHANNEL_IDS", ""), "DISCORD_SWING_CHANNEL_IDS"
    )
    heat_channels = _parse_channel_ids(
        os.environ.get("DISCORD_HEAT_CHANNEL_IDS", ""), "DISCORD_HEAT_CHANNEL_IDS"
    )
    heat_author_ids = _parse_channel_ids(
        os.environ.get("DISCORD_HEAT_AUTHOR_IDS", ""), "DISCORD_HEAT_AUTHOR_IDS"
    )

    if not (signal_channels or plan_channels or swing_channels or heat_channels):
        sys.exit(
            "At least one of DISCORD_CHANNEL_IDS / DISCORD_PLAN_CHANNEL_IDS / "
            "DISCORD_SWING_CHANNEL_IDS / DISCORD_HEAT_CHANNEL_IDS must be set in .env"
        )
    if heat_channels and not heat_author_ids:
        sys.exit("DISCORD_HEAT_AUTHOR_IDS is required when a Heat channel is configured")

    sets = [
        ("DISCORD_CHANNEL_IDS", signal_channels),
        ("DISCORD_PLAN_CHANNEL_IDS", plan_channels),
        ("DISCORD_SWING_CHANNEL_IDS", swing_channels),
        ("DISCORD_HEAT_CHANNEL_IDS", heat_channels),
    ]
    for i in range(len(sets)):
        for j in range(i + 1, len(sets)):
            overlap = sets[i][1] & sets[j][1]
            if overlap:
                sys.exit(
                    f"Channel id(s) {overlap} appear in BOTH {sets[i][0]} and "
                    f"{sets[j][0]}; pick one role per channel."
                )

    signal_log_path = Path(os.environ.get("SIGNAL_LOG_PATH", "./logs/signals.jsonl"))
    plan_log_path = Path(os.environ.get("PLAN_LOG_PATH", "./logs/plans.jsonl"))
    swing_log_path = Path(os.environ.get("SWING_LOG_PATH", "./logs/swings.jsonl"))
    heat_log_path = Path(os.environ.get("HEAT_IDEAS_LOG_PATH", "./logs/heat_ideas.jsonl"))
    heat_attachments_dir = Path(
        os.environ.get("HEAT_ATTACHMENTS_DIR", "./logs/heat_attachments")
    )
    try:
        heat_max_attachment_bytes = int(
            os.environ.get("HEAT_MAX_ATTACHMENT_BYTES", str(8 * 1024 * 1024))
        )
    except ValueError:
        sys.exit("HEAT_MAX_ATTACHMENT_BYTES must be an integer")
    for p in (signal_log_path, plan_log_path, swing_log_path, heat_log_path):
        p.parent.mkdir(parents=True, exist_ok=True)
    heat_attachments_dir.mkdir(parents=True, exist_ok=True)

    return ListenerConfig(
        token=token,
        signal_channels=signal_channels,
        plan_channels=plan_channels,
        swing_channels=swing_channels,
        heat_channels=heat_channels,
        heat_author_ids=heat_author_ids,
        signal_log_path=signal_log_path,
        plan_log_path=plan_log_path,
        swing_log_path=swing_log_path,
        heat_log_path=heat_log_path,
        heat_attachments_dir=heat_attachments_dir,
        heat_max_attachment_bytes=heat_max_attachment_bytes,
    )


def _discord_metadata(message: discord.Message) -> dict:
    return {
        "message_id": message.id,
        "channel_id": message.channel.id,
        "channel_name": getattr(message.channel, "name", None),
        "guild_id": getattr(message.guild, "id", None) if message.guild else None,
        "author_id": message.author.id,
        "author_name": str(message.author),
        "created_at": message.created_at.isoformat(),
    }


def _append_jsonl(record: dict, log_path: Path) -> None:
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def _persist_signal(signal: Signal, message: discord.Message, log_path: Path) -> None:
    record = signal.to_dict()
    record["discord"] = _discord_metadata(message)
    _append_jsonl(record, log_path)


def _persist_plan(plan: TradePlan, message: discord.Message, log_path: Path) -> None:
    record = plan.to_dict()
    record["discord"] = _discord_metadata(message)
    _append_jsonl(record, log_path)


def _persist_swing(action: TradeAction, message: discord.Message, log_path: Path) -> None:
    record = action.to_dict()
    record["discord"] = _discord_metadata(message)
    _append_jsonl(record, log_path)


def _embed_text(embed: discord.Embed) -> str:
    """Flatten an embed to a single string for parsers that don't care about structure."""
    chunks: list[str] = []
    if embed.title:
        chunks.append(str(embed.title))
    if embed.description:
        chunks.append(str(embed.description))
    for fld in embed.fields:
        chunks.append(f"{fld.name}: {fld.value}")
    return "\n".join(chunks)


def build_client(config: ListenerConfig) -> discord.Client:
    """Construct the discord.py-self client with our event handlers wired up."""
    client = discord.Client()
    # author id -> (idea id, source timestamp).  Heat often posts chart-only
    # messages immediately after the explanatory text.
    recent_heat_ideas: dict[int, tuple[str, datetime]] = {}

    @client.event
    async def on_ready() -> None:
        log.info("Logged in as %s (id=%s)", client.user, client.user.id if client.user else "?")
        if config.signal_channels:
            log.info("Signal channel(s): %s", ", ".join(map(str, config.signal_channels)))
        if config.plan_channels:
            log.info("Plan    channel(s): %s", ", ".join(map(str, config.plan_channels)))
        if config.swing_channels:
            log.info("Swing   channel(s): %s", ", ".join(map(str, config.swing_channels)))
        if config.heat_channels:
            log.info("Heat    channel(s): %s", ", ".join(map(str, config.heat_channels)))

    @client.event
    async def on_message(message: discord.Message) -> None:
        cid = message.channel.id

        if cid in config.signal_channels:
            await _handle_signal_message(message, config.signal_log_path)
        elif cid in config.plan_channels:
            await _handle_plan_message(message, config.plan_log_path)
        elif cid in config.swing_channels:
            await _handle_swing_message(message, config.swing_log_path)
        elif cid in config.heat_channels:
            await _handle_heat_message(message)
        # else: not a watched channel, ignore.

    async def _handle_signal_message(message: discord.Message, log_path: Path) -> None:
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
            _persist_signal(sig, message, log_path)
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

    async def _handle_plan_message(message: discord.Message, log_path: Path) -> None:
        # Plan messages have one logical post per Discord message. We feed
        # the body + first embed title (where the BATS:TICKER hint lives).
        embed_title: str | None = None
        if message.embeds and message.embeds[0].title:
            embed_title = str(message.embeds[0].title)

        plan = parse_plan(message.content or "", embed_title=embed_title)
        if plan is None:
            return

        _persist_plan(plan, message, log_path)
        log.info(
            "PLAN_NOTE %s levels=%s chart=%s",
            plan.ticker,
            plan.watch_levels,
            "yes" if plan.chart_url else "no",
        )

    async def _handle_swing_message(message: discord.Message, log_path: Path) -> None:
        # The swing channel posts a single structured action per message in the
        # body. Embeds (mostly chart screenshots) carry no parser-relevant text.
        action = parse_swing(message.content or "")
        if action is None:
            return

        _persist_swing(action, message, log_path)
        actionable_marker = "  <- ACTIONABLE" if action.is_actionable else ""
        log.info(
            "%s %s side=%s price=%s size=%s stop=%s%s",
            action.kind.value,
            action.ticker,
            action.side.value if action.side else "?",
            action.price,
            action.position_size,
            action.stop_loss_label,
            actionable_marker,
        )

    async def _save_heat_attachments(message: discord.Message) -> list[str]:
        saved: list[str] = []
        allowed = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
        for index, attachment in enumerate(message.attachments[:4]):
            suffix = Path(attachment.filename or "").suffix.lower()
            content_type = str(getattr(attachment, "content_type", "") or "")
            if suffix not in allowed and not content_type.startswith("image/"):
                continue
            if int(getattr(attachment, "size", 0) or 0) > config.heat_max_attachment_bytes:
                log.warning("Skipping oversized Heat attachment %s", attachment.filename)
                continue
            if suffix not in allowed:
                suffix = ".jpg"
            filename = f"{message.id}-{index}{suffix}"
            path = config.heat_attachments_dir / filename
            try:
                await attachment.save(path)
            except Exception as exc:
                log.warning("Could not save Heat attachment %s: %s", attachment.filename, exc)
                continue
            saved.append(filename)
        return saved

    async def _reply_text(message: discord.Message) -> str:
        reference = getattr(message, "reference", None)
        if not reference:
            return ""
        resolved = getattr(reference, "resolved", None)
        if resolved is None and getattr(reference, "message_id", None):
            try:
                resolved = await message.channel.fetch_message(reference.message_id)
            except Exception:
                return ""
        if resolved is None:
            return ""
        chunks = [str(getattr(resolved, "content", "") or "")]
        for embed in getattr(resolved, "embeds", []):
            chunks.append(_embed_text(embed))
        return "\n".join(chunk for chunk in chunks if chunk).strip()

    async def _handle_heat_message(message: discord.Message) -> None:
        author_id = int(message.author.id)
        if author_id not in config.heat_author_ids:
            return

        attachments = await _save_heat_attachments(message)
        text_chunks = [message.content or ""]
        text_chunks.extend(_embed_text(embed) for embed in message.embeds)
        body = "\n".join(chunk for chunk in text_chunks if chunk).strip()
        created_at = message.created_at.isoformat()
        idea = parse_heat_idea(
            body,
            reply_text=await _reply_text(message),
            idea_id=str(message.id),
            created_at=created_at,
        )
        if idea is not None:
            idea["attachments"] = attachments
            idea["discord"] = _discord_metadata(message)
            append_heat_jsonl(config.heat_log_path, idea)
            recent_heat_ideas[author_id] = (idea["id"], message.created_at)
            log.info(
                "HEAT_IDEA %s trigger=%s mode=%s attachments=%d",
                idea["ticker"], idea["trigger_price"],
                "auto" if idea["auto_eligible"] else "review", len(attachments),
            )
            return

        # Associate a separate chart-only post with Heat's immediately prior
        # text idea.  No new order candidate is created from an image alone.
        previous = recent_heat_ideas.get(author_id)
        if attachments and not body and previous:
            idea_id, previous_at = previous
            age_s = (message.created_at - previous_at).total_seconds()
            if 0 <= age_s <= 120:
                append_heat_jsonl(config.heat_log_path, {
                    "event_type": "attachment_update",
                    "id": str(message.id),
                    "idea_id": idea_id,
                    "attachments": attachments,
                    "created_at": created_at,
                    "discord": _discord_metadata(message),
                })
                log.info("HEAT_CHART idea=%s attachments=%d", idea_id, len(attachments))

    return client


def main() -> None:
    _setup_logging()
    config = _load_config()
    if config.signal_channels:
        log.info("Signals -> %s", config.signal_log_path)
    if config.plan_channels:
        log.info("Plans   -> %s", config.plan_log_path)
    if config.swing_channels:
        log.info("Swings  -> %s", config.swing_log_path)
    if config.heat_channels:
        log.info("Heat    -> %s", config.heat_log_path)

    client = build_client(config)
    try:
        client.run(config.token)
    except KeyboardInterrupt:
        log.info("Shutting down.")


if __name__ == "__main__":
    main()
