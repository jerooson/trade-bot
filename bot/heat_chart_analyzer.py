"""One-shot Heat chart analysis worker.

The Discord listener saves and associates Heat's chart attachments.  This
host-side worker analyzes each new chart once with Codex, persists a confident
price level as a normal Heat approval, and never revisits the image after a
successful/complete analysis.  It is deliberately separate from the day
trader so image latency cannot delay quote, stop, or exit polling.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bot.heat_ideas import (
    HEAT_ATTACHMENTS_DIR,
    HEAT_DECISIONS_PATH,
    HEAT_IDEAS_PATH,
    append_jsonl as append_heat_jsonl,
    load_materialized_heat_ideas,
    read_jsonl,
)
from bot.leveraged_etfs import candidate_symbols


log = logging.getLogger("bot.heat_chart_analyzer")

ANALYSES_PATH = Path("state/heat_chart_analyses.jsonl")
POLL_INTERVAL_S = float(os.getenv("HEAT_CHART_POLL_INTERVAL_S", "5"))
CODEX_TIMEOUT_S = float(os.getenv("HEAT_CHART_CODEX_TIMEOUT_S", "900"))
MIN_CONFIDENCE = float(os.getenv("HEAT_CHART_MIN_CONFIDENCE", "0.85"))
RETRY_DELAYS_S = (60.0, 300.0, 900.0)

_ACTIONABLE_CLASSES = {"needs_level", "actionable_setup"}


def build_prompt(idea: dict[str, Any]) -> str:
    ticker = str(idea.get("ticker") or "").upper()
    direction = str(idea.get("direction") or "long").lower()
    setup = str(idea.get("setup") or idea.get("text") or "")
    return f"""Analyze this TradingView screenshot as a one-time Heat day-trade watch.

Ticker: {ticker}
Economic direction: {direction}
Heat text: {setup}

Identify the single clearest user-drawn actionable horizontal price level near
the latest candles. Distinguish historical commentary from a new entry rule:
words such as 'previously broke below' do not make the new trigger 'below'. For
a bullish reclaim/breakout, a resistance or reclaim line is normally 'above'.
Do not use indicator values, dates, volume-axis values, Fibonacci ratio labels,
or the live bid/ask boxes as the trigger.

Return only one JSON object with:
- ticker: string
- trigger_price: positive number or null
- trigger_operator: "above" or "below"
- confidence: number from 0 to 1
- rationale: short string

