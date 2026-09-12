"""
Unified day-trade signal dataset for replay.

Every executable level the bot has ever seen becomes one row, whatever its
source, so the same replay engine can score Discord PLANs, Heat ideas and
manual watches side by side:

    source        discord | heat | manual
    ticker        signal ticker (risk is judged here)
    trigger       price level; operator above|below
    direction     long|short (economic)
    target        optional target price
    posted_at     ISO, when the level was published
    session       first tradable session date (US/Eastern)
    execution     symbol actually traded (leveraged ETF route or the ticker)
    leverage      1.0 for the ticker itself
    actual        the live lifecycle for this signal when one exists
                  (fill/exit prices, exit reason, realized P&L)

    python -m bot.signal_dataset --out data/signals_dataset.jsonl \
        --discord logs/history.jsonl --discord .review-20260911/vps/signals.jsonl \
        --heat .review-20260911/vps/heat_ideas.jsonl \
        --heat-decisions .review-20260911/vps/heat_idea_decisions.jsonl \
        --positions .review-20260911/vps/day_trade_positions.jsonl

Heat rows only carry a level when Heat's own words state one (or an operator
approved one); chart-only ideas are listed with ``trigger=None`` so the gap
is visible, and are skipped by the replay.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from bot.heat_ideas import is_plausible_trigger, materialize_heat_ideas, read_jsonl
from bot.leveraged_etfs import leveraged_candidates
from bot.market_data import ET, session_date_for


@dataclass
class SignalRow:
    id: str
    source: str
    ticker: str
    trigger: float | None
    operator: str
    direction: str
    target: float | None
    posted_at: str
    session: str
    execution: str
    leverage: float
    setup: str | None = None
    text: str | None = None
    actual: dict[str, Any] | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _parse_ts(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _route(ticker: str, direction: str) -> tuple[str, float]:
    """Execution symbol the live bot would prefer for this signal."""
    candidates = leveraged_candidates(ticker, direction)
    if candidates:
        return candidates[0].ticker, candidates[0].leverage
    return ticker.upper(), 1.0


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

def discord_rows(paths: Iterable[Path]) -> list[SignalRow]:
    rows: dict[str, SignalRow] = {}
    for path in paths:
        for rec in read_jsonl(path):
            if rec.get("kind") != "PLAN" or not rec.get("ticker") or rec.get("trigger") is None:
                continue
            if str(rec.get("side") or "LONG").upper() != "LONG":
                continue
            discord = rec.get("discord") or {}
            posted = _parse_ts(discord.get("created_at") or rec.get("received_at"))
            if posted is None:
                continue
            sig_id = str(discord.get("message_id") or f"{rec['ticker']}:{posted.isoformat()}")
            ticker = str(rec["ticker"]).upper()
            rows[sig_id] = SignalRow(
                id=f"discord:{sig_id}", source="discord", ticker=ticker,
                trigger=float(rec["trigger"]), operator="above", direction="long",
                target=float(rec["target"]) if rec.get("target") is not None else None,
                posted_at=posted.isoformat(), session=session_date_for(posted).isoformat(),
                execution=ticker, leverage=1.0, setup=rec.get("setup"),
            )
    return list(rows.values())


def heat_rows(ideas_path: Path, decisions_path: Path | None) -> list[SignalRow]:
    ideas = materialize_heat_ideas(
        read_jsonl(ideas_path), read_jsonl(decisions_path) if decisions_path else (),
    )
    rows: list[SignalRow] = []
    for idea in ideas:
        ticker = str(idea.get("ticker") or "").upper()
        posted = _parse_ts(idea.get("created_at"))
        if not ticker or posted is None:
            continue
        direction = str(idea.get("direction") or "").lower()
        if direction not in ("long", "short"):
            continue
        trigger = idea.get("trigger_price")
        notes: list[str] = []
        classification = str(idea.get("classification") or "")
        if classification in ("position_update", "market_context", "swing_dca"):
            continue
        if trigger is not None and not is_plausible_trigger(float(trigger), None):
            trigger = None
        if idea.get("decision") == "rejected":
            notes.append("operator_rejected")
        if trigger is None:
            notes.append("no_numeric_level")
        execution, leverage = _route(ticker, direction)
        if direction == "short" and leverage == 1.0:
            notes.append("short_without_inverse_route")
        rows.append(SignalRow(
            id=f"heat:{idea.get('id')}", source="heat", ticker=ticker,
            trigger=float(trigger) if trigger is not None else None,
            operator=str(idea.get("trigger_operator") or "above"), direction=direction,
            target=float(idea["target_price"]) if idea.get("target_price") is not None else None,
            posted_at=posted.isoformat(), session=session_date_for(posted).isoformat(),
            execution=execution, leverage=leverage,
            setup=idea.get("setup"), text=idea.get("text"), notes=notes,
        ))
    return rows


def manual_rows(plans_path: Path | None) -> list[SignalRow]:
    if not plans_path or not plans_path.exists():
        return []
    data = json.loads(plans_path.read_text(encoding="utf-8"))
    plans = data.get("plans", data) if isinstance(data, dict) else data
    rows = []
    for plan in plans or []:
        posted = _parse_ts(plan.get("created_at"))
        ticker = str(plan.get("ticker") or "").upper()
        if posted is None or not ticker or plan.get("trigger_price") is None:
            continue
        rows.append(SignalRow(
            id=f"manual:{plan.get('id')}", source="manual", ticker=ticker,
            trigger=float(plan["trigger_price"]), operator="above", direction="long",
            target=float(plan["target_price"]) if plan.get("target_price") is not None else None,
            posted_at=posted.isoformat(), session=session_date_for(posted).isoformat(),
            execution=ticker, leverage=1.0, setup=plan.get("setup"),
        ))
    return rows


# ---------------------------------------------------------------------------
# Actual lifecycles
# ---------------------------------------------------------------------------

def attach_actuals(rows: list[SignalRow], positions_path: Path | None) -> int:
    """Link each signal to the live day-trade lifecycle it produced, if any."""
    if not positions_path or not positions_path.exists():
        return 0
    latest: dict[str, dict[str, Any]] = {}
    for rec in read_jsonl(positions_path):
        if rec.get("id"):
            latest[str(rec["id"])] = rec
    by_plan: dict[str, dict[str, Any]] = {}
    for pos in latest.values():
        key = str(pos.get("plan_signal_id") or "")
        if key.startswith("heat:") or key.startswith("manual:"):
            by_plan[key] = pos
        elif key:
            by_plan[f"discord:{key}"] = pos
    linked = 0
    for row in rows:
        pos = by_plan.get(row.id)
        if pos is None:
            continue
        linked += 1
        row.actual = {
            "position_id": pos.get("id"),
            "status": pos.get("status"),
            "execution": (pos.get("execution_ticker") or pos.get("ticker") or "").upper(),
            "fill_price": pos.get("fill_price"),
            "fill_qty": pos.get("fill_qty"),
            "entered_at": pos.get("entered_at"),
            "signal_entry_price": pos.get("signal_entry_price"),
            "exit_price": pos.get("exit_price"),
            "exit_reason": pos.get("exit_reason"),
            "closed_at": pos.get("closed_at"),
            "realized_pnl": pos.get("realized_pnl"),
            "realized_pnl_pct": pos.get("realized_pnl_pct"),
        }
        if pos.get("execution_ticker"):
            row.execution = str(pos["execution_ticker"]).upper()
            row.leverage = float(pos.get("execution_leverage") or row.leverage)
    return linked


# ---------------------------------------------------------------------------
# Build / read
# ---------------------------------------------------------------------------

def build(discord: list[Path], heat: Path | None, heat_decisions: Path | None,
          manual: Path | None, positions: Path | None) -> list[SignalRow]:
    rows = discord_rows(discord)
    if heat:
        rows += heat_rows(heat, heat_decisions)
    rows += manual_rows(manual)
    attach_actuals(rows, positions)
    rows.sort(key=lambda r: r.posted_at)
    return rows


def write_dataset(rows: Iterable[SignalRow], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row.to_dict(), ensure_ascii=False) + "\n")


def read_dataset(path: Path) -> list[SignalRow]:
    rows = []
    for rec in read_jsonl(path):
        rows.append(SignalRow(**{k: rec.get(k) for k in SignalRow.__dataclass_fields__}))
    return rows


def bar_requirements(rows: Iterable[SignalRow], sessions: int | dict[str, int] | None = None) -> set[tuple[str, date]]:
    """(symbol, day) pairs the replay needs: ticker and execution symbol.

    ``sessions`` is a fixed count or a per-source mapping; by default the
    replay's own per-source spans are used.
    """
    from bot.market_data import next_trading_day
    if sessions is None:
        from bot.replay import SESSIONS_BY_SOURCE
        sessions = SESSIONS_BY_SOURCE
    pairs: set[tuple[str, date]] = set()
    for row in rows:
        if row.trigger is None:
            continue
        span = sessions if isinstance(sessions, int) else sessions.get(row.source, 1)
        day = date.fromisoformat(row.session)
        for _ in range(span):
            pairs.add((row.ticker, day))
            pairs.add((row.execution, day))
            day = next_trading_day(day)
    return pairs


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Build the unified signal dataset")
    parser.add_argument("--out", type=Path, default=Path("data/signals_dataset.jsonl"))
    parser.add_argument("--discord", type=Path, action="append", default=[])
    parser.add_argument("--heat", type=Path)
    parser.add_argument("--heat-decisions", type=Path)
    parser.add_argument("--manual", type=Path)
    parser.add_argument("--positions", type=Path)
    args = parser.parse_args(argv)
    rows = build(args.discord, args.heat, args.heat_decisions, args.manual, args.positions)
    write_dataset(rows, args.out)
    by_source: dict[str, list[SignalRow]] = {}
    for row in rows:
        by_source.setdefault(row.source, []).append(row)
    print(f"wrote {len(rows)} rows -> {args.out}")
    for source, items in sorted(by_source.items()):
        with_level = sum(1 for r in items if r.trigger is not None)
        with_actual = sum(1 for r in items if r.actual)
        closed = sum(1 for r in items if r.actual and r.actual.get("status") == "closed")
        print(f"  {source:<8} rows={len(items):<4} with_level={with_level:<4} live_lifecycles={with_actual:<4} closed={closed}")
    pairs = bar_requirements(rows)
    print(f"  bar sessions needed: {len(pairs)}")


if __name__ == "__main__":
    main()
