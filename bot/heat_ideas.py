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


HEAT_IDEAS_PATH = Path("logs/heat_ideas.jsonl")
HEAT_DECISIONS_PATH = Path("state/heat_idea_decisions.jsonl")
HEAT_SETTINGS_PATH = Path("state/heat_settings.json")
HEAT_ATTACHMENTS_DIR = Path("logs/heat_attachments")

_TICKER_RE = re.compile(r"(?<![A-Z0-9])\$?([A-Z][A-Z0-9.-]{0,5})(?![A-Z0-9])")
_BLOCKED_TICKERS = {
    "AI", "ATH", "CALL", "CUP", "DCA", "EMA", "ETF", "LONG", "MA",
    "OTM", "PUT", "RSI", "SHORT", "SPX", "TRIM", "USD", "VWAP",
}
_OPTION_RE = re.compile(r"(?:期权|\b(?:call|put|calls|puts)\b|\d+[CP]\b)", re.I)
_SHORT_RE = re.compile(r"(?:做空|看空|空单|\bshort\b)", re.I)
_MANAGEMENT_RE = re.compile(
    r"(?:减仓|止盈|落袋|卖出|清仓|平仓|runner|trim|take\s*profit|sold|sell)",
    re.I,
)
_ENTRY_INTENT_RE = re.compile(
    r"(?:关注|留意|突破|站上|超过|高于|买入|做多|考虑操作|看好|breakout|"
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
)


def _ticker_from(text: str) -> str | None:
    for match in _TICKER_RE.finditer(text or ""):
        ticker = match.group(1).upper().rstrip(".")
        if ticker not in _BLOCKED_TICKERS:
            return ticker
    return None


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
    if _OPTION_RE.search(body) or _SHORT_RE.search(body):
        return None
    if _MANAGEMENT_RE.search(body) and not _ENTRY_INTENT_RE.search(body):
        return None
    if not _ENTRY_INTENT_RE.search(body):
        return None

    trigger = _trigger_from(body)
    auto_eligible = trigger is not None
    return {
        "event_type": "idea",
        "id": str(idea_id),
        "ticker": ticker,
        "trigger_price": trigger,
        "target_price": None,
        "setup": body[:500] or "Heat chart watch",
        "text": body,
        "reply_text": context[:1000] or None,
        "attachments": [],
        "auto_eligible": auto_eligible,
        "confidence": "high" if auto_eligible else "review",
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
            for field in ("ticker", "trigger_price", "target_price", "setup"):
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

    return sorted(by_id.values(), key=lambda row: row.get("created_at") or "", reverse=True)


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
