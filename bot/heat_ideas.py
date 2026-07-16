"""Parse and materialize Heat's Discord day-trade ideas.

The listener owns the append-only idea log.  The dashboard API owns the
append-only decision log and the small global settings file.  Keeping those
writers separate makes the hand-off to the host day-trader deterministic.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from bot.leveraged_etfs import candidate_symbols


HEAT_IDEAS_PATH = Path("logs/heat_ideas.jsonl")
HEAT_DECISIONS_PATH = Path("state/heat_idea_decisions.jsonl")
HEAT_SETTINGS_PATH = Path("state/heat_settings.json")
HEAT_ATTACHMENTS_DIR = Path("logs/heat_attachments")

_TICKER_RE = re.compile(r"(?<![A-Za-z0-9])([A-Z][A-Z0-9.-]{0,5})(?![A-Za-z0-9])")
_CASH_TAG_RE = re.compile(r"(?<![A-Za-z0-9])\$([A-Za-z][A-Za-z0-9.-]{0,5})(?![A-Za-z0-9])")
_MIXED_CASE_TICKERS = {
    # Heat often types large-cap symbols like ordinary names (for example
    # ``Msft``). Keep this curated: accepting every mixed-case word is what
    # previously turned ``Alter`` into ticker ``A``.
    "AAPL", "AMD", "AMZN", "AVGO", "COIN", "GOOG", "GOOGL", "IBM",
    "INTC", "META", "MSFT", "MSTR", "MU", "NFLX", "NVDA", "PLTR",
    "QQQ", "SMH", "SOXX", "SPY", "TSLA",
}
_BLOCKED_TICKERS = {
    "AH", "AI", "ATH", "CALL", "CUP", "DCA", "EMA", "EOD", "ETF",
    "FIB", "HOD", "LOD", "LONG", "MA", "OTM", "PM", "POC", "PUT",
    "RSI", "SHORT", "SL", "SPX", "TP", "TRIM", "USD", "VAH", "VAL",
    "VWAP", "YDH", "YDL",
}
_OPTION_RE = re.compile(r"(?:期权|\b(?:call|put|calls|puts)\b|\d+[CP]\b)", re.I)
_SHORT_RE = re.compile(r"(?:做空|看空|空单|\bshort\b)", re.I)
_BULLISH_RE = re.compile(r"(?:做多|看多|\b(?:long|bullish)\b)", re.I)
_BEARISH_RE = re.compile(r"(?:做空|看空|空单|\b(?:short|bearish)\b)", re.I)
_BELOW_RE = re.compile(
    r"(?:跌破|低于|below|under|break(?:s|ing)?\s*(?:below|under))",
    re.I,
)
_RISK_RE = re.compile(r"(?:极高风险|高风险|风险很高|谨慎|小仓位|轻仓)", re.I)
_SWING_DCA_RE = re.compile(
    r"(?:\bDCA\b|\bswing\b|multi[- ]?day|抄底|分批|波段|中长线|隔夜|持有几天)",
    re.I,
)
_POSITION_UPDATE_RE = re.compile(
    r"(?:我买|买了|已经买|买入了|持仓|仓位|减仓|卖出|清仓|平仓|止损|止盈|交易策略|原交易计划|"
    r"买早了|收不回|my position|I bought|bought|sold)",
    re.I,
)
_MARKET_CONTEXT_RE = re.compile(
    r"(?:市场|资金轮换|大盘|板块|全面下跌|目前站上|已经站上|"
    r"market breadth|rotation|risk[- ]?on|risk[- ]?off)",
    re.I,
)
_WATCH_RE = re.compile(r"(?:关注|留意|能否|等待|watch)", re.I)
_ENTRY_INTENT_RE = re.compile(
    r"(?:关注|留意|突破|站上|超过|高于|买入|买了|买点|建仓|做多|考虑操作|看好|bought|breakout|"
    r"break\s*(?:out|above)|buy|long|reclaim)",
    re.I,
)
_TRIGGER_PATTERNS = (
    re.compile(
        r"(?:站上|突破|超过|高于|above|over|reclaim(?:s|ed)?|break(?:s|ing)?\s*(?:above|over)?)"
        r"[^0-9]{0,18}\$?([0-9]+(?:\.[0-9]+)?)",
        re.I,
    ),
    re.compile(
        r"\$?([0-9]+(?:\.[0-9]+)?)\s*(?:以上)?\s*(?:突破|站上|breakout|break\s*above)",
        re.I,
    ),
    re.compile(
        r"\$?([0-9]+(?:\.[0-9]+)?)\s*(?:附近|左右)?\s*"
        r"(?:买入|买了|买点|建仓|做多|bought|buy)",
        re.I,
    ),
    re.compile(
        r"(?:跌破|低于|below|under|break(?:s|ing)?\s*(?:below|under))"
        r"[^0-9]{0,18}\$?([0-9]+(?:\.[0-9]+)?)",
        re.I,
    ),
)

_DIRECT_BUY_TICKER_PATTERNS = (
    re.compile(
        r"\$?[0-9]+(?:\.[0-9]+)?\s*(?:附近|左右)?\s*"
        r"(?:买入|买了|买点|建仓|做多)[^A-Z0-9]{0,10}\$?([A-Z][A-Z0-9.-]{0,5})",
        re.I,
    ),
    re.compile(
        r"\$?([A-Z][A-Z0-9.-]{0,5})[^0-9]{0,12}\$?[0-9]+(?:\.[0-9]+)?\s*"
        r"(?:买入|买了|买点|建仓|做多|bought|buy)",
        re.I,
    ),
)


def _ticker_from(text: str) -> str | None:
    for pattern in _DIRECT_BUY_TICKER_PATTERNS:
        match = pattern.search(text or "")
        if match:
            ticker = match.group(1).upper()
            if len(ticker) > 1 and ticker not in _BLOCKED_TICKERS:
                return ticker
    cash_tag = _CASH_TAG_RE.search(text or "")
    if cash_tag:
        ticker = cash_tag.group(1).upper().rstrip(".")
        if ticker not in _BLOCKED_TICKERS:
            return ticker
    for match in _TICKER_RE.finditer(text or ""):
        ticker = match.group(1).upper().rstrip(".")
        if len(ticker) > 1 and ticker not in _BLOCKED_TICKERS:
            return ticker
    for token in re.findall(
        r"(?<![A-Za-z0-9])([A-Za-z][A-Za-z0-9.-]{1,5})(?![A-Za-z0-9])",
        text or "",
    ):
        ticker = token.upper().rstrip(".")
        if ticker in _MIXED_CASE_TICKERS:
            return ticker
    return None


def _classification_from(text: str, trigger: float | None) -> str:
    """Classify Heat content before it reaches trade approval controls."""
    if _SWING_DCA_RE.search(text or ""):
        return "swing_dca"
    if _POSITION_UPDATE_RE.search(text or ""):
        return "position_update"
    if trigger is not None:
        return "actionable_setup"
    if _WATCH_RE.search(text or ""):
        return "needs_level"
    if _MARKET_CONTEXT_RE.search(text or ""):
        return "market_context"
    return "needs_level"


def _trigger_from(text: str) -> float | None:
    for pattern in _TRIGGER_PATTERNS:
        for match in pattern.finditer(text or ""):
            suffix = (text[match.end():match.end() + 8]).lower()
            if re.match(r"\s*(?:日线|均线|ema\b|ma\b)", suffix, re.I):
                continue
            try:
                value = float(match.group(1))
            except (TypeError, ValueError):
                continue
            if value > 0:
                return value
    return None


def _direction_from(text: str) -> str | None:
    """Return an unambiguous economic direction, not the option side."""
    bullish = bool(_BULLISH_RE.search(text or ""))
    bearish = bool(_BEARISH_RE.search(text or ""))
    if bullish == bearish:
        return None
    return "long" if bullish else "short"


def parse_heat_idea(
    text: str,
    *,
    reply_text: str = "",
    idea_id: str,
    created_at: str,
) -> dict[str, Any] | None:
    """Return a reviewable Heat idea, or ``None`` for non-entry chatter.

    A ticker may come from the message being replied to, but an executable
    trigger must always be stated in Heat's own message.
    """
    body = (text or "").strip()
    context = (reply_text or "").strip()
    ticker = _ticker_from(body) or _ticker_from(context)
    if not ticker:
        return None
    option_message = bool(_OPTION_RE.search(body))
    if option_message:
        # Heat's option posts are performance/show-and-tell, not actionable
        # trade opportunities. Do not surface them as ideas or review items.
        return None
    trigger = _trigger_from(body)
    classification = _classification_from(body, trigger)
    direction = _direction_from(body)
    if direction is None and (_ENTRY_INTENT_RE.search(body) or _WATCH_RE.search(body)):
        direction = "long"
    if (
        not _ENTRY_INTENT_RE.search(body)
        and not _SHORT_RE.search(body)
        and not _WATCH_RE.search(body)
        and classification not in {
            "market_context", "position_update", "swing_dca"
        }
    ):
        return None

    trigger_operator = "below" if _BELOW_RE.search(body) else "above"
    mapping_supported = bool(direction and candidate_symbols(ticker, direction))
    # Heat explicitly labels some trades as unusually risky. Preserve the
    # parsed level for a fast review, but never auto-approve those messages.
    # Option posts were already discarded above, so only cash-equity/index
    # language with a supported execution route can auto-approve.
    auto_eligible = bool(
        trigger is not None
        and classification == "actionable_setup"
        and direction is not None
        and mapping_supported
        and not _RISK_RE.search(body)
    )
    return {
        "event_type": "idea",
        "id": str(idea_id),
        "ticker": ticker,
        "trigger_price": trigger,
        "target_price": None,
        "direction": direction,
        "trigger_operator": trigger_operator,
        "mapping_supported": mapping_supported,
        "leveraged_candidates": candidate_symbols(ticker, direction or ""),
        "setup": body[:500] or "Heat chart watch",
        "text": body,
        "reply_text": context[:1000] or None,
        "attachments": [],
        "auto_eligible": auto_eligible,
        "confidence": "high" if auto_eligible else "review",
        "classification": classification,
        "created_at": created_at,
    }


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            try:
                rows.append(json.loads(line))
            except (json.JSONDecodeError, TypeError):
                continue
    return rows


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def materialize_heat_ideas(
    ideas: Iterable[dict[str, Any]],
    decisions: Iterable[dict[str, Any]] = (),
) -> list[dict[str, Any]]:
    """Fold idea, attachment-update, and dashboard-decision events."""
    by_id: dict[str, dict[str, Any]] = {}
    for event in ideas:
        event_type = event.get("event_type", "idea")
        if event_type == "idea":
            idea_id = str(event.get("id", ""))
            if idea_id:
                by_id[idea_id] = dict(event)
        elif event_type == "attachment_update":
            idea = by_id.get(str(event.get("idea_id", "")))
            if idea is not None:
                existing = list(idea.get("attachments") or [])
                for filename in event.get("attachments") or []:
                    if filename not in existing:
                        existing.append(filename)
                idea["attachments"] = existing

    latest_decision: dict[str, dict[str, Any]] = {}
    for decision in decisions:
        idea_id = str(decision.get("idea_id", ""))
        if idea_id:
            latest_decision[idea_id] = decision

    for idea_id, idea in by_id.items():
        decision = latest_decision.get(idea_id)
        if decision:
            idea["decision"] = decision.get("decision")
            for field in (
                "ticker", "trigger_price", "target_price", "setup",
                "direction", "trigger_operator",
            ):
                if field in decision:
                    idea[field] = decision[field]
            idea["decided_at"] = decision.get("decided_at")
            idea["status"] = decision.get("decision")
        elif idea.get("auto_eligible"):
            idea["decision"] = "approved"
            idea["status"] = "auto_approved"
        else:
            idea["decision"] = None
            idea["status"] = "needs_review"

        body = str(idea.get("text") or "")
        reply = str(idea.get("reply_text") or "")
        inferred_ticker = (
            _ticker_from(body) or _ticker_from(reply)
            if body or reply
            else str(idea.get("ticker") or "").upper() or None
        )
        if decision and decision.get("ticker"):
            inferred_ticker = str(decision["ticker"]).upper()
        idea["ticker"] = inferred_ticker
        if decision and decision.get("decision") == "approved":
            idea["classification"] = "actionable_setup"
        else:
            idea["classification"] = _classification_from(
                str(idea.get("text") or ""), idea.get("trigger_price")
            )

        direction = str(idea.get("direction") or "")
        candidates = candidate_symbols(str(idea.get("ticker") or ""), direction)
        idea["mapping_supported"] = bool(candidates)
        idea["leveraged_candidates"] = candidates

    # Invalid legacy captures stay in the append-only audit log but disappear
    # from the operational UI. Example: ``Alter`` used to become ticker ``A``.
    visible = [idea for idea in by_id.values() if idea.get("ticker")]
    return sorted(visible, key=lambda row: row.get("created_at") or "", reverse=True)


def load_materialized_heat_ideas(
    ideas_path: Path = HEAT_IDEAS_PATH,
    decisions_path: Path = HEAT_DECISIONS_PATH,
) -> list[dict[str, Any]]:
    return materialize_heat_ideas(read_jsonl(ideas_path), read_jsonl(decisions_path))


def load_heat_settings(path: Path = HEAT_SETTINGS_PATH) -> dict[str, Any]:
    default = {"auto_trading_enabled": False, "updated_at": None}
    if not path.exists():
        return default
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default
    return {**default, **data}


def save_heat_settings(enabled: bool, path: Path = HEAT_SETTINGS_PATH) -> dict[str, Any]:
    data = {
        "auto_trading_enabled": bool(enabled),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return data
