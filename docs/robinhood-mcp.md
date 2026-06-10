# Robinhood Trading MCP and Codex

## Current status

The trade bot does **not** place Robinhood orders.

`bot.executor` runs as a long-lived Python process. It reads Discord swing
actions, applies sizing and risk rules, updates a virtual book, and writes
proposed decisions to `logs/proposed_orders.jsonl`.

Robinhood Trading MCP is connected to an AI platform such as Codex. Tools from
that connection are available to an active Codex session, not directly to the
Python executor. Setting `EXECUTOR_MODE=LIVE` therefore cannot make this bot
trade through Codex, and the executor rejects that mode.

## Connect Robinhood Trading MCP to Codex

Robinhood's MCP URL is:

```text
https://agent.robinhood.com/mcp/trading
```

In Codex desktop:

1. Open **Settings**.
2. Open **MCP servers**.
3. Select **Streamable HTTP**.
4. Add the URL above and name it `robinhood-trading`.
5. Authenticate in the desktop browser.
6. Complete Robinhood's Agentic account onboarding if prompted.
7. Start a new Codex thread and verify the Robinhood tools are available.

Robinhood only allows agent-placed trades in the dedicated Agentic account.
Authentication and Agentic account onboarding must be completed on desktop.

## Safe supported workflow

Keep this project configured as:

```dotenv
EXECUTOR_MODE=DRY_RUN
EXECUTOR_BUDGET_PER_TICKER=20
EXECUTOR_MAX_OPEN_TICKERS=5
```

Then use this review workflow:

1. Run `python -m bot.dashboard`.
2. Review proposed orders in the Executor view or
   `logs/proposed_orders.jsonl`.
3. In an authenticated Codex thread with Robinhood MCP, ask Codex to inspect
   the Agentic account and review a specific proposed order.
4. Confirm the exact ticker, side, quantity or dollar amount, order type, and
   expected account before allowing an order to be submitted.
5. Verify the broker order status and fill before treating the trade as open.

## What is required for unattended execution

Do not wire the current virtual-book mutation directly to live orders. A live
broker adapter needs, at minimum:

- A supported machine-to-machine broker API or an MCP client architecture that
  Robinhood explicitly supports for unattended use.
- Broker account, buying-power, market-hours, and position reconciliation.
- Idempotency keys and duplicate-signal protection across restarts.
- Order-status polling and handling for rejected, canceled, partial, and stale
  orders.
- A hard kill switch, daily loss limit, maximum order notional, and allowlist.
- Durable audit logs that separate proposed, submitted, acknowledged, and
  filled states.
- Explicit handling for market versus limit orders and price slippage.

Until those controls exist, `DRY_RUN` is the correct mode.

## VPS shadow-review workflow

`bot.shadow_reviewer` is an event-triggered VPS host process. It watches fresh
DRY_RUN proposals and invokes Codex CLI only for:

- accepted `ENTRY` buys for new tickers
- accepted `REDUCE` sells for existing virtual positions

Before invoking Codex, it independently verifies the executor's proportional
sizing against the `$20` per-ticker budget. Codex may call Robinhood read-only
tools and `review_equity_order`, but the order-placement tool remains hidden.
Results are appended to:

```text
logs/robinhood_shadow_reviews.jsonl
```

The shadow reviewer never places or cancels an order.

## Official documentation

- Robinhood Agentic Trading overview:
  https://robinhood.com/us/en/support/articles/agentic-trading-overview/
