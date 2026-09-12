# VPS Operations Runbook

This runbook covers the manual processes for operating the trade bot on the
DigitalOcean VPS.

## Current Architecture

The VPS runs four Docker containers:

| Container | Purpose |
| --- | --- |
| `listener` | Connects to Discord and records parsed messages |
| `executor` | Produces risk-sized proposed orders in `DRY_RUN` |
| `api` | Reads logs and serves dashboard data |
| `web` | Serves the private dashboard |

The host-side `trade-bot-shadow-review` systemd service watches fresh proposed
orders and invokes Codex/Robinhood to place real Agentic-account orders.

The VPS keeps running when the local PC is off. The dashboard is private and
only becomes accessible locally while an SSH tunnel is open.

The Docker executor remains in `DRY_RUN` for virtual-book sizing. Real orders
are placed by the host-side auto-trader via Codex `place_equity_order`.

## Important Locations

| Item | Location |
| --- | --- |
| VPS project | `/home/deploy/trade-bot` |
| VPS secrets | `/home/deploy/trade-bot/.env` |
| VPS runtime data | `/home/deploy/trade-bot/logs` |
| GitHub repository | `https://github.com/jerooson/trade-bot` |
| Private dashboard | `http://127.0.0.1:8080` through an SSH tunnel |

Never commit `.env` or `logs/` to GitHub.

## Connect to the VPS

Run this from local Windows PowerShell:

```powershell
ssh deploy@67.205.185.110
```

After connecting, the prompt should begin with:

```text
deploy@ubuntu-s-1vcpu-1gb-nyc1
```

Disconnect from the VPS:

```bash
exit
```

## Open the Private Dashboard

Run this from a separate local Windows PowerShell window:

```powershell
ssh -L 8080:127.0.0.1:8080 deploy@67.205.185.110
```

Keep that SSH session open, then visit:

```text
http://127.0.0.1:8080
```

Closing the SSH tunnel only closes local dashboard access. It does not stop the
bot on the VPS.

## Check Bot Health

Connect to the VPS, then run:

```bash
cd ~/trade-bot
docker compose ps
```

Expected state:

- `api`: `Up ... (healthy)`
- `listener`: `Up`
- `executor`: `Up`
- `web`: `Up`

Inspect the most useful logs:

```bash
docker compose logs --tail=100 listener executor
```

Watch logs continuously:

```bash
docker compose logs -f listener executor
```

Press `Ctrl+C` to stop watching logs. This does not stop the containers.

## Deploy Code Updates from GitHub

First, make and test changes locally. Commit and push them to GitHub.

Then connect to the VPS and run:

```bash
cd ~/trade-bot
git pull --ff-only
docker compose up -d --build
docker compose ps
docker compose logs --tail=100 listener executor
```

`docker compose up -d --build` rebuilds changed images and replaces the
affected containers. It does not replace `.env` or delete `logs/`.

For documentation-only changes, pulling is enough:

```bash
cd ~/trade-bot
git pull --ff-only
```

## Restart Services

Restart only the Discord listener:

```bash
cd ~/trade-bot
docker compose restart listener
docker compose logs --tail=100 listener
```

Restart all services:

```bash
cd ~/trade-bot
docker compose restart
docker compose ps
```

Stop all services:

```bash
cd ~/trade-bot
docker compose down
```

Start all services again:

```bash
cd ~/trade-bot
docker compose up -d
docker compose ps
```

## Edit VPS Configuration

Connect to the VPS, then run:

```bash
cd ~/trade-bot
nano .env
```

Save in Nano:

1. Press `Ctrl+O`.
2. Press `Enter`.
3. Press `Ctrl+X`.

Restart the affected services after editing `.env`:

```bash
docker compose up -d
docker compose logs --tail=100 listener executor
```

Keep these safety settings:

```dotenv
EXECUTOR_BUDGET_PER_TICKER=20
EXECUTOR_MAX_OPEN_TICKERS=5
EXECUTOR_MODE=DRY_RUN
```

`compose.yaml` also forces `EXECUTOR_MODE=DRY_RUN`.

## Rotate an Expired Discord Token

If the listener repeatedly restarts or reports login/authentication errors:

1. Obtain a new Discord token.
2. Connect to the VPS.
3. Update `DISCORD_USER_TOKEN` in `.env`.
4. Restart the listener.

Commands:

```bash
cd ~/trade-bot
nano .env
docker compose restart listener
docker compose logs --tail=100 listener
```

Never paste the Discord token into chat, GitHub, screenshots, or shell history.

## Reboot the VPS

The containers use `restart: unless-stopped`, so they should start
automatically after a VPS reboot.

Run as a sudo-capable user:

```bash
sudo reboot
```

Wait about one minute, reconnect, and verify:

```bash
cd ~/trade-bot
docker compose ps
docker compose logs --tail=50 listener executor
```

## Inspect Runtime Data

List log files and sizes:

```bash
cd ~/trade-bot
ls -lh logs
```

View recent proposed orders:

```bash
tail -n 20 logs/proposed_orders.jsonl
```

View the virtual executor book:

```bash
cat logs/virtual_book.json
```

