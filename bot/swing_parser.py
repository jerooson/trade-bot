"""
Parser for the Will-the-Rocket "real-time swing-trade" channel.

This channel posts STRUCTURED trade actions: entry, add, reduce, close,
stop-loss trigger, stop-loss update, position update. Each message has
a header line (with an emoji) and "Label: value" lines below.

Both English-style labels (Ticker / Trade Type / Position Size / ...) and
Chinese-style labels (股票 / 操作 / 价格 / 仓位 / 止损 / ...) are used,
sometimes within the same message type, so we accept both via aliases.

The parser is pure: text in, `TradeAction | None` out. We don't track state.
A separate consumer can fold a list of TradeActions chronologically to
derive a "current open positions" view.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Any


# ---------------------------------------------------------------------------
# Action kinds.
# ---------------------------------------------------------------------------

class ActionKind(str, Enum):
    """Logical category of a swing-channel post.

    The channel uses both Simplified ("平仓") and Traditional ("平倉") chars
    for the same concept; we collapse them onto a single enum value.
    """
    ENTRY = "ENTRY"                 # 🚨 正股交易 (buy-in / open new position)
    ADD = "ADD"                     # 🚨 正股加仓 (add to existing position)
    REDUCE = "REDUCE"               # 🚨 正股减仓 (partial sell)
    CLOSE = "CLOSE"                 # 🚨 正股平仓 / 平倉 (full exit)
    STOP_TRIGGER = "STOP_TRIGGER"   # 🛑 止损提醒 (stop-loss hit)
    STOP_UPDATE = "STOP_UPDATE"     # 🛡️ 正股止损更新 (stop raised/changed)
    POSITION_UPDATE = "POSITION_UPDATE"  # 📈 持仓股票提醒 / 🚨 正股仓位更新 (P/L info)


class Side(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


# ---------------------------------------------------------------------------
# Header detection -- match Chinese phrase, ignore emoji noise.
# ---------------------------------------------------------------------------

_HEADER_PATTERNS: list[tuple[re.Pattern[str], ActionKind]] = [
    # Order matters: match more specific phrases (with 仓位) before generic 正股交易.
    (re.compile(r"正股仓位更新|仓位更新"), ActionKind.POSITION_UPDATE),
    (re.compile(r"正股止损更新"),         ActionKind.STOP_UPDATE),
    (re.compile(r"止损提醒"),             ActionKind.STOP_TRIGGER),
    (re.compile(r"正股加仓"),             ActionKind.ADD),
    (re.compile(r"正股减仓"),             ActionKind.REDUCE),
    (re.compile(r"正股平[仓倉]"),         ActionKind.CLOSE),
    (re.compile(r"正股交易"),             ActionKind.ENTRY),
    (re.compile(r"持仓股票提醒|持倉股票提醒"), ActionKind.POSITION_UPDATE),
]


def detect_kind(text: str) -> ActionKind | None:
    for pat, kind in _HEADER_PATTERNS:
        if pat.search(text):
            return kind
    return None


# ---------------------------------------------------------------------------
# Field extraction.
# ---------------------------------------------------------------------------

# Each field is "Label: value" on a line. Labels may be ASCII or CJK.
# We tolerate both ":" and Chinese full-width "：".
_FIELD_RE = re.compile(
    r"^\s*([\w\u4e00-\u9fff][\w\u4e00-\u9fff \-/]*?)\s*[:：]\s*(.+?)\s*$",
    re.MULTILINE,
)

# English- and Chinese-style label aliases mapped onto canonical field names.
_LABEL_ALIASES: dict[str, str] = {
    # Ticker
    "ticker": "ticker",
    "股票": "ticker",
    # Side / type / action
    "trade type": "side",
    "type": "side",
    "action": "action_text",
    "操作": "action_text",
    # Price (entry price for the action OR avg cost for updates)
    "price": "price",
    "价格": "price",
    "entry": "price",
    "avg cost": "avg_cost",
    "均价": "avg_cost",
    "cost basis": "avg_cost",
    "current price": "current_price",
    # Position size
    "position size": "position_size",
    "size": "position_size",
    "仓位": "position_size",
    "倉位": "position_size",
    # Stop loss
    "stop loss": "stop_loss",
    "stop": "stop_loss",
    "止损": "stop_loss",
    "止損": "stop_loss",
    "新止损": "stop_loss_new",
    "新止損": "stop_loss_new",
    "止损类型": "stop_type",
    "止損類型": "stop_type",
    # P/L
    "profit": "profit",
    "盈亏": "profit",
    "盈虧": "profit",
    # Misc
    "posted by": "posted_by",
}

# Pull a numeric value out of "$873.00", "+50%", "1/2", etc.
_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")

# Match a bare fraction like "1/8" or "1/16". Used to pull the trim amount out
# of an action_text such as "🟠 卖出减仓 1/8" or "🟠 Sell to trim 1/4". We
# require both numerator and denominator so we don't accidentally match dates
# or other slashes; we also forbid an extra digit on either side so "1/100x"
# wouldn't be misread.
_FRACTION_RE = re.compile(r"(?<!\d)(\d+/\d+)(?!\d)")


def _parse_number(value: str) -> float | None:
    """Extract the first float from a label-value, or None."""
    if not value:
        return None
    m = _NUMBER_RE.search(value)
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def _parse_side(value: str) -> Side | None:
    v = value.upper()
    if "LONG" in v or "做多" in value or "买入" in value or "買入" in value:
        return Side.LONG
    if "SHORT" in v or "做空" in value or "卖空" in value or "賣空" in value:
        return Side.SHORT
    return None


def _parse_position_fraction(value: str | None) -> float | None:
    """
    Convert "1/2" -> 0.5, "3/4" -> 0.75, "+1/4 -> 3/4" -> 0.75 (the new total).

    For deltas like "+1/4" or arrows like "1/2 -> 7/8" we return the FINAL
    fraction (right-hand side of the arrow if present, or the fraction after
    a "+"). Falls back to None if we can't parse cleanly.
    """
    if not value:
        return None
    text = value.replace("→", "->").replace(" ", "")
    # If there's an arrow, take the right-hand side.
    if "->" in text:
        text = text.split("->", 1)[1]
    # Strip a leading "+" or "-".
    text = text.lstrip("+-")
    m = re.match(r"^(\d+)/(\d+)$", text)
    if not m:
        return None
    num, den = int(m.group(1)), int(m.group(2))
    if den == 0:
        return None
    return num / den


# ---------------------------------------------------------------------------
# Dataclass.
# ---------------------------------------------------------------------------

@dataclass
class TradeAction:
    """One structured trade post from the swing-trade channel."""

    kind: ActionKind
    ticker: str
    side: Side | None = None
    # Numeric levels.
    price: float | None = None        # entry/exit/current price for the action
    avg_cost: float | None = None     # cost basis after the action
    stop_loss: float | None = None    # numeric stop, when explicit price given
    profit_pct: float | None = None
    # Position sizing.
    position_size: str | None = None  # raw string, e.g. "1/2", "+1/4 -> 3/4"
    position_fraction: float | None = None  # 0..1, derived from position_size
    delta_size: str | None = None     # for REDUCE/trim: the AMOUNT sold, e.g. "1/8".
                                      # The channel's reduce posts don't say the
                                      # new total -- only the delta -- so we keep
                                      # this separate from position_size.
    # Free-form string extras (preserved verbatim for display).
    action_text: str | None = None    # "🟢 买入开仓 (做多)" etc.
    stop_loss_label: str | None = None  # "无", "保本 (均价)", or "$X"
    stop_type: str | None = None      # "立即" / "盘后" etc.
    posted_by: str | None = None
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
        """ENTRY / ADD / REDUCE / CLOSE / STOP_TRIGGER are actionable.

        Position-update P/L pings and stop-update raises are informational.
        """
        return self.kind in (
            ActionKind.ENTRY, ActionKind.ADD, ActionKind.REDUCE,
            ActionKind.CLOSE, ActionKind.STOP_TRIGGER,
        )


# ---------------------------------------------------------------------------
# Parser.
# ---------------------------------------------------------------------------

def parse_swing(text: str) -> TradeAction | None:
    """
    Parse one Discord message from the swing-trade channel into a TradeAction,
    or return None if it doesn't match a known shape.
    """
    if not text or not text.strip():
        return None

    kind = detect_kind(text)
    if kind is None:
        return None

    fields: dict[str, str] = {}
    for m in _FIELD_RE.finditer(text):
        label = m.group(1).strip().lower()
        value = m.group(2).strip()
        canonical = _LABEL_ALIASES.get(label)
        if canonical and canonical not in fields:
            fields[canonical] = value

    ticker = (fields.get("ticker") or "").upper().strip()
    if not ticker:
        return None

    # Side: most messages have "Trade Type: LONG" or "操作: 🟢 买入..." etc.
    side: Side | None = None
    if "side" in fields:
        side = _parse_side(fields["side"])
    if side is None and "action_text" in fields:
        side = _parse_side(fields["action_text"])

    # Stop-loss handling: prefer "新止损" (new stop) when this is an UPDATE.
    stop_label: str | None = fields.get("stop_loss_new") or fields.get("stop_loss")
    stop_loss_num: float | None = None
    if stop_label and stop_label.strip() not in ("无", "無", ""):
        # "保本 (均价)" -> no number, but we still keep the label.
        stop_loss_num = _parse_number(stop_label)

    # Numeric extraction.
    price = _parse_number(fields.get("price", "")) or _parse_number(fields.get("current_price", ""))
    avg_cost = _parse_number(fields.get("avg_cost", ""))
    profit_pct = _parse_number(fields.get("profit", ""))

    # Position size.
    pos_size_raw = fields.get("position_size")
    pos_size_frac = _parse_position_fraction(pos_size_raw) if pos_size_raw else None

    # For REDUCE, the size lives inline in the action text (e.g. "卖出减仓 1/8"
    # or "Sell to trim 1/4") rather than in a "Position Size" field. Pull it
    # into delta_size so the UI can show it. ENTRY/ADD/CLOSE all use the
    # explicit "仓位:" field, which we already parsed above.
    delta_size: str | None = None
    action_text = fields.get("action_text")
    if kind is ActionKind.REDUCE and pos_size_raw is None and action_text:
        m = _FRACTION_RE.search(action_text)
        if m:
            delta_size = m.group(1)

    return TradeAction(
        kind=kind,
        ticker=ticker,
        side=side,
        price=price,
        avg_cost=avg_cost,
        stop_loss=stop_loss_num,
        stop_loss_label=stop_label,
        profit_pct=profit_pct,
        position_size=pos_size_raw,
        position_fraction=pos_size_frac,
        delta_size=delta_size,
        action_text=action_text,
        stop_type=fields.get("stop_type"),
        posted_by=fields.get("posted_by"),
        raw_text=text,
    )
