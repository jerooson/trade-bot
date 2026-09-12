"""
Direct Python MCP client for the Robinhood Trading MCP.

Bypasses Codex CLI to avoid the MCP protocol-2025-06-18 elicitation
mechanism introduced in Codex 0.139.0, which requires interactive user
consent for every tool call and cannot be satisfied in unattended VPS mode.

Uses MCP protocol 2025-03-26 (no elicitation capability), matching the
protocol version that Codex CLI used before v0.139.0.

This module only owns the transport (token refresh + JSON-RPC session).  The
broker-agnostic execution surface lives in ``bot.broker`` and the swing
order-placement logic in ``bot.swing_orders``.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from bot.broker.base import BrokerError, OrderResult, OwnershipBlocked  # noqa: F401 - re-exported

log = logging.getLogger("bot.robinhood_mcp_client")

MCP_URL = "https://agent.robinhood.com/mcp/trading"
CREDS_PATH = Path("~/.codex/.credentials.json").expanduser()


class RobinhoodMCPError(BrokerError):
    pass


_ROBINHOOD_TOKEN_URL = "https://api.robinhood.com/oauth2/token/"
# Refresh when less than 30 minutes remain (gives time to retry on failure).
_TOKEN_REFRESH_THRESHOLD_S = 30 * 60


def _refresh_token(cred: dict, creds: dict, rh_key: str) -> str:
    """Use the OAuth refresh_token to obtain a new access_token silently."""
    refresh_token = cred.get("refresh_token")
    client_id = cred.get("client_id")
    if not refresh_token or not client_id:
        raise RobinhoodMCPError(
            "No refresh_token/client_id available — run 'codex mcp login robinhood-trading'."
        )

    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
    }).encode()
    req = urllib.request.Request(
        _ROBINHOOD_TOKEN_URL,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise RobinhoodMCPError(f"Token refresh HTTP {exc.code}: {exc.read().decode()[:200]}")
    except Exception as exc:
        raise RobinhoodMCPError(f"Token refresh failed: {exc}")

    new_access = data.get("access_token")
    if not new_access:
        raise RobinhoodMCPError(f"Token refresh returned no access_token: {list(data.keys())}")

    # Update credentials in memory and persist.
    expires_in = data.get("expires_in", 0)
    cred["access_token"] = new_access
    cred["expires_at"] = int((time.time() + expires_in) * 1000)
    if data.get("refresh_token"):
        cred["refresh_token"] = data["refresh_token"]
    creds[rh_key] = cred
    try:
        CREDS_PATH.write_text(json.dumps(creds, indent=2))
        log.info("Robinhood token auto-refreshed — new expiry in %dh", expires_in // 3600)
    except Exception as exc:
        log.warning("Token refreshed in memory but could not persist: %s", exc)

    return new_access


def _load_token() -> str:
    """Return a valid Robinhood access token, auto-refreshing if near expiry."""
    try:
        creds = json.loads(CREDS_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        raise RobinhoodMCPError(f"Cannot read credentials from {CREDS_PATH}: {exc}")

    rh_key = next((k for k in creds if "robinhood" in k.lower()), None)
    if not rh_key:
        raise RobinhoodMCPError("No Robinhood credentials found in credentials file")

    cred = creds[rh_key]
    expires_at_s = cred.get("expires_at", 0) / 1000
    if time.time() > expires_at_s - _TOKEN_REFRESH_THRESHOLD_S:
        log.info("Robinhood token expiring soon (or expired) — attempting auto-refresh...")
        return _refresh_token(cred, creds, rh_key)

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
