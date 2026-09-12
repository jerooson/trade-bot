# IBKR Paper Trading via the Broker Adapter

The day trader and the swing shadow reviewer talk to a `Broker` interface
(`bot/broker/`). `BROKER=robinhood` (default) keeps the Robinhood Agentic
account; `BROKER=ibkr` routes the same decision logic to Interactive Brokers
through IB Gateway. Point it at the **paper** gateway first: the whole
pipeline (signals -> proposals -> ownership-sized orders -> fills -> P&L
ledger) then runs with zero capital at risk.

## What the adapter does

| Capability | Robinhood | IBKR |
|---|---|---|
| Fractional buy by notional | `dollar_amount` market order | `cashQty` market order (needs fractional permission) |
| Sell by quantity | market | market, refused with `ShareShortfall` if it exceeds the holding (never opens a short) |
| Stop / limit sell | `stop_market` / `limit` (not for fractional) | native `STP` / `LMT` |
| Quotes | MCP `get_equity_quotes` | `reqTickers` (delayed unless subscribed, see `IBKR_MARKET_DATA_TYPE`) |
| Idempotency | `ref_id` | `orderRef`; a repeated ref returns the open order |
| Order id | Robinhood order id | IBKR `permId` (stable across sessions) |

Order states are normalized to the Robinhood vocabulary
(`queued`, `confirmed`, `partially_filled`, `filled`, `cancelled`, `rejected`)
so the day-trade state machine is untouched.

## One-time IBKR setup

1. Open the IBKR account with **IBKR Lite** pricing (US stocks/ETFs $0
   commission). Enable the paper trading account from Account Management.
2. In Account Settings -> Trading Permissions, enable **Fractional Shares**
   (the paper account mirrors the live permission set).
3. In Trader Workstation / IB Gateway: *API -> Settings* -> enable
   "ActiveX and Socket Clients", add `127.0.0.1` to trusted IPs, note the
   socket port (paper gateway `4002`, paper TWS `7497`). Untick "Read-Only API".
4. Market data: paper accounts get delayed data for free. The adapter asks
   for `IBKR_MARKET_DATA_TYPE=3` (delayed) by default; set `1` once a
   real-time subscription is active.

## Run IB Gateway on the VPS

Use the community headless image (IBC inside):

```yaml
# compose.yaml, profile "ibkr"
  ib-gateway:
    image: ghcr.io/gnzsnz/ib-gateway:stable
    profiles: ["ibkr"]
    restart: unless-stopped
    environment:
      TWS_USERID: ${TWS_USERID}
      TWS_PASSWORD: ${TWS_PASSWORD}
      TRADING_MODE: paper
      READ_ONLY_API: "no"
      TWOFA_TIMEOUT_ACTION: restart
    ports:
      - "127.0.0.1:4002:4004"   # paper API port, loopback only
```

```bash
docker compose --profile ibkr up -d ib-gateway
docker compose logs -f ib-gateway     # wait for "Login succeeded"
```

Two-factor authentication: IBKR requires the mobile app prompt on login;
the container restarts and re-prompts when the weekly re-login happens. Plan
for that or use a paper-only user with 2FA disabled.

## Configure the bot

```dotenv
BROKER=ibkr
IBKR_HOST=127.0.0.1
IBKR_PORT=4002
IBKR_CLIENT_ID=17          # day trader 17, swing 18, smoke 19 (role offsets)
IBKR_ACCOUNT=              # optional; first managed account otherwise
IBKR_MARKET_DATA_TYPE=3    # 1 live, 3 delayed
```

Install the client library on the host venv:

```bash
.venv/bin/pip install ib_async
```

## Verify connectivity

```bash
.venv/bin/python -m bot.broker.smoke
.venv/bin/python -m bot.broker.smoke --round-trip SPXL --usd 5
```

The round trip buys $5 of the symbol and sells the fill; it refuses to run
unless `BROKER=ibkr` and the port is a paper port.

## Switch the services

```bash
systemctl restart trade-bot-day-trader trade-bot-shadow-review
journalctl -u trade-bot-day-trader -n 50 --no-pager   # "Connected to IBKR at ..."
```

Everything else (ownership ledger, P&L, performance report) works unchanged
because it reads the same ledgers. Keep `BROKER=robinhood` in the live
`.env` until the paper run has been compared against the Robinhood ledgers
for a few weeks.

## Known limits

- IBKR quotes carry no 30-day average volume. Curated leveraged routes
  (SPXL, TQQQ, SOXL, ...) do not need it; a non-curated leveraged ETF must
  clear `DAY_TRADE_LEVERAGED_ETF_MIN_AVG_VOLUME` on the day's traded volume
  alone, so early in the session it may fall back to the 1x underlying.

- The executor container is unaffected (it only writes proposals).
- IB Gateway must stay logged in; the day trader reconnects with backoff but
  cannot complete 2FA for you.
- `tradability()` on IBKR cannot see fractional eligibility per symbol; an
  ineligible symbol surfaces as an `OrderRejected` at placement, which the
  day trader records as `entry_broker_rejected` without retrying.