View recent Codex/Robinhood shadow reviews:

```bash
tail -n 20 logs/robinhood_shadow_reviews.jsonl
```

Swing `ADD` signals treat the right side of the size arrow as the target total
allocation. The live buy amount is only the incremental difference from the
pre-signal virtual allocation. The executor consumes final shadow-review
outcomes and rebuilds `virtual_book.json` after an order is definitively
skipped or a review-only operation fails, including during startup replay.
Ambiguous direct-broker outcomes remain provisional until reconciled.

Check the host-side shadow reviewer:

```bash
systemctl status trade-bot-shadow-review --no-pager
journalctl -u trade-bot-shadow-review -n 100 --no-pager
```

### Broker adapter (Robinhood or IBKR paper)

Both host services place orders through `bot/broker/`. `BROKER=robinhood` is
the default; `BROKER=ibkr` with `IBKR_PORT=4002` routes the same logic to the
IBKR paper gateway (`docker compose --profile ibkr up -d ib-gateway`). Check
connectivity before switching services:

```bash
cd ~/trade-bot
.venv/bin/python -m bot.broker.smoke
```

Full setup, client-id rules and limits: `docs/ibkr-paper.md`.

### Performance report (reconciled)

```bash
cd ~/trade-bot
.venv/bin/python -m bot.performance
.venv/bin/python -m bot.performance --json > /tmp/performance.json
```

The report splits day trades by source (`discord` / `heat` / `manual`), month
and exit reason, values every swing sell on the swing-owned quantity only,
re-allocates any fill that exceeded swing ownership to the day trade that lost
those shares, shows open P&L from the last polled quote, and ends with an
**omissions** list. Read the omissions before the totals: a sell without a
fill, an `unreconciled` day exit, or a `PENDING`/`UNVERIFIED` review means the
totals are incomplete. The same data is served at `GET /api/performance`.

### Strategy isolation and unreconciled day trades

The swing executor and the day trader share one Robinhood account. Each
strategy now sells only the shares it owns (`bot/position_ownership.py`), a
swing `ENTRY` is rejected while the day trader holds the symbol, and a day
trade waits while the swing book holds its execution symbol. Both guards are
on by default:

```dotenv
EXECUTOR_BLOCK_SHARED_TICKERS=true
DAY_TRADE_BLOCK_SHARED_TICKERS=true
```

When the broker refuses a day-trade exit with "Not enough shares to sell", the
day trader reads the account quantity and the swing book, sells only the part
that cannot belong to the swing strategy, and records the rest as
`unreconciled_qty` with a `reconciliation_note`. A lifecycle with nothing
sellable ends in status `unreconciled`: it is no longer polled, no P&L is
invented, and it appears under `pnl.omissions.unreconciled` in
`GET /api/daytrader`. Repair the accounting from the Robinhood order history
(the `/api/performance` re-allocation shows the evidence-based split) before
treating the totals as complete.

A watch whose trigger is more than `DAY_TRADE_TRIGGER_MAX_RATIO` (default 2)
times away from the live quote is quarantined (`exit_reason =
implausible_trigger`, `quarantine_reason` set) instead of armed. Heat ideas
are re-parsed with the current parser on every read, so an old capture such as
`站上 fib 1.414` no longer materializes as a `$1.414` trigger; approve the idea
from the dashboard with the real level to lift the quarantine.

### Heat day-trade ideas

The listener can watch Heat's channel using stable Discord ID allowlists:

```dotenv
DISCORD_HEAT_CHANNEL_IDS=1121667438254227506
DISCORD_HEAT_AUTHOR_IDS=<stable Discord user id>
```

Both settings are required together. Only new live messages from that author
ID are captured; listener restarts do not replay history. Explicit numeric
long-equity entries are auto-approved. Chart-only or non-numeric ideas wait in
**Day Trade → Heat Ideas** for approval. Options, shorts, and trade-management
messages are excluded.

The Dashboard Heat switch controls new entries. Turning it off expires or
cancels only unfilled Heat entries; filled positions retain normal stop,
target, and EOD management. Once approved, a Heat setup is a persistent watch:
it appears in both **Manual Watches** and **Active Trades**, survives trading-day
boundaries, and remains until it fills or the operator selects **Cancel Watch**.
Heat watches must first observe price below the trigger, use the trigger +0.2%
entry cap, and default to a maximum of three new plan lifecycles per market day.

Leveraged ETF routing uses a curated high-liquidity allowlist for established
products such as SPXL, TQQQ, SOXL, TSLL, NVDL, MSTU, and PLTU. Robinhood quote
responses may omit volume, so allowlisted routes do not require that field;
they still must pass live spread, minimum-price, tradability, and fractional-
trading checks. Less-established leveraged products continue to require at
least the configured observed-volume threshold. Each trigger writes the chosen
route, liquidity basis, spread, volume availability, and rejected candidates
to the day-trader log.

Discord day-trade plans received during a regular session remain eligible
through the following trading session when they do not fill. A carried plan
starts the next session unarmed: if price gaps above the trigger and the +0.2%
entry cap, the bot waits for price to return below the trigger and break out
again instead of chasing. It expires at the following session's close.

