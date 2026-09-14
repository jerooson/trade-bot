"""
Option shadow tracker: what would Heat's option rules have done, on paper?

Every approved Heat idea (typed level or chart-analyzed) is watched.  When the
underlying crosses the trigger during regular hours the tracker "buys" the
nearest-expiry option closest to the money (call for long ideas, put for
short) at the ask and then manages the paper position with the mechanical
part of Heat's rules (docs: data/heat_options_playbook.md, private):

    +50 %                 sell half
    +100 %                reduce to the runner (RUNNER_FRACTION of the lot)
    target price reached  reduce to the runner
    option <= -30 %       sell all   (his "a good plan never loses more than 30 %")
    underlying held past  sell all   (5 minutes beyond the trigger against us,
      the trigger 5 min                 a proxy for "5m bar closes past the line")
    15:50 ET              sell all   (never hold short-dated options overnight)

No orders are ever placed.  Fills are paper: buys at the ask, sells at the
bid, both from ``get_option_quotes``.  Everything is appended to
``logs/option_shadow.jsonl``; open state lives in ``state/option_shadow.json``.

    python -m bot.option_shadow            # run the loop (systemd service)
    python -m bot.option_shadow --report   # summarise the ledger
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import statistics as st
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

from bot.heat_ideas import is_plausible_trigger, load_materialized_heat_ideas  # noqa: E402
from bot.leveraged_etfs import result_by_symbol

log = logging.getLogger("bot.option_shadow")
ET = ZoneInfo("America/New_York")

LEDGER_PATH = Path(os.getenv("OPTION_SHADOW_LEDGER", "logs/option_shadow.jsonl"))
STATE_PATH = Path(os.getenv("OPTION_SHADOW_STATE", "state/option_shadow.json"))
POLL_S = float(os.getenv("OPTION_SHADOW_POLL_S", "15"))
CONTRACTS = int(os.getenv("OPTION_SHADOW_CONTRACTS", "10"))
RUNNER_FRACTION = float(os.getenv("OPTION_SHADOW_RUNNER_FRACTION", "0.2"))
MAX_IDEA_AGE_SESSIONS = int(os.getenv("OPTION_SHADOW_MAX_IDEA_AGE_SESSIONS", "10"))
TRIM_HALF_PCT = 50.0
RUNNER_PCT = 100.0
STOP_PCT = -30.0
ADVERSE_HOLD_S = 300.0
FLATTEN_TIME = dtime(15, 50)
OPEN_TIME = dtime(9, 30)
CLOSE_TIME = dtime(16, 0)
MULTIPLIER = 100.0


class Session(Protocol):
    def call(self, tool: str, **kwargs: Any) -> Any: ...


@dataclass
class Contract:
    instrument_id: str
    symbol: str
    expiration: str
    strike: float
    kind: str            # call | put


@dataclass
class Shadow:
    idea_id: str
    ticker: str
    direction: str
    trigger: float
    operator: str        # above | below
    target: float | None
    status: str = "watching"      # watching | open | closed | expired
    contract: dict[str, Any] | None = None
    entry_ts: str | None = None
    entry_price: float | None = None     # per-share option premium (ask)
    underlying_at_entry: float | None = None
    qty: int = 0
    qty_open: int = 0
    trimmed_half: bool = False
    trimmed_runner: bool = False
    adverse_since: str | None = None
    realized_usd: float = 0.0
    max_gain_pct: float = 0.0
    fills: list[dict[str, Any]] = field(default_factory=list)
    exit_reason: str | None = None
    closed_ts: str | None = None


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def load_state(path: Path | None = None) -> dict[str, Shadow]:
    path = path or STATE_PATH
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {k: Shadow(**v) for k, v in raw.items()}


def save_state(shadows: dict[str, Shadow], path: Path | None = None) -> None:
    path = path or STATE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({k: asdict(v) for k, v in shadows.items()}, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def append_ledger(record: dict[str, Any], path: Path | None = None) -> None:
    path = path or LEDGER_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Robinhood option helpers
# ---------------------------------------------------------------------------

def _f(v: Any) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x > 0 else None


def nearest_expiration(session: Session, symbol: str, today: date) -> str | None:
    data = session.call("get_option_chains", underlying_symbol=symbol)
    chains = (data.get("data") or data).get("chains") or []
    dates: list[str] = []
    for ch in chains:
        if str(ch.get("symbol") or "").upper() != symbol.upper():
            continue
        dates += [d for d in ch.get("expiration_dates") or [] if d >= today.isoformat()]
    return min(dates) if dates else None


def nearest_contract(session: Session, symbol: str, expiration: str, kind: str, price: float) -> Contract | None:
    """Closest-to-the-money contract of ``kind`` for one expiration (paginated)."""
    best: Contract | None = None
    cursor: str | None = None
    for _ in range(20):
        kwargs: dict[str, Any] = {"chain_symbol": symbol, "expiration_dates": expiration, "type": kind}
        if cursor:
            kwargs["cursor"] = cursor
        data = session.call("get_option_instruments", **kwargs)
        body = data.get("data") or data
        for ins in body.get("instruments") or []:
            k = _f(ins.get("strike_price"))
            if k is None or ins.get("tradability") == "untradable":
                continue
            if best is None or abs(k - price) < abs(best.strike - price):
                best = Contract(str(ins["id"]), symbol, expiration, k, kind)
        nxt = body.get("next")
        if not nxt:
            break
        cursor = nxt.split("cursor=")[-1].split("&")[0] if "cursor=" in str(nxt) else str(nxt)
    return best


def option_quote(session: Session, instrument_id: str) -> tuple[float | None, float | None]:
    """(bid, ask) for one contract."""
    data = session.call("get_option_quotes", instrument_ids=[instrument_id])
    for item in (data.get("data") or data).get("results") or []:
        q = item.get("quote") or item
        if str(q.get("instrument_id")) == instrument_id:
            return _f(q.get("bid_price")), _f(q.get("ask_price"))
    return None, None


def underlying_prices(session: Session, symbols: list[str]) -> dict[str, float]:
    if not symbols:
        return {}
    data = session.call("get_equity_quotes", symbols=symbols)
    out: dict[str, float] = {}
    rows = (data.get("data") or data).get("results") or []
    for sym, item in result_by_symbol(rows, symbols).items():
        q = item.get("quote") or item
        px = _f(q.get("last_trade_price")) or _f(q.get("mark_price"))
        if px:
            out[sym.upper()] = px
    return out


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------

def crossed(price: float, trigger: float, operator: str) -> bool:
    return price >= trigger if operator == "above" else price <= trigger


def adverse(price: float, trigger: float, operator: str) -> bool:
    return price < trigger if operator == "above" else price > trigger


def target_hit(price: float, target: float | None, direction: str) -> bool:
    if target is None:
        return False
    return price >= target if direction == "long" else price <= target


def _sell(s: Shadow, qty: int, bid: float, reason: str, now: datetime) -> None:
    qty = max(0, min(qty, s.qty_open))
    if qty == 0:
        return
    pnl = (bid - float(s.entry_price)) * MULTIPLIER * qty
    s.qty_open -= qty
    s.realized_usd += pnl
    s.fills.append({"ts": now.isoformat(), "side": "sell", "qty": qty, "price": bid, "reason": reason, "pnl_usd": round(pnl, 2)})
    append_ledger({"event": "sell", "idea_id": s.idea_id, "ticker": s.ticker, "contract": s.contract, "qty": qty,
                   "price": bid, "reason": reason, "pnl_usd": round(pnl, 2), "qty_open": s.qty_open, "ts": now.isoformat()})
    if s.qty_open == 0:
        s.status = "closed"
        s.exit_reason = reason
        s.closed_ts = now.isoformat()
        append_ledger({"event": "closed", "idea_id": s.idea_id, "ticker": s.ticker, "contract": s.contract,
                       "entry_price": s.entry_price, "realized_usd": round(s.realized_usd, 2),
                       "realized_pct": round(s.realized_usd / (float(s.entry_price) * MULTIPLIER * s.qty) * 100, 2),
                       "max_gain_pct": round(s.max_gain_pct, 2), "exit_reason": reason, "ts": now.isoformat()})


def manage_open(s: Shadow, underlying: float, bid: float | None, now: datetime) -> None:
    """Apply the mechanical rules to one open shadow."""
    if bid is None:
        return
    gain_pct = (bid / float(s.entry_price) - 1) * 100
    s.max_gain_pct = max(s.max_gain_pct, gain_pct)
    runner = max(1, round(s.qty * RUNNER_FRACTION))

    if now.time() >= FLATTEN_TIME:
        _sell(s, s.qty_open, bid, "eod", now)
        return
    if gain_pct <= STOP_PCT:
        _sell(s, s.qty_open, bid, "stop_30pct", now)
        return
    if adverse(underlying, s.trigger, s.operator):
        if s.adverse_since is None:
            s.adverse_since = now.isoformat()
        elif (now - datetime.fromisoformat(s.adverse_since)).total_seconds() >= ADVERSE_HOLD_S:
            _sell(s, s.qty_open, bid, "stop_level", now)
            return
    else:
        s.adverse_since = None
    if not s.trimmed_runner and (gain_pct >= RUNNER_PCT or target_hit(underlying, s.target, s.direction)):
        reason = "trim_runner_100pct" if gain_pct >= RUNNER_PCT else "trim_runner_target"
        _sell(s, s.qty_open - runner, bid, reason, now)
        s.trimmed_runner = True
        s.trimmed_half = True
        return
    if not s.trimmed_half and gain_pct >= TRIM_HALF_PCT:
        _sell(s, s.qty_open - max(runner, s.qty_open // 2), bid, "trim_half_50pct", now)
        s.trimmed_half = True


def open_shadow(session: Session, s: Shadow, underlying: float, now: datetime) -> bool:
    kind = "call" if s.direction == "long" else "put"
    expiration = nearest_expiration(session, s.ticker, now.date())
    if not expiration:
        return False
    contract = nearest_contract(session, s.ticker, expiration, kind, underlying)
    if contract is None:
        return False
    bid, ask = option_quote(session, contract.instrument_id)
    if ask is None:
        return False
    s.contract = asdict(contract)
    s.entry_ts = now.isoformat()
    s.entry_price = ask
    s.underlying_at_entry = underlying
    s.qty = s.qty_open = CONTRACTS
    s.status = "open"
    s.fills.append({"ts": now.isoformat(), "side": "buy", "qty": CONTRACTS, "price": ask, "bid": bid})
    append_ledger({"event": "open", "idea_id": s.idea_id, "ticker": s.ticker, "direction": s.direction,
                   "trigger": s.trigger, "target": s.target, "contract": s.contract, "qty": CONTRACTS,
                   "price": ask, "bid": bid, "underlying": underlying, "ts": now.isoformat()})
    return True


# ---------------------------------------------------------------------------
# Loop
# ---------------------------------------------------------------------------

def in_regular_hours(now: datetime) -> bool:
    return now.weekday() < 5 and OPEN_TIME <= now.time() < CLOSE_TIME


def _sessions_since(created_at: str, today: date) -> int:
    d = datetime.fromisoformat(created_at).astimezone(ET).date()
    n = 0
    while d < today:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


def traded_ids(path: Path | None = None) -> set[str]:
    """Ideas that already produced a paper position (one shadow per idea, ever)."""
    path = path or LEDGER_PATH
    if not path.exists():
        return set()
    out: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("event") == "open" and rec.get("idea_id"):
            out.add(str(rec["idea_id"]))
    return out


def sync_ideas(shadows: dict[str, Shadow], ideas: list[dict[str, Any]], today: date,
               done: set[str] | None = None) -> None:
    """Create watches for approved Heat ideas; expire stale watches."""
    done = done if done is not None else traded_ids()
    live: set[str] = set()
    for idea in ideas:
        iid = str(idea.get("id") or "")
        trig = idea.get("trigger_price")
        direction = str(idea.get("direction") or "").lower()
        if not iid or trig is None or idea.get("decision") != "approved" or direction not in {"long", "short"}:
            continue
        if _sessions_since(str(idea.get("created_at")), today) > MAX_IDEA_AGE_SESSIONS or iid in done:
            continue
        live.add(iid)
        if iid in shadows:
            continue
        shadows[iid] = Shadow(
            idea_id=iid, ticker=str(idea.get("ticker")).upper(), direction=direction,
            trigger=float(trig), operator=str(idea.get("trigger_operator") or "above"),
            target=float(idea["target_price"]) if idea.get("target_price") else None,
        )
        append_ledger({"event": "watch", "idea_id": iid, "ticker": shadows[iid].ticker,
                       "direction": direction, "trigger": float(trig), "ts": datetime.now(ET).isoformat()})
    for iid, s in shadows.items():
        if s.status == "watching" and iid not in live:
            s.status = "expired"
            append_ledger({"event": "expired", "idea_id": iid, "ticker": s.ticker, "ts": datetime.now(ET).isoformat()})


def run_once(session: Session, shadows: dict[str, Shadow], now: datetime, ideas: list[dict[str, Any]] | None = None) -> bool:
    """One poll. Returns True when state changed."""
    before = json.dumps({k: asdict(v) for k, v in shadows.items()}, sort_keys=True)
    if ideas is None:
        ideas = load_materialized_heat_ideas()
    sync_ideas(shadows, ideas, now.date(), done=traded_ids() | {k for k, v in shadows.items() if v.status == "open"})
    if in_regular_hours(now):
        active = [s for s in shadows.values() if s.status in ("watching", "open")]
        prices = underlying_prices(session, sorted({s.ticker for s in active}))
        for s in active:
            px = prices.get(s.ticker)
            if px is None:
                continue
            if s.status == "watching":
                if not is_plausible_trigger(s.trigger, px):
                    # a mis-parsed ratio or indicator value (``fib 1.618``) is not a level
                    s.status = "expired"
                    append_ledger({"event": "expired", "idea_id": s.idea_id, "ticker": s.ticker,
                                   "reason": f"implausible_trigger {s.trigger} vs {px}", "ts": now.isoformat()})
                    continue
                if crossed(px, s.trigger, s.operator) and now.time() < FLATTEN_TIME:
                    try:
                        open_shadow(session, s, px, now)
                    except Exception as exc:  # noqa: BLE001 - keep polling other ideas
                        log.warning("open_shadow %s failed: %s", s.ticker, exc)
            elif s.status == "open":
                # contracts expire at their own close; a shadow still open past
                # expiration is force-closed at the last bid we can get
                bid, _ask = option_quote(session, s.contract["instrument_id"])
                manage_open(s, px, bid, now)
    # drop closed/expired shadows from state (they live in the ledger)
    for iid in [k for k, v in shadows.items() if v.status in ("closed", "expired")]:
        shadows.pop(iid)
    after = json.dumps({k: asdict(v) for k, v in shadows.items()}, sort_keys=True)
    return before != after


def report(path: Path | None = None) -> str:
    path = path or LEDGER_PATH
    if not path.exists():
        return "no ledger"
    rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    voided = {r["idea_id"] for r in rows if r["event"] == "void"}
    closed = [r for r in rows if r["event"] == "closed" and r["idea_id"] not in voided]
    watches = sum(1 for r in rows if r["event"] == "watch")
    opens = sum(1 for r in rows if r["event"] == "open")
    lines = [f"watches={watches} opened={opens} closed={len(closed)}"]
    if closed:
        pct = [r["realized_pct"] for r in closed]
        usd = [r["realized_usd"] for r in closed]
        lines.append(f"realized: ${sum(usd):+.2f} on {CONTRACTS} contracts/trade | per trade avg {st.mean(pct):+.1f}% "
                     f"med {st.median(pct):+.1f}% win {sum(1 for x in pct if x > 0) / len(pct) * 100:.0f}%")
        by = {}
        for r in closed:
            by.setdefault(r["exit_reason"], []).append(r["realized_pct"])
        for k, v in sorted(by.items(), key=lambda kv: -len(kv[1])):
            lines.append(f"  {k:<22} n={len(v):<3} avg={st.mean(v):+.1f}%")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="Heat option shadow tracker (paper only)")
    p.add_argument("--report", action="store_true")
    p.add_argument("--once", action="store_true")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if args.report:
        print(report())
        return
    from bot.robinhood_mcp_client import _MCPSession, _load_token
    session = _MCPSession(_load_token())
    shadows = load_state()
    while True:
        now = datetime.now(ET)
        try:
            if run_once(session, shadows, now):
                save_state(shadows)
        except Exception as exc:  # noqa: BLE001
            log.warning("poll failed: %s", exc)
            try:
                session = _MCPSession(_load_token())
            except Exception as exc2:  # noqa: BLE001
                log.warning("reconnect failed: %s", exc2)
        if args.once:
            break
        time.sleep(POLL_S if in_regular_hours(now) else 60)


if __name__ == "__main__":
    main()
