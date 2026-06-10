# Trade Bot

Reads trading signals from a Discord channel, produces risk-sized proposed
orders, and provides a dashboard for reviewing them.

## Status

The executor is currently **DRY_RUN only**. Connecting Robinhood Trading MCP to
Codex allows an active Codex session to access a dedicated Robinhood Agentic
account, but it does not let the unattended Python executor call Codex tools.
See [docs/robinhood-mcp.md](docs/robinhood-mcp.md) before attempting any live
trade workflow.

For a Docker Compose deployment on a small VPS, see
[docs/vps-deployment.md](docs/vps-deployment.md). The VPS configuration forces
the executor to remain in `DRY_RUN`. For everyday VPS operation, use
[docs/vps-operations-runbook.md](docs/vps-operations-runbook.md).

To review fresh ENTRY and REDUCE proposals through Robinhood without placing
orders, see [docs/robinhood-shadow-review.md](docs/robinhood-shadow-review.md).

- Phase 1: Discord message parser (pure functions, fully tested without Discord)
- Phase 2: Discord listener (selfbot via `discord.py-self`) — wires real messages into the parser
- Phase 3: Broker order placement (IBKR — `ib_async` or Client Portal Web API)
- Phase 4: Risk management, position sizing, kill switch

## Architecture

```
Discord channel ──► Listener (discord.py-self) ──► Parser (pure) ──► JSONL log
                                                                  ╲
                                                                   ╲──► Broker (future)
                                                                   ╲──► Risk filter (future)

                       FastAPI ◄──── reads JSONL
                          │
                          │  REST + SSE
                          ▼
                       React/Vite dashboard ── http://localhost:5173
```

Keeping the parser as a pure function (string ➜ `Signal | None`) means we can:

- Test it with copy-pasted message text, no Discord access required
- Swap selfbot for a real bot/webhook later without changing the parser
- Replay history by re-parsing a JSONL log

## Three message types we recognize

The signal channel posts three kinds of messages. Examples are in `tests/fixtures/`.


| Type      | Header emoji + title | What it means                               |
| --------- | -------------------- | ------------------------------------------- |
| `PLAN`    | `📊 日内短线交易计划`        | A planned setup for later (heads-up)        |
| `TRIGGER` | `🎯 日内短线触发`          | The trigger price was hit — **act now**     |
| `PROFIT`  | `📈 日内短线盈利提醒`        | Position is up X% — heads-up to take profit |


For trading, only `TRIGGER` is actionable. `PLAN` is informational. `PROFIT` is informational/exit-signal.

## ⚠️ Discord ToS warning

Reading messages with a user token (selfbot) violates Discord's ToS. Risk: account ban.
Mitigations used:

- Read-only, no reactions/typing/joining/leaving
- Use a dedicated secondary account
- No automation that looks like rapid user behavior

If a webhook or real bot becomes available, switch by replacing only `bot/listener.py` — the parser is unchanged.

## Setup

Create the venv (one-time):

```bash
python -m venv .venv
```

Activate it (depends on your shell):

```bash
# Git Bash / MINGW64 (Windows)
source .venv/Scripts/activate

# PowerShell (Windows)
.venv\Scripts\Activate.ps1

# CMD (Windows)
.venv\Scripts\activate.bat

# macOS / Linux
source .venv/bin/activate
```

Once activated, your prompt should start with `(.venv)`. Then:

```bash
pip install -e ".[dev]"
cp .env.example .env
# Edit .env and fill in DISCORD_USER_TOKEN and DISCORD_CHANNEL_IDS
```

> **Tip — skip activation entirely.** If activation is finicky in your shell, you
> can always invoke the venv's Python directly (works in any shell):
>
> ```bash
> ./.venv/Scripts/python.exe -m bot.listener      # Windows
> ./.venv/bin/python -m bot.listener              # macOS / Linux
> ```

## Run tests (no Discord needed)

```bash
pytest -v
# or, without activating:
./.venv/Scripts/python.exe -m pytest -v
```

## Run the listener

```bash
python -m bot.listener
# or, without activating:
./.venv/Scripts/python.exe -m bot.listener
```

Stop with **Ctrl+C**. Parsed signals are appended to `logs/signals.jsonl`.

## Run the dashboard

The dashboard reads `logs/*.jsonl` and gives you a live, visual view of all
captured signals plus a real-time stream of new ones via Server-Sent Events.

One command starts the FastAPI backend, the Vite frontend, **and** the
Discord listener — so anything posted in the watched channel shows up in
the dashboard immediately:

```bash
python -m bot.dashboard
```

Then open http://localhost:5173 in your browser.

If the listener crashes (bad token, network blip, Discord rate limit), the
dashboard keeps running so you can still inspect historical data — you'll
see a warning in the launcher's terminal.

If you only want the dashboard (no listener), use:

```bash
python -m bot.dashboard --no-listener
```

Or run each piece in its own terminal (handy for development):

```bash
# Terminal 1 -- backend on :8787
python -m server.api

# Terminal 2 -- frontend on :5173 (proxies /api to :8787)
cd web && npm run dev

# Terminal 3 -- live Discord capture (writes logs/signals.jsonl)
python -m bot.listener
```

### One-time frontend setup

```bash
cd web
npm install
```

## Backfill historical signals

```bash
python -m bot.history --limit 500       # last 500 messages per channel
python -m bot.history --since 2026-04-15
python -m bot.stats logs/history.jsonl  # quick CLI stats
```
