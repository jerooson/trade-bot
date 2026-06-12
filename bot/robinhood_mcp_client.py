"""
Direct Python MCP client for the Robinhood Trading MCP.

Bypasses Codex CLI to avoid the MCP protocol-2025-06-18 elicitation
mechanism introduced in Codex 0.139.0, which requires interactive user
consent for every tool call and cannot be satisfied in unattended VPS mode.

Uses MCP protocol 2025-03-26 (no elicitation capability), matching the
protocol version that Codex CLI used before v0.139.0.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

log = logging.getLogger("bot.robinhood_mcp_client")

MCP_URL = "https://agent.robinhood.com/mcp/trading"
CREDS_PATH = Path("~/.codex/.credentials.json").expanduser()
REF_ID_NAMESPACE = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")

OPEN_STATES = {
    "new", "queued", "confirmed", "unconfirmed", "partially_filled"
}


class RobinhoodMCPError(Exception):
    pass


def _load_token() -> str:
    """Return a valid Robinhood access token from Codex credentials."""
    try:
        creds = json.loads(CREDS_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        raise RobinhoodMCPError(f"Cannot read credentials from {CREDS_PATH}: {exc}")

    rh_key = next((k for k in creds if "robinhood" in k.lower()), None)
    if not rh_key:
        raise RobinhoodMCPError("No Robinhood credentials found in credentials file")

    cred = creds[rh_key]
    expires_at_s = cred.get("expires_at", 0) / 1000
    if time.time() > expires_at_s - 60:
        raise RobinhoodMCPError(
            f"Robinhood access token expires at {expires_at_s:.0f} "
            "(within 60s or already expired). "
            "Run 'codex mcp login robinhood-trading' to refresh it."
        )
    return cred["access_token"]


def _parse_sse(raw: str) -> Any:
    """Parse SSE-formatted or plain JSON response body."""
    for line in raw.splitlines():
        if line.startswith("data: "):
            return json.loads(line[6:])
    stripped = raw.strip()
    if stripped:
        return json.loads(stripped)
    return None


class _MCPSession:
    """Minimal stateful MCP session using protocol 2025-03-26 (no elicitation)."""

    def __init__(self, access_token: str, timeout: float = 30.0) -> None:
        self._token = access_token
        self._session_id: str | None = None
        self._rpc_id = 0
        self._timeout = timeout
        self._initialize()

    def _next_id(self) -> int:
        self._rpc_id += 1
        return self._rpc_id

    def _post(self, body: dict) -> Any:
        headers: dict[str, str] = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id

        req = urllib.request.Request(
            MCP_URL, data=json.dumps(body).encode(), headers=headers
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                if not self._session_id:
                    sid = resp.headers.get("mcp-session-id")
                    if sid:
                        self._session_id = sid
                return _parse_sse(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body_text = exc.read().decode("utf-8", errors="replace")
            raise RobinhoodMCPError(
                f"HTTP {exc.code} from MCP server: {body_text[:300]}"
            ) from exc

    def _initialize(self) -> None:
        resp = self._post({
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "initialize",
            "params": {
                # Older protocol — no elicitation capability advertised.
                # Codex 0.139.0 sends 2025-06-18 + elicitation, which causes
                # the Robinhood server to require interactive user consent and
                # auto-cancel all tool calls in unattended mode.
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "trade-bot-direct", "version": "1.0"},
            },
        })
        if resp and "error" in resp:
            raise RobinhoodMCPError(f"MCP initialize error: {resp['error']}")
        # Acknowledge per MCP protocol.
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def call(self, tool: str, **kwargs: Any) -> Any:
        resp = self._post({
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "tools/call",
            "params": {"name": tool, "arguments": kwargs},
        })
        if resp is None:
            raise RobinhoodMCPError(f"Empty response for tool '{tool}'")
        if "error" in resp:
            raise RobinhoodMCPError(f"'{tool}' RPC error: {resp['error']}")

        result = resp.get("result", {})
        if result.get("isError"):
            content = result.get("content", [])
            msg = content[0].get("text", str(content)) if content else str(result)
            raise RobinhoodMCPError(f"'{tool}' returned isError: {msg}")

        content = result.get("content", [])
        if not content:
            raise RobinhoodMCPError(f"'{tool}' returned no content")

        text = content[0].get("text", "")
        if text == "user cancelled MCP tool call":
            raise RobinhoodMCPError(
                f"'{tool}' was cancelled by the Robinhood server. "
                "This indicates the MCP protocol elicitation/consent mechanism "
                "was triggered. Check Robinhood app agent settings."
            )
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text


def place_order(
    proposal: dict[str, Any],
    expected_usd: float,
) -> tuple[str, str]:
    """Place a Robinhood order directly via MCP without Codex.

    Returns (broker_order_id, order_state).
    Raises RobinhoodMCPError on any failure.
    """
    ticker = proposal["ticker"]
    kind = proposal["signal_kind"]
    amount = float(proposal["usd_amount"])

    signal = proposal.get("signal") or {}
    msg_id = signal.get("message_id")
    dedupe_key = f"{msg_id}:{ticker}:{kind}" if msg_id else str(proposal.get("id") or "")
    ref_id = str(uuid.uuid5(REF_ID_NAMESPACE, dedupe_key))

    log.info(
        "Direct MCP: %s %s $%.4f ref_id=%s", kind, ticker, amount, ref_id
    )

    token = _load_token()
    session = _MCPSession(token)

    # Step 1: Find the Agentic account.
    accounts_data = session.call("get_accounts")
    accounts = accounts_data.get("data", {}).get("accounts", [])
    agentic = [a for a in accounts if a.get("agentic_allowed")]
    if not agentic:
        raise RobinhoodMCPError(
            "No Agentic account found (agentic_allowed=true). "
            "Complete Robinhood Agentic onboarding in the Robinhood app."
        )
    account_number = agentic[0]["account_number"]
    log.info("Agentic account: %s", account_number)

    # Step 2: Check tradability.
    tradability_data = session.call(
        "get_equity_tradability",
        account_number=account_number,
        symbols=[ticker],
    )
    results = tradability_data.get("data", {}).get("results", [])
    if results:
        item = results[0]
        if not item.get("is_tradable", True):
            reason = item.get("reason", "unknown")
            raise RobinhoodMCPError(f"{ticker} is not tradable: {reason}")

    # Step 3: Check for existing open orders.
    existing_data = session.call(
        "get_equity_orders",
        account_number=account_number,
        symbol=ticker,
    )
    existing_orders = existing_data.get("data", {}).get("orders", [])
    open_orders = [o for o in existing_orders if o.get("state") in OPEN_STATES]
    if open_orders:
        oid = open_orders[0].get("id", "?")
        raise RobinhoodMCPError(
            f"Existing open order for {ticker}: {oid} — skipping to avoid duplicate"
        )

    # Step 4: For REDUCE, determine actual position to cap the sell quantity.
    quantity: float | None = None
    if kind == "REDUCE":
        positions_data = session.call(
            "get_equity_positions", account_number=account_number
        )
        positions = positions_data.get("data", {}).get("equity_positions", [])
        position = next((p for p in positions if p.get("symbol") == ticker), None)
        actual_shares = float(position.get("quantity", 0)) if position else 0.0
        if actual_shares <= 0:
            raise RobinhoodMCPError(
                f"REDUCE for {ticker} but actual position is 0 shares"
            )
        virtual_est = float(proposal.get("shares_estimate", 0))
        quantity = min(virtual_est, actual_shares)
        log.info(
            "REDUCE %s: virtual_est=%.6f actual=%.6f → sell=%.6f",
            ticker,
            virtual_est,
            actual_shares,
            quantity,
        )

    # Step 5: Place the order.
    order_kwargs: dict[str, Any] = {
        "account_number": account_number,
        "symbol": ticker,
        "side": "buy" if kind == "ENTRY" else "sell",
        "type": "market",
        "time_in_force": "gfd",
        "market_hours": "regular_hours",
        "ref_id": ref_id,
    }
    if kind == "ENTRY":
        order_kwargs["dollar_amount"] = f"{amount:.2f}"
    else:
        order_kwargs["quantity"] = f"{quantity:.6f}"

    log.info("Placing order: %s", {k: v for k, v in order_kwargs.items() if k != "account_number"})
    order_data = session.call("place_equity_order", **order_kwargs)

    order = order_data.get("data", {}).get("order", {})
    broker_order_id = order.get("id")
    order_state = order.get("state", "unknown")

    if not broker_order_id:
        raise RobinhoodMCPError(
            f"place_equity_order response missing order id: {order_data}"
        )
    log.info("Order placed: id=%s state=%s", broker_order_id, order_state)

    # Step 6: Confirm via get_equity_orders.
    confirm_data = session.call(
        "get_equity_orders",
        account_number=account_number,
        order_id=broker_order_id,
    )
    confirm_orders = confirm_data.get("data", {}).get("orders", [])
    if confirm_orders:
        order_state = confirm_orders[0].get("state", order_state)
    log.info("Order confirmed: id=%s final_state=%s", broker_order_id, order_state)

    return broker_order_id, order_state