`DAY_TRADE_ENTRY_CONFIRM_S` (default 0) makes the day trader wait until the
signal price has stayed beyond the trigger for that many seconds before it
submits the entry; a dip back through the trigger restarts the wait. Set it
to `60` to run the one-minute confirmation studied in
`docs/replay-findings-2026-09.md` (paper account first).

The day-trade stop policy starts at -2%. Its first risk-reduction milestone
uses one 5-second observation (`+1% -> -0.5% stop`); milestones from +2% onward
still require two consecutive 5-second observations.

Runtime files:

- `logs/heat_ideas.jsonl` — append-only ideas and chart updates
- `logs/heat_attachments/` — locally saved images
- `state/heat_idea_decisions.jsonl` — Dashboard approvals/rejections
- `state/heat_settings.json` — Heat entry kill switch

## Back Up Runtime Data

Create a compressed backup on the VPS:

```bash
cd ~/trade-bot
tar -czf "$HOME/trade-bot-logs-$(date +%Y-%m-%d).tar.gz" logs
ls -lh "$HOME"/trade-bot-logs-*.tar.gz
```

Download a backup from local Windows PowerShell:

```powershell
scp deploy@67.205.185.110:/home/deploy/trade-bot-logs-YYYY-MM-DD.tar.gz .
```

Replace `YYYY-MM-DD` with the backup date.

## Troubleshooting

### A container is restarting

```bash
cd ~/trade-bot
docker compose ps
docker compose logs --tail=200 SERVICE_NAME
```

Replace `SERVICE_NAME` with `listener`, `executor`, `api`, or `web`.

### Dashboard tunnel reports port already in use

Use a different local port:

```powershell
ssh -L 8081:127.0.0.1:8080 deploy@67.205.185.110
```

Then visit:

```text
http://127.0.0.1:8081
```

### Deployment fails after `git pull`

Inspect the status without deleting anything:

```bash
cd ~/trade-bot
git status
docker compose build
```

Do not run `git reset --hard` or delete `logs/`.

### VPS is low on disk space

```bash
df -h
docker system df
```

Review the output before removing Docker data. Do not delete active volumes or
the project `logs/` directory.

## Safety Checklist

- Keep the Docker executor in `DRY_RUN` (virtual book only).
- Confirm `place_equity_order` is in the VPS Codex allowlist only when auto-trade
  should be active; set `SHADOW_REVIEW_PLACE_ORDERS=false` to pause placement.
- Confirm every container and `trade-bot-shadow-review` are healthy.
- Maintain off-VPS backups of `logs/`.
- Add monitoring for listener and auto-trader failures.
- Replace the Discord selfbot with an official bot or webhook when possible.

## Discord main-channel plans: record only

`DAY_TRADE_DISCORD_PLANS=record` keeps the listener writing main-channel PLAN
signals to `logs/signals.jsonl` (so replay datasets keep growing) but the day
trader never turns them into watches. Heat ideas and manual watches are not
affected. Set it back to `execute` (the default) and restart the day trader to
resume trading them. Enabled on the VPS on 2026-09-12 together with
`DAY_TRADE_BUDGET_USD=50`, so the day P&L from that date on is Heat-only.

## Heat option shadow tracker (paper)

`trade-bot-option-shadow.service` runs `python -m bot.option_shadow` on the
host. It watches every approved Heat idea, and when the underlying crosses the
trigger it records a paper purchase of the nearest-expiry at-the-money option
(call for long, put for short) at the ask, then applies Heat's mechanical
rules: sell half at +50 %, cut to a runner at +100 % or at the target, sell all
at -30 %, after five minutes past the trigger against the position, or at
15:50 ET. It never places orders. Ledger: `logs/option_shadow.jsonl`; open
state: `state/option_shadow.json`. Summary:

```
.venv/bin/python -m bot.option_shadow --report
```

## Daily review

`trade-bot-daily-review.timer` runs `python -m bot.daily_review --narrative --email`
at 16:15 ET on weekdays. It writes `logs/reviews/<date>.md` and `.json`
(Heat feed, chart analyzer output, day trades with slippage and exits, a
"Heat said / bot did" reconciliation, recorded-only Discord plans, option
shadow results, swing activity, running totals, health). The dashboard shows
them under Review (`/api/review`, `/api/review/<date>` or `latest`).

- `--narrative` asks the Codex CLI (ChatGPT login on the host) for a short
  Chinese summary; on a quota error the review is still written and sent,
  with a one-line note instead of the summary.
- `--email` sends the markdown by SMTP when `REVIEW_EMAIL_TO`,
  `REVIEW_SMTP_USER` and `REVIEW_SMTP_PASSWORD` (a Gmail App Password) are
  set in `.env`; otherwise it only writes the files.
- `REVIEW_DISCORD_WEBHOOK` posts the review to a Discord webhook over HTTPS
  (head inline, full markdown attached). DigitalOcean blocks outbound SMTP
  ports 465/587 from this droplet, so this is the working delivery path.
- Regenerate a day by hand: `.venv/bin/python -m bot.daily_review --date 2026-09-14 --print`.
