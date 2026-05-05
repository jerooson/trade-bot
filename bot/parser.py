"""
Pure-function parser for the Will-the-Rocket signal channel.

Three message shapes are recognized:

  PLAN     -- header contains "日内短线交易计划"  (📊)  -- planned setup, heads-up
  TRIGGER  -- header contains "日内短线触发"      (🎯)  -- trigger hit, ACTIONABLE
  PROFIT   -- header contains "日内短线盈利提醒"  (📈)  -- profit alert, informational

Both Discord-rendered (with **bold** asterisks) and plain-text-copied versions are handled.

The parser is intentionally a pure function: text in, structured Signal out, no I/O.
That way we can unit-test against fixture strings without touching Discord at all,
and we can replay historical messages from a JSONL log by re-parsing them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class SignalKind(str, Enum):
    PLAN = "PLAN"
    TRIGGER = "TRIGGER"
    PROFIT = "PROFIT"


class Side(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


# -- Header detection ---------------------------------------------------------
# We look for the Chinese phrase rather than the emoji, because the emoji can be
# rendered/dropped inconsistently across copy-paste and platforms.
_KIND_PATTERNS: list[tuple[re.Pattern[str], SignalKind]] = [
    (re.compile(r"日内短线交易计划"), SignalKind.PLAN),
    (re.compile(r"日内短线触发"), SignalKind.TRIGGER),
    (re.compile(r"日内短线盈利提醒"), SignalKind.PROFIT),
]


# -- Field extraction ---------------------------------------------------------
# Strip the bold ** markers so a single regex handles both raw and rendered text.
_BOLD_RE = re.compile(r"\*\*")


def _strip_bold(text: str) -> str:
    return _BOLD_RE.sub("", text)


# Each field is "Label: value" on its own line. The label can have a few synonyms
# (e.g. "Trigger" vs "Trigger Price") so we accept any of them.
_FIELD_RE = re.compile(
    r"^\s*(?P<label>[A-Za-z][A-Za-z ]*?)\s*:\s*(?P<value>.+?)\s*$",
    re.MULTILINE,
)

# Label aliases -> canonical key. Keys are lowercased.
_LABEL_ALIASES: dict[str, str] = {
    "ticker": "ticker",
    "type": "side",
    "trigger": "trigger",
    "trigger price": "trigger",
    "target": "target",
    "current price": "current_price",
    "setup": "setup",
    "chart": "chart",
    "attention": "attention",
    "profit": "profit",
}

# A value like "$96.32" or "9.64" or "> 9.64" -> 9.64 (or 96.32).
# We strip $, commas, comparison operators, and whitespace; the *direction* of
# the comparison is implied by Side anyway (LONG = breakout above, SHORT = below).
_NUMBER_IN_VALUE = re.compile(r"-?\d+(?:\.\d+)?")


def _parse_number(value: str) -> float | None:
    """
    Best-effort numeric extraction. Returns None for "None", empty, or unparseable.
    Examples:
        "$96.32"      -> 96.32
        "> 9.64"      -> 9.64
        "11.07"       -> 11.07
        "None"        -> None
        "+3.6%"       -> 3.6  (caller decides interpretation)
    """
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned or cleaned.lower() == "none":
        return None
    m = _NUMBER_IN_VALUE.search(cleaned)
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def _parse_side(value: str) -> Side | None:
    v = value.upper()
    if "LONG" in v or "多" in value:
        return Side.LONG
    if "SHORT" in v or "空" in value:
        return Side.SHORT
    return None


@dataclass
class Signal:
    kind: SignalKind
    ticker: str
    side: Side | None = None
    trigger: float | None = None
    target: float | None = None
    current_price: float | None = None
    profit_pct: float | None = None
    setup: str | None = None
    chart_url: str | None = None
    raw_text: str = ""
    received_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["kind"] = self.kind.value
        d["side"] = self.side.value if self.side else None
        d["received_at"] = self.received_at.isoformat()
        return d

    @property
    def is_actionable(self) -> bool:
        """Only TRIGGER messages with a ticker, side, and trigger price are actionable."""
        return (
            self.kind is SignalKind.TRIGGER
            and bool(self.ticker)
            and self.side is not None
            and self.trigger is not None
        )


def detect_kind(text: str) -> SignalKind | None:
    for pattern, kind in _KIND_PATTERNS:
        if pattern.search(text):
            return kind
    return None


def parse_message(text: str) -> Signal | None:
    """
    Parse a single Discord message. Returns None if the message is not a recognized signal.

    A "single message" here means one logical post. A real Discord message_create event
    delivers exactly that. If you have a long blob containing multiple posts (e.g. you
    copy-pasted a scrollback), use `split_messages` first.
    """
    if not text or not text.strip():
        return None

    kind = detect_kind(text)
    if kind is None:
        return None

    cleaned = _strip_bold(text)
    fields: dict[str, str] = {}
    for m in _FIELD_RE.finditer(cleaned):
        label = m.group("label").strip().lower()
        value = m.group("value").strip()
        canonical = _LABEL_ALIASES.get(label)
        if canonical and canonical not in fields:
            fields[canonical] = value

    ticker = fields.get("ticker", "").upper().strip()
    if not ticker:
        return None

    return Signal(
        kind=kind,
        ticker=ticker,
        side=_parse_side(fields["side"]) if "side" in fields else None,
        trigger=_parse_number(fields.get("trigger", "")),
        target=_parse_number(fields.get("target", "")),
        current_price=_parse_number(fields.get("current_price", "")),
        profit_pct=_parse_number(fields.get("profit", "")),
        setup=fields.get("setup"),
        chart_url=fields.get("chart"),
        raw_text=text,
    )


# -- Multi-post splitting -----------------------------------------------------
# Useful when the user pastes a scrollback containing several signals back-to-back,
# or when a single Discord message contains multiple stacked posts (which the
# sample shows can happen).

_POST_SPLIT_RE = re.compile(
    r"(?=^\s*(?:📊|🎯|📈)?\s*\**(?:日内短线交易计划|日内短线触发|日内短线盈利提醒))",
    re.MULTILINE,
)


def split_messages(blob: str) -> list[str]:
    """Split a blob into individual signal posts (each starting with a known header)."""
    parts = [p.strip() for p in _POST_SPLIT_RE.split(blob) if p and p.strip()]
    return [p for p in parts if detect_kind(p) is not None]


def parse_blob(blob: str) -> list[Signal]:
    """Parse a blob possibly containing multiple stacked signals."""
    signals: list[Signal] = []
    for chunk in split_messages(blob):
        sig = parse_message(chunk)
        if sig is not None:
            signals.append(sig)
    return signals
