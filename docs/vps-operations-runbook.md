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

Check the host-side shadow reviewer:

```bash
systemctl status trade-bot-shadow-review --no-pager
journalctl -u trade-bot-shadow-review -n 100 --no-pager
```

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
target, and EOD management. Heat watches must first observe price below the
trigger, use the trigger +0.2% entry cap, and default to a maximum of three
plan lifecycles per market day.

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
