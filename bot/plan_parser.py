"""
Pure-function parser for the Will-the-Rocket "swing trade plan" channel.

Unlike the live-signal channel, plan messages are free-form Chinese prose:
no Trigger:/Target:/Setup: fields, no fixed schema. Each message typically
discusses one ticker, mentions one or more price levels in the body
(breakout above, invalidation below, target zones), links a TradingView
chart, and may include a small glossary section ("技术名词解释").

Our extraction strategy is pragmatic and conservative:

  - Ticker:        prefer the embed's "BATS:TICKER ..." title if present;
                   otherwise pull the first uppercase token from the body
                   that looks like a US-equity ticker.
  - Watch levels:  extract ALL numeric values from the narrative section.
                   We deliberately do not pretend to know which one is the
                   "primary" level -- the UI shows them all as chips.
  - Narrative:     the body with the boilerplate footer + glossary stripped.
  - Chart URL:     first tradingview.com/x/<id>/ link.
  - Glossary:      key->definition pairs from the "技术名词解释" section.

This module is pure: no I/O, no Discord. We pass it raw `content` plus an
optional `embed_title` and get a `TradePlan | None` back.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any


# ---------------------------------------------------------------------------
# Markers we use to recognize / segment plan messages.
# ---------------------------------------------------------------------------

# Boilerplate footer lines. Anything from the first match downward is dropped.
_FOOTER_MARKERS = (
    "点击链接看高清技术图",
    "试着自己先找出",
    "以上为个人观点分享",
    "想好再按",
    "@everyone",
)

# Start of the optional inline glossary block.
_GLOSSARY_MARKER = "技术名词解释"

# A US-equity ticker pulled from prose: 1-5 uppercase letters,
# at a word boundary, optionally followed by punctuation/space.
# We restrict to length>=2 so single-letter words don't confuse us.
_TICKER_RE = re.compile(r"\b([A-Z]{2,5})\b")

# BATS:TICKER pattern from the embed title ("BATS:OKTA Chart Image by ...").
_EMBED_TICKER_RE = re.compile(r"\b(?:BATS|NASDAQ|NYSE|AMEX):([A-Z]{1,5})\b")

# TradingView snapshot links.
_CHART_URL_RE = re.compile(r"https?://(?:www\.)?tradingview\.com/x/[A-Za-z0-9]+/?")

# Number tokens. Plan prose throws around prices like "150-151", "127.57",
# "8EMA", "200 SMA", "@everyone". We want true price levels, not "8" from
# "8EMA" or "200" from "200sma". Heuristics applied below in extraction.
#
# We deliberately don't match a leading "-" because (a) prices aren't negative
# and (b) ranges written as "150-151" should give us BOTH endpoints.
_NUMBER_RE = re.compile(r"(?<!\d)\d+(?:\.\d+)?")

# Tokens that look like a number followed immediately by an indicator name
# (e.g. "8EMA", "200sma", "20MA"). We want to ignore those numbers.
#
# Note: we use (?<!\d) instead of \b at the start because Python's \b treats
# CJK characters as word chars (re.UNICODE default), so "\b8" inside "回8EMA"
# fails to fire. The lookbehind says "not preceded by another digit", which
# is the only constraint we actually care about.
#
# Note: `\b` at the *end* would also fail with trailing CJK characters
# (e.g. "8EMA的"), because CJK is `\w` under re.UNICODE. Use a negative
# lookahead for "another ASCII letter" instead, so we still reject things
# like "8EMAS" but accept "8EMA" + Chinese / punctuation.
_INDICATOR_NUMBER_RE = re.compile(
    r"(?<!\d)(\d+(?:\.\d+)?)\s*(?:EMA|SMA|MA|RSI|VWAP|ATR|MACD)(?![A-Za-z])",
    re.IGNORECASE,
)

# Footer noise that sneaks before the boilerplate but is still not narrative.
_NOISE_LINES = (
    "技术名词解释:",
    "技术名词解释：",
)


# ---------------------------------------------------------------------------
# Dataclass.
# ---------------------------------------------------------------------------

@dataclass
class TradePlan:
    """One planned setup posted in the swing-trade channel."""

    ticker: str | None
    watch_levels: list[float] = field(default_factory=list)
    narrative: str = ""
    chart_url: str | None = None
    glossary: dict[str, str] = field(default_factory=dict)
    raw_text: str = ""
    received_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["received_at"] = self.received_at.isoformat()
        return d

    @property
    def is_actionable(self) -> bool:
        """A plan is actionable if we extracted a ticker AND at least one level."""
        return bool(self.ticker) and bool(self.watch_levels)


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------

def _strip_footer(text: str) -> str:
    """Remove the standard disclaimer footer that follows every plan message."""
    earliest: int | None = None
    for marker in _FOOTER_MARKERS:
        i = text.find(marker)
        if i != -1 and (earliest is None or i < earliest):
            earliest = i
    return text[:earliest].rstrip() if earliest is not None else text.rstrip()


def _split_glossary(body: str) -> tuple[str, dict[str, str]]:
    """
    The optional glossary section starts with "技术名词解释:" and contains
    "term: definition" pairs separated by commas/newlines. Split it off and
    return (narrative_without_glossary, glossary_dict).
    """
    idx = body.find(_GLOSSARY_MARKER)
    if idx == -1:
        return body, {}

    narrative = body[:idx].rstrip()
    glossary_block = body[idx + len(_GLOSSARY_MARKER):].lstrip(":：\n ")

    # Glossary entries are roughly "term: definition[,]" possibly multi-line.
    glossary: dict[str, str] = {}
    for chunk in re.split(r"[,，\n]\s*", glossary_block):
        chunk = chunk.strip().rstrip(",.，。 ")
        if not chunk:
            continue
        parts = re.split(r"[:：]", chunk, maxsplit=1)
        if len(parts) != 2:
            continue
        key, val = parts[0].strip(), parts[1].strip()
        if key and val:
            glossary[key] = val
    return narrative, glossary


def _extract_ticker(body: str, embed_title: str | None) -> str | None:
    """Prefer the BATS:TICKER token from the embed; fall back to body scan."""
    if embed_title:
        m = _EMBED_TICKER_RE.search(embed_title)
        if m:
            return m.group(1)

    # Fallback: first uppercase token in body that's not a noisy keyword.
    blocked = {"EMA", "SMA", "MA", "RSI", "VCP", "ATH", "ATL", "USA", "CEO",
               "ETF", "IPO", "IV", "PE", "EPS", "WTR", "BATS", "NASDAQ", "NYSE",
               "AMEX", "AM", "PM", "BOT", "APP", "URL"}
    for m in _TICKER_RE.finditer(body):
        tok = m.group(1)
        if tok in blocked:
            continue
        return tok
    return None


def _extract_levels(narrative: str) -> list[float]:
    """
    Pull every plausible price level from the narrative.

    Heuristics:
      - Drop numbers that are part of an indicator token like "8EMA" / "200sma".
      - Keep ranges expressed as "150-151" as TWO levels (low and high).
      - De-duplicate while preserving order.
    """
    masked = _INDICATOR_NUMBER_RE.sub(lambda _: "", narrative)
    seen: set[float] = set()
    levels: list[float] = []
    for m in _NUMBER_RE.finditer(masked):
        try:
            value = float(m.group(0))
        except ValueError:
            continue
        # Reject obviously-non-price values: typical equity prices are in
        # ($0.10, $10000). Anything outside is almost certainly a ticker code,
        # phone number, year, or stray digit.
        if value <= 0 or value > 10000:
            continue
        if value in seen:
            continue
        seen.add(value)
        levels.append(value)
    return levels


def _extract_chart_url(text: str) -> str | None:
    m = _CHART_URL_RE.search(text)
    return m.group(0) if m else None


# ---------------------------------------------------------------------------
# Public API.
# ---------------------------------------------------------------------------

def parse_plan(content: str, embed_title: str | None = None) -> TradePlan | None:
    """
    Turn a Discord message into a TradePlan, or return None if it doesn't
    look like one.

    A message qualifies as a plan if we can find at least a ticker AND a
    chart URL; otherwise it's likely just a chat aside ("看样子没人抓住TE").
    """
    if not content or not content.strip():
        return None

    body = _strip_footer(content)
    chart_url = _extract_chart_url(content)

    if not chart_url:
        # Without a chart, this is almost always a casual comment, not a plan.
        return None

    narrative_no_gloss, glossary = _split_glossary(body)

    # Strip the chart URL itself from the narrative so chips/levels don't pull
    # numbers out of the URL slug.
    narrative_clean = _CHART_URL_RE.sub("", narrative_no_gloss).strip()

    ticker = _extract_ticker(narrative_clean, embed_title)
    if not ticker:
        return None

    levels = _extract_levels(narrative_clean)

    return TradePlan(
        ticker=ticker,
        watch_levels=levels,
        narrative=narrative_clean,
        chart_url=chart_url,
        glossary=glossary,
        raw_text=content,
    )
