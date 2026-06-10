# Robinhood Shadow Review

The shadow reviewer proves the complete Discord-to-Robinhood review path without
placing real orders.

## Behavior

The worker runs on the VPS host because Codex CLI and Robinhood OAuth
credentials belong to the `deploy` user. It tails:

```text
logs/proposed_orders.jsonl
```

It reviews only:

- fresh accepted `ENTRY` + `BUY` proposals
- fresh accepted `REDUCE` + `SELL` proposals

It skips `ADD`, `CLOSE`, `STOP_TRIGGER`, rejected proposals, stale proposals,
shorts, and malformed sizing.

Before Codex is invoked, the worker independently verifies:

- `ENTRY`: `$20 * position_fraction`
- `REDUCE`: `$20 * delta_fraction`, capped at the virtual amount held
- amount is positive and no greater than `$20`
- `REDUCE` has a positive share quantity

Codex then checks the Agentic account, actual positions, buying power, ticker
tradability, and calls `review_equity_order`. The prompt explicitly forbids
placing or cancelling orders.

Every result is appended to:

```text
logs/robinhood_shadow_reviews.jsonl
```

## Codex MCP Configuration

Keep order-placement and cancellation tools hidden. Add only the non-trading
review tool to the existing allowlist:

```toml
[mcp_servers.robinhood-trading]
url = "https://agent.robinhood.com/mcp/trading"
enabled = true
required = true
enabled_tools = [
  "get_accounts",
  "get_portfolio",
  "get_equity_positions",
  "get_equity_tradability",
  "review_equity_order",
]

[mcp_servers.robinhood-trading.tools.get_accounts]
approval_mode = "approve"

[mcp_servers.robinhood-trading.tools.get_portfolio]
approval_mode = "approve"

[mcp_servers.robinhood-trading.tools.get_equity_positions]
approval_mode = "approve"

[mcp_servers.robinhood-trading.tools.get_equity_tradability]
approval_mode = "approve"

[mcp_servers.robinhood-trading.tools.review_equity_order]
approval_mode = "approve"
```

Do not add `place_equity_order` or `cancel_equity_order`.

## Install on the VPS

As `root`, install Python virtual-environment support:

```bash
apt update
apt install -y python3-venv
```

Then, as `deploy`, after pulling the deployment:

```bash
cd ~/trade-bot
python3 -m venv .venv
.venv/bin/pip install .
```

As `root`, install and start the systemd service:

```bash
cp /home/deploy/trade-bot/deploy/trade-bot-shadow-review.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now trade-bot-shadow-review
```

Verify:

```bash
systemctl status trade-bot-shadow-review --no-pager
journalctl -u trade-bot-shadow-review -n 100 --no-pager
```

## Review Logs

```bash
cd ~/trade-bot
tail -n 20 logs/robinhood_shadow_reviews.jsonl
```

The dashboard API also exposes:

```text
GET /api/executor/shadow-reviews
GET /api/executor/shadow-reviews/stream
```
