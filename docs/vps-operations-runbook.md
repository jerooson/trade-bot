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

The optional host-side `trade-bot-shadow-review` systemd service watches fresh
proposed orders and invokes Codex/Robinhood for non-trading order reviews.

The VPS keeps running when the local PC is off. The dashboard is private and
only becomes accessible locally while an SSH tunnel is open.

The VPS does **not** place real Robinhood orders. The executor is forced to
`DRY_RUN` in `compose.yaml`.

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

Before considering any future broker integration:

- Keep the VPS executor in `DRY_RUN`.
- Confirm every container is healthy.
- Maintain off-VPS backups of `logs/`.
- Add monitoring for listener failures.
- Replace the Discord selfbot with an official bot or webhook when possible.
- Implement broker reconciliation, idempotency, order-status tracking, a kill
  switch, and daily loss limits.
