# Robinhood Auto-Trade

The host-side auto-trader turns fresh DRY_RUN executor proposals into real
Robinhood Agentic-account orders via Codex CLI.

## Behavior

The worker runs on the VPS host because Codex CLI and Robinhood OAuth
credentials belong to the `deploy` user. It tails:

```text
logs/proposed_orders.jsonl
```

It trades only fresh accepted:

- `ENTRY` and `ADD` + `BUY` proposals
- `REDUCE`, `CLOSE`, and `STOP_TRIGGER` + `SELL` proposals

It skips rejected proposals, stale proposals, shorts, and malformed sizing.

Before Codex is invoked, the worker independently verifies:

- `ENTRY`: `$20 * position_fraction`
- `ADD`: `$20 * target position_fraction - pre-order deployed amount`
- `REDUCE`: `$20 * delta_fraction`, capped at the virtual amount held
- amount is positive and no greater than `$20`
- sell proposals have a positive share quantity

The executor also reads the final shadow-review ledger. `PLACED` and `PENDING`
orders remain provisionally represented in the virtual book. A final
`SKIPPED`, review-only `FAILED`, or review-only `REVIEWED` result causes the
book to rebuild from signal history while excluding that unplaced order. An
`UNVERIFIED` direct-broker outcome remains provisionally represented until
manual broker reconciliation, preventing a duplicate order when placement may
have succeeded but its response was lost. The same filtering runs at startup.

Codex then checks the Agentic account, actual positions, buying power, ticker
tradability, and calls `place_equity_order` (review is skipped). Each proposal
gets a stable `ref_id` UUID for idempotent retries.

### Strategy isolation (one account, two strategies)

The day trader and the swing executor share the Agentic account and can hold
the same symbol (a Heat `SPY` watch routes into `SPXL`; Will can post an
`SPXL` swing). Sells are therefore sized from **what the swing strategy owns**,
never from the account-level quantity:

- `CLOSE` / `STOP_TRIGGER` sell the swing-owned quantity: broker-confirmed
  swing fills from `logs/trade_pnl.jsonl` since the holding's first entry
  (falling back to the virtual estimate when no fill was recorded).
- `REDUCE` sells `min(virtual estimate, swing-owned)`.
- Shares the day trader owns in the same symbol
  (`logs/day_trade_positions.jsonl`) are excluded. If the broker holds fewer
  shares than both ledgers claim, only `actual - day-owned` can be sold and
  the drift is logged.
- When nothing sellable belongs to the swing strategy, no order is placed and
  the ledger records `BLOCKED`. The virtual book treats the position as gone.
- A swing `ENTRY` for a symbol the day trader currently holds is rejected by
  the executor (`EXECUTOR_BLOCK_SHARED_TICKERS=false` to allow), and the day
  trader waits (re-arms) instead of buying a symbol the swing book holds
  (`DAY_TRADE_BLOCK_SHARED_TICKERS=false` to allow).

The bot-managed swing stop monitor only appends a `STOP_TRIGGER` to
`logs/swings.jsonl`; the resulting proposal is placed through the same sized
sell path above and recorded in the P&L ledger. It never places its own order.

Every result is appended to:

```text
logs/robinhood_shadow_reviews.jsonl
```

Ledger statuses: `PLACED`, `SKIPPED` (validation), `BLOCKED` (ownership
refused the sell; nothing placed), `UNVERIFIED` (broker call failed after
validation; reconcile), and review-only `REVIEWED` / `FAILED`.

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
GET /api/performance
```

`/api/performance` (and `python -m bot.performance` on the VPS) is the
reconciled view: per-source and per-month day-trade results, swing sells
valued on the swing-owned quantity only, fills that exceeded swing ownership
re-allocated to the day trade that lost them, open P&L from the last polled
quote, and an explicit list of omissions (sells without fills, unreconciled
day exits, `PENDING`/`UNVERIFIED` reviews). `/api/pnl` and the day-trade
`pnl` block keep their historical meaning and now state their scope.
