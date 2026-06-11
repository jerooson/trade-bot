# Robinhood Auto-Trade

The host-side auto-trader turns fresh DRY_RUN executor proposals into real
Robinhood Agentic-account orders via Codex CLI.

## Behavior

The worker runs on the VPS host because Codex CLI and Robinhood OAuth
credentials belong to the `deploy` user. It tails:

```text
logs/proposed_orders.jsonl
```

It trades only:

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
tradability, and calls `place_equity_order` (review is skipped). Each proposal
gets a stable `ref_id` UUID for idempotent retries.

Every result is appended to:

```text
logs/robinhood_shadow_reviews.jsonl
```

Set `SHADOW_REVIEW_PLACE_ORDERS=false` to pause live placement without stopping
the watcher.

## Codex MCP Configuration

Add `place_equity_order` to the Robinhood MCP allowlist. Do not add
`review_equity_order` unless you want manual review again.

See `deploy/codex-robinhood-live.toml` for the full VPS config snippet.

```toml
enabled_tools = [
  "get_accounts",
  "get_portfolio",
  "get_equity_positions",
  "get_equity_tradability",
  "place_equity_order",
]
```

`cancel_equity_order` remains excluded by default.

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

Merge `deploy/codex-robinhood-live.toml` into `~/.codex/config.toml`.

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

Expected fresh log lines include:

```text
auto-trader watching logs/proposed_orders.jsonl (live auto-trade)
```

## Trade Logs

```bash
cd ~/trade-bot
tail -n 20 logs/robinhood_shadow_reviews.jsonl
```

The dashboard API also exposes:

```text
GET /api/executor/shadow-reviews
GET /api/executor/shadow-reviews/stream
```
