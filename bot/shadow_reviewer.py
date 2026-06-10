"""
Event-triggered Robinhood shadow reviewer.

Watches the DRY_RUN executor's proposed-orders JSONL and invokes Codex CLI only
for fresh accepted ENTRY and REDUCE decisions. Codex may call Robinhood's
read-only tools and review_equity_order, but this process never places orders.

Run this on the VPS host (not in Docker) because Codex CLI and Robinhood OAuth
credentials live under the deploy user's home directory.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from bot.executor import TailReader, _fraction_of

log = logging.getLogger("bot.shadow_reviewer")


@dataclass(frozen=True)
class ShadowConfig:
    orders_path: Path
    ledger_path: Path
    codex_command: str
    budget_per_ticker: float
    max_age_s: float
    poll_interval_s: float
    codex_timeout_s: float


@dataclass
class ShadowRecord:
    reviewed_at: str
    dedupe_key: str
    proposal_id: str | None
    ticker: str
    signal_kind: str
    action: str
    status: str
    rationale: str
    expected_usd: float | None
    proposal_usd: float | None
    quantity: float | None
    codex_exit_code: int | None = None
    codex_stdout: str | None = None
    codex_stderr: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_config() -> ShadowConfig:
    load_dotenv()
    return ShadowConfig(
        orders_path=Path(
            os.environ.get("EXECUTOR_ORDERS_PATH", "./logs/proposed_orders.jsonl")
        ),
        ledger_path=Path(
            os.environ.get(
                "SHADOW_REVIEW_LEDGER_PATH",
                "./logs/robinhood_shadow_reviews.jsonl",
            )
        ),
        codex_command=os.environ.get("SHADOW_REVIEW_CODEX_COMMAND", "codex"),
        budget_per_ticker=float(
            os.environ.get("EXECUTOR_BUDGET_PER_TICKER", "20")
        ),
        max_age_s=float(os.environ.get("SHADOW_REVIEW_MAX_AGE_S", "300")),
        poll_interval_s=float(os.environ.get("SHADOW_REVIEW_POLL_INTERVAL_S", "1")),
        codex_timeout_s=float(os.environ.get("SHADOW_REVIEW_CODEX_TIMEOUT_S", "120")),
    )


def _parse_dt(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.replace(tzinfo=dt.tzinfo or timezone.utc)


def _dedupe_key(proposal: dict[str, Any]) -> str:
    signal = proposal.get("signal") or {}
    message_id = signal.get("message_id")
    if message_id is not None:
        return f"{message_id}:{proposal.get('ticker')}:{proposal.get('signal_kind')}"
    return str(proposal.get("id") or "")


def _load_seen(path: Path) -> set[str]:
    seen: set[str] = set()
    if not path.exists():
        return seen
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("dedupe_key"):
                seen.add(str(row["dedupe_key"]))
    return seen


def _append_record(record: ShadowRecord, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")


def _expected_usd(proposal: dict[str, Any], budget: float) -> float | None:
    signal = proposal.get("signal") or {}
    kind = proposal.get("signal_kind")
    if kind == "ENTRY":
        fraction = signal.get("position_fraction")
        if not isinstance(fraction, (int, float)) or fraction <= 0:
            return None
        return round(budget * float(fraction), 4)
    if kind == "REDUCE":
        fraction = _fraction_of(signal.get("delta_size"))
        before = (proposal.get("book_before") or {}).get("ticker_position") or {}
        deployed = before.get("deployed_usd")
        if fraction is None or not isinstance(deployed, (int, float)):
            return None
        return min(round(budget * fraction, 4), float(deployed))
    return None


def validate_proposal(
    proposal: dict[str, Any],
    config: ShadowConfig,
    *,
    now: datetime | None = None,
) -> tuple[bool, str, float | None]:
    kind = proposal.get("signal_kind")
    action = proposal.get("action")
    signal = proposal.get("signal") or {}

    if (kind, action) not in {("ENTRY", "BUY"), ("REDUCE", "SELL")}:
        return False, "only accepted ENTRY buys and REDUCE sells are reviewed", None
    if signal.get("side") == "SHORT":
        return False, "SHORT proposals are not supported", None
    if not proposal.get("ticker"):
        return False, "missing ticker", None

    decided_at = _parse_dt(proposal.get("decided_at"))
    if decided_at is None:
        return False, "missing or invalid decided_at", None
    age_s = ((now or datetime.now(timezone.utc)) - decided_at).total_seconds()
    if age_s < -30 or age_s > config.max_age_s:
        return False, f"proposal is stale ({age_s:.1f}s old)", None

    expected = _expected_usd(proposal, config.budget_per_ticker)
    actual = proposal.get("usd_amount")
    if expected is None or not isinstance(actual, (int, float)):
        return False, "could not verify proportional USD sizing", expected
    if abs(float(actual) - expected) > 0.01:
        return (
            False,
            f"USD sizing mismatch: expected ${expected:.4f}, proposal ${float(actual):.4f}",
            expected,
        )
    if actual <= 0.01 or actual > config.budget_per_ticker + 0.01:
        return False, f"proposal USD amount ${actual:.4f} is outside limits", expected

    if kind == "REDUCE":
        quantity = proposal.get("shares_estimate")
        if not isinstance(quantity, (int, float)) or quantity <= 0:
            return False, "REDUCE has no usable share quantity", expected

    return True, "eligible for Robinhood shadow review", expected


def build_prompt(proposal: dict[str, Any], expected_usd: float) -> str:
    kind = proposal["signal_kind"]
    ticker = proposal["ticker"]
    amount = float(proposal["usd_amount"])

    if kind == "ENTRY":
        order_details = (
            f"Review a BUY for {ticker} using a regular-hours market dollar order "
            f"for exactly ${amount:.4f}."
        )
    else:
        quantity = float(proposal["shares_estimate"])
        order_details = (
            f"Review a SELL for {ticker} using a regular-hours market quantity order "
            f"for exactly {quantity:.6f} shares."
        )

    return (
        "This is SHADOW REVIEW ONLY. Never place or cancel an order. "
        "Use only the Robinhood Agentic account. First use read-only tools to verify "
        "the Agentic account, actual positions, buying power, and ticker tradability. "
        f"Then call review_equity_order once. {order_details} "
        f"The independently verified $20-budget proportional amount is ${expected_usd:.4f}. "
        "Report the quote, estimated order details, alerts, and whether this would be "
        "ready to submit. Do not call place_equity_order."
    )


def invoke_codex(
    proposal: dict[str, Any],
    expected_usd: float,
    config: ShadowConfig,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            config.codex_command,
            "exec",
            "--ephemeral",
            "--sandbox",
            "read-only",
            build_prompt(proposal, expected_usd),
        ],
        capture_output=True,
        text=True,
        timeout=config.codex_timeout_s,
        check=False,
    )


def review_one(proposal: dict[str, Any], config: ShadowConfig) -> ShadowRecord:
    now = datetime.now(timezone.utc).isoformat()
    key = _dedupe_key(proposal)
    eligible, reason, expected = validate_proposal(proposal, config)
    base = dict(
        reviewed_at=now,
        dedupe_key=key,
        proposal_id=proposal.get("id"),
        ticker=str(proposal.get("ticker") or ""),
        signal_kind=str(proposal.get("signal_kind") or ""),
        action=str(proposal.get("action") or ""),
        expected_usd=expected,
        proposal_usd=proposal.get("usd_amount"),
        quantity=proposal.get("shares_estimate"),
    )
    if not eligible or expected is None:
        return ShadowRecord(status="SKIPPED", rationale=reason, **base)

    try:
        result = invoke_codex(proposal, expected, config)
    except subprocess.TimeoutExpired as exc:
        return ShadowRecord(
            status="FAILED",
            rationale=f"Codex timed out after {config.codex_timeout_s:.0f}s",
            codex_stdout=exc.stdout,
            codex_stderr=exc.stderr,
            **base,
        )
    except OSError as exc:
        return ShadowRecord(
            status="FAILED",
            rationale=f"failed to launch Codex: {exc}",
            **base,
        )

    return ShadowRecord(
        status="REVIEWED" if result.returncode == 0 else "FAILED",
        rationale=reason if result.returncode == 0 else "Codex review exited non-zero",
        codex_exit_code=result.returncode,
        codex_stdout=result.stdout.strip() or None,
        codex_stderr=result.stderr.strip() or None,
        **base,
    )


def run(config: ShadowConfig) -> None:
    seen = _load_seen(config.ledger_path)
    tail = TailReader(config.orders_path, start_at_end=True)
    log.info("shadow reviewer watching %s", config.orders_path)
    log.info("review ledger -> %s", config.ledger_path)
    log.info("eligible actions: fresh ENTRY buys and REDUCE sells only")

    while True:
        for proposal in tail.read_new_records():
            key = _dedupe_key(proposal)
            if not key or key in seen:
                continue
            record = review_one(proposal, config)
            _append_record(record, config.ledger_path)
            seen.add(key)
            log.info(
                "%s %s %s usd=%s :: %s",
                record.status,
                record.signal_kind,
                record.ticker,
                record.proposal_usd,
                record.rationale,
            )
        time.sleep(config.poll_interval_s)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    run(load_config())


if __name__ == "__main__":
    main()
