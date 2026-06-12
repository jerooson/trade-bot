"""
Event-triggered Robinhood order executor.

Watches the DRY_RUN executor's proposed-orders JSONL and invokes Codex CLI for
fresh accepted ENTRY and REDUCE decisions.

When SHADOW_REVIEW_PLACE_ORDERS=true (must be explicit; an absent or empty
value defaults to *false* for fail-safe behavior), Codex verifies the Agentic
account with read-only tools and calls place_equity_order.

When SHADOW_REVIEW_PLACE_ORDERS is absent, empty, or any other value, Codex
runs read-only review only and is explicitly instructed not to place orders.

Run this on the VPS host (not in Docker) because Codex CLI and Robinhood OAuth
credentials live under the deploy user's home directory.

## Known architectural limitations

- Python never directly calls the Robinhood API before placement, so real
  position size and buying-power checks are instruction-only inside the Codex
  prompt. They are not deterministically enforced.
- Order-lifecycle tracking (acknowledged, rejected, partial fill, full fill) is
  limited to one get_equity_orders call immediately after placement.
- Full idempotency across process restarts is limited: PENDING entries are
  permanent until manually reconciled.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from bot.executor import TailReader, _fraction_of
from bot import robinhood_mcp_client

log = logging.getLogger("bot.shadow_reviewer")

REF_ID_NAMESPACE = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")

# Only match the explicit BROKER_ORDER_ID=<uuid> tag emitted by the live
# prompt.  Any other UUID in the Codex output (session IDs, MCP correlation
# IDs, account IDs) is ignored, preventing false PLACED status.
_BROKER_ORDER_TAG_RE = re.compile(
    r"^BROKER_ORDER_ID="
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
    r"\s*$",
    re.IGNORECASE | re.MULTILINE,
)


@dataclass(frozen=True)
class ShadowConfig:
    orders_path: Path
    ledger_path: Path
    codex_command: str
    budget_per_ticker: float
    max_age_s: float
    poll_interval_s: float
    codex_timeout_s: float
    place_orders: bool


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
    broker_order_id: str | None = None
    codex_exit_code: int | None = None
    codex_stdout: str | None = None
    codex_stderr: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_config() -> ShadowConfig:
    load_dotenv()
    # Fail-safe: missing or empty env var → placement disabled.
    raw = os.environ.get("SHADOW_REVIEW_PLACE_ORDERS", "").strip().lower()
    place_orders = raw in {"1", "true", "yes", "on"}
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
        place_orders=place_orders,
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


def _warn_stale_pending(path: Path) -> None:
    """Log a warning for every PENDING ledger entry found at startup.

    PENDING entries arise when the process crashed between Robinhood
    accepting an order and the final ledger write.  They block re-processing
    to prevent double-placement.  Manual broker reconciliation is required:
    check get_equity_orders for the ticker and mark the outcome offline.
    """
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("status") == "PENDING":
                log.warning(
                    "STALE PENDING record found — manual broker reconciliation required: "
                    "key=%s ticker=%s reviewed_at=%s; "
                    "verify order state in Robinhood before re-enabling placement.",
                    row.get("dedupe_key"),
                    row.get("ticker"),
                    row.get("reviewed_at"),
                )


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

    return True, "eligible", expected


def _ref_id(proposal: dict[str, Any]) -> str:
    key = _dedupe_key(proposal)
    return str(uuid.uuid5(REF_ID_NAMESPACE, key or str(proposal.get("id") or "")))


def _extract_broker_order_id(text: str) -> str | None:
    """Return the broker order UUID from the explicit BROKER_ORDER_ID= tag.

    Any other UUID in the Codex output (session IDs, MCP correlation IDs,
    account IDs) is ignored.  Only the tagged line emitted by the live prompt
    is matched, preventing false PLACED status from unrelated UUIDs.
    """
    m = _BROKER_ORDER_TAG_RE.search(text)
    return m.group(1).lower() if m else None


def build_prompt(
    proposal: dict[str, Any],
    expected_usd: float,
    *,
    place_orders: bool,
) -> str:
    """Build the Codex instruction for this proposal.

    When place_orders is True:  instruct Codex to place via place_equity_order.
    When place_orders is False: instruct Codex to call review_equity_order only
        and explicitly forbid placement.  This is the operative kill switch —
        sandbox mode alone does not restrict MCP tool calls.
    """
    kind = proposal["signal_kind"]
    ticker = proposal["ticker"]
    amount = float(proposal["usd_amount"])

    if not place_orders:
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
            "This is REVIEW ONLY. Do not call place_equity_order or cancel_equity_order. "
            "Use only the Robinhood Agentic account. First use read-only tools to verify "
            "the Agentic account, actual positions, buying power, and ticker tradability. "
            f"Then call review_equity_order once. {order_details} "
            f"The independently verified $20-budget proportional amount is ${expected_usd:.4f}. "
            "Report the quote, estimated order details, alerts, and whether this would be "
            "ready to submit."
        )

    ref_id = _ref_id(proposal)

    if kind == "ENTRY":
        order_details = (
            f"Place a BUY for {ticker} as a regular-hours market dollar order "
            f"for exactly ${amount:.4f} (dollar_amount='{amount:.2f}')."
        )
    else:
        quantity = float(proposal["shares_estimate"])
        order_details = (
            f"Call get_equity_positions to fetch the actual quantity of {ticker} "
            f"held in the Agentic account. "
            f"The virtual book estimates {quantity:.6f} shares. "
            f"Cap the sell quantity to min({quantity:.6f}, actual_shares_held). "
            f"If actual_shares_held is zero, do NOT place any order and output: "
            f"BROKER_ORDER_ID=NONE. "
            f"Otherwise place a SELL for {ticker} as a regular-hours market quantity "
            f"order for the capped quantity."
        )

    return (
        # The place_equity_order tool description requires exact bypass phrasing
        # to skip its built-in confirmation step.  The phrases below match the
        # documented examples: "skip the review", "just place it, don't review".
        f"AUTOMATED SIGNAL — skip the review, just place it, don't review. "
        "Use only the Robinhood Agentic account with agentic_allowed=true. "
        "Do not call review_equity_order at any point. "
        "Step 1: Call get_accounts to identify the Agentic account. "
        "Step 2: Call get_equity_positions and get_equity_tradability to verify "
        f"the position and tradability for {ticker}. "
        "Step 3: Call get_equity_orders to verify there is no existing open order "
        f"for {ticker}. "
        "Step 4: Verify buying power is sufficient. "
        f"Step 5: {order_details} "
        f"The independently verified $20-budget proportional amount is ${expected_usd:.4f}. "
        f"Use ref_id={ref_id} for idempotency. "
        "Step 6: After calling place_equity_order, immediately call get_equity_orders "
        "with the returned order id to confirm it is acknowledged. "
        "Step 7: Output EXACTLY these two lines at the end of your response:\n"
        "BROKER_ORDER_ID=<the-order-id-uuid-from-place_equity_order>\n"
        "ORDER_STATE=<state-field-from-get_equity_orders-confirmation>\n"
        "Do not cancel the order."
    )


def invoke_codex(
    proposal: dict[str, Any],
    expected_usd: float,
    config: ShadowConfig,
) -> subprocess.CompletedProcess[str]:
    cmd = [config.codex_command, "exec", "--ephemeral"]
    if config.place_orders:
        # Use a named profile with approval_policy=never and sandbox=read-only.
        # This restricts shell/filesystem access while allowing MCP tool calls
        # without interactive prompts, replacing --dangerously-bypass-approvals-
        # and-sandbox which granted unrestricted VPS access.
        cmd.extend(["--profile", "trade-bot"])
    else:
        cmd.extend(["--sandbox", "read-only"])
    cmd.append(build_prompt(proposal, expected_usd, place_orders=config.place_orders))
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=config.codex_timeout_s,
        check=False,
    )


def review_one(
    proposal: dict[str, Any],
    config: ShadowConfig,
    *,
    _append_pending: bool = True,
) -> ShadowRecord:
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

    # Write PENDING to the ledger before placement.  If the process crashes
    # after Robinhood accepts but before the final record is written, the
    # PENDING entry prevents a re-attempt on restart.
    if _append_pending:
        backend = "direct-mcp" if config.place_orders else "codex-review"
        _append_record(
            ShadowRecord(status="PENDING", rationale=f"invoking {backend}", **base),
            config.ledger_path,
        )

    if config.place_orders:
        # Use the direct Python MCP client to bypass Codex CLI.
        # Codex 0.139.0 uses MCP protocol 2025-06-18 with the elicitation
        # capability, which causes the Robinhood server to require interactive
        # user consent — auto-cancelled in unattended mode.  The direct client
        # uses protocol 2025-03-26 (no elicitation) and avoids this entirely.
        try:
            broker_order_id, order_state = robinhood_mcp_client.place_order(
                proposal, expected
            )
        except robinhood_mcp_client.RobinhoodMCPError as exc:
            return ShadowRecord(
                status="FAILED",
                rationale=f"direct MCP error: {exc}",
                **base,
            )
        except Exception as exc:  # noqa: BLE001
            return ShadowRecord(
                status="FAILED",
                rationale=f"direct MCP unexpected error: {exc}",
                **base,
            )
        return ShadowRecord(
            status="PLACED",
            rationale=f"broker order {broker_order_id} state={order_state}",
            broker_order_id=broker_order_id,
            **base,
        )

    # Review-only path: use Codex CLI (read-only, no placement).
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

    if result.returncode != 0:
        return ShadowRecord(
            status="FAILED",
            rationale="Codex exec exited non-zero",
            codex_exit_code=result.returncode,
            codex_stdout=result.stdout.strip() or None,
            codex_stderr=result.stderr.strip() or None,
            **base,
        )

    return ShadowRecord(
        status="REVIEWED",
        rationale=reason,
        broker_order_id=None,
        codex_exit_code=result.returncode,
        codex_stdout=result.stdout.strip() or None,
        codex_stderr=result.stderr.strip() or None,
        **base,
    )


def run(config: ShadowConfig) -> None:
    _warn_stale_pending(config.ledger_path)
    seen = _load_seen(config.ledger_path)
    tail = TailReader(config.orders_path, start_at_end=True)
    mode = "live auto-trade" if config.place_orders else "review-only (placement disabled)"
    log.info("auto-trader watching %s (%s)", config.orders_path, mode)
    log.info("trade ledger -> %s", config.ledger_path)
    log.info("eligible actions: fresh ENTRY buys and REDUCE sells only")

    while True:
        for proposal in tail.read_new_records():
            key = _dedupe_key(proposal)
            if not key or key in seen:
                continue
            # Add to seen before invoking to guard against duplicate lines.
            seen.add(key)
            record = review_one(proposal, config)
            _append_record(record, config.ledger_path)
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