If no single level is clearly actionable, return trigger_price null and low
confidence. Do not place orders and do not call tools.
"""


def parse_analysis_output(stdout: str) -> dict[str, Any]:
    text = (stdout or "").strip()
    candidates = [text, *reversed([line.strip() for line in text.splitlines()])]
    for candidate in candidates:
        if not candidate.startswith("{") or not candidate.endswith("}"):
            continue
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    # Also accept pretty-printed or fenced JSON without depending on Markdown
    # formatting. raw_decode stops cleanly at the matching closing brace.
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            parsed, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    raise ValueError("Codex did not return a JSON object")


def validate_analysis(
    idea: dict[str, Any], analysis: dict[str, Any]
) -> tuple[bool, str, float | None, str | None, float]:
    ticker = str(idea.get("ticker") or "").upper()
    result_ticker = str(analysis.get("ticker") or "").upper()
    operator = str(analysis.get("trigger_operator") or "").lower()
    try:
        confidence = float(analysis.get("confidence") or 0)
    except (TypeError, ValueError):
        confidence = 0.0
    try:
        trigger = float(analysis["trigger_price"])
    except (KeyError, TypeError, ValueError):
        trigger = None

    if result_ticker != ticker:
        return False, "ticker_mismatch", trigger, operator or None, confidence
    if trigger is None or trigger <= 0:
        return False, "no_clear_level", trigger, operator or None, confidence
    if operator not in {"above", "below"}:
        return False, "invalid_operator", trigger, operator or None, confidence
    if confidence < MIN_CONFIDENCE:
        return False, "low_confidence", trigger, operator, confidence
    direction = str(idea.get("direction") or "").lower()
    if not candidate_symbols(ticker, direction):
        return False, "unsupported_execution_route", trigger, operator, confidence
    return True, "approved", trigger, operator, confidence


def invoke_codex(
    idea: dict[str, Any], image_path: Path, *, codex_command: str = "codex"
) -> subprocess.CompletedProcess[str]:
    cmd = [
        codex_command,
        "exec",
        "--ephemeral",
        "--ignore-user-config",
        "--sandbox",
        "read-only",
        "--skip-git-repo-check",
        build_prompt(idea),
        "-i",
        str(image_path),
    ]
    return subprocess.run(
        cmd,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=CODEX_TIMEOUT_S,
        check=False,
    )


def _completed_ids() -> set[str]:
    completed = {
        str(row.get("idea_id"))
        for row in read_jsonl(ANALYSES_PATH)
        if row.get("idea_id") and row.get("status") in {"approved", "reviewed"}
    }
    completed.update(
        str(row.get("idea_id"))
        for row in read_jsonl(HEAT_DECISIONS_PATH)
        if row.get("idea_id")
    )
    return completed


def analyze_one(
    idea: dict[str, Any], *, codex_command: str = "codex"
) -> tuple[bool, str]:
    idea_id = str(idea.get("id") or "")
    attachments = list(idea.get("attachments") or [])
    if not idea_id or not attachments:
        return True, "no_attachment"
    image_path = HEAT_ATTACHMENTS_DIR / str(attachments[-1])
    if not image_path.is_file():
        return False, "attachment_missing"

    result = invoke_codex(idea, image_path, codex_command=codex_command)
    if result.returncode != 0:
        return False, f"codex_exit_{result.returncode}"
    try:
        analysis = parse_analysis_output(result.stdout)
    except ValueError as exc:
        return False, str(exc)

    approved, reason, trigger, operator, confidence = validate_analysis(idea, analysis)
    now = datetime.now(timezone.utc).isoformat()
    if approved and trigger is not None and operator is not None:
        # Persist the executable watch first. If the process stops before the
        # audit line is written, the decision itself still prevents reanalysis.
        append_heat_jsonl(HEAT_DECISIONS_PATH, {
            "idea_id": idea_id,
            "decision": "approved",
            "ticker": str(idea.get("ticker") or "").upper(),
            "trigger_price": trigger,
            "target_price": idea.get("target_price"),
            "setup": idea.get("setup") or "Heat chart watch",
            "direction": str(idea.get("direction") or "long").lower(),
            "trigger_operator": operator,
            "good_til_cancelled": True,
            "source": "heat_chart_analyzer",
            "analysis_rationale": str(analysis.get("rationale") or "")[:500],
            "decided_at": now,
        })

    append_heat_jsonl(ANALYSES_PATH, {
        "idea_id": idea_id,
        "attachment": image_path.name,
        "status": "approved" if approved else "reviewed",
        "reason": reason,
        "ticker": analysis.get("ticker"),
        "trigger_price": trigger,
        "trigger_operator": operator,
        "confidence": confidence,
        "rationale": str(analysis.get("rationale") or "")[:500],
        "analyzed_at": now,
    })
    return True, reason


def pending_ideas() -> list[dict[str, Any]]:
    completed = _completed_ids()
    return [
        idea
        for idea in load_materialized_heat_ideas(HEAT_IDEAS_PATH, HEAT_DECISIONS_PATH)
        if str(idea.get("id") or "") not in completed
        and idea.get("attachments")
        and idea.get("trigger_price") is None
        and str(idea.get("classification") or "needs_level") in _ACTIONABLE_CLASSES
        and str(idea.get("direction") or "").lower() in {"long", "short"}
    ]


def main() -> None:
    attempts: dict[str, int] = {}
    retry_after: dict[str, float] = {}
    log.info(
        "Heat chart analyzer started. poll=%.1fs codex_timeout=%.0fs min_confidence=%.2f",
        POLL_INTERVAL_S,
        CODEX_TIMEOUT_S,
        MIN_CONFIDENCE,
    )
    while True:
        now = time.monotonic()
        for idea in pending_ideas():
            idea_id = str(idea.get("id") or "")
            if now < retry_after.get(idea_id, 0):
                continue
            log.info("Analyzing Heat chart idea=%s ticker=%s", idea_id, idea.get("ticker"))
            try:
                complete, reason = analyze_one(idea)
            except subprocess.TimeoutExpired:
                complete, reason = False, "codex_timeout"
            except Exception as exc:
                log.exception("Heat chart analysis failed idea=%s: %s", idea_id, exc)
                complete, reason = False, "unexpected_error"
            if complete:
                log.info("Heat chart analysis complete idea=%s result=%s", idea_id, reason)
                attempts.pop(idea_id, None)
                retry_after.pop(idea_id, None)
                continue
            attempt = attempts.get(idea_id, 0) + 1
            attempts[idea_id] = attempt
            delay = RETRY_DELAYS_S[min(attempt - 1, len(RETRY_DELAYS_S) - 1)]
            retry_after[idea_id] = time.monotonic() + delay
            log.warning(
                "Heat chart analysis retry idea=%s reason=%s in %.0fs",
                idea_id,
                reason,
                delay,
            )
        time.sleep(POLL_INTERVAL_S)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    main()
