"""
Minute-bar replay of the day-trade rules over the unified signal dataset.

The engine re-runs the *live* policy (arm below the level, cross, +0.2% entry
cap, -2% initial stop, stepped milestones, 15:30 EOD tighten, 15:50 force
close, leveraged-ETF execution with risk judged on the underlying) on cached
IBKR 1-minute bars, and next to it a small pre-registered set of variants:

    live            the deployed policy
    hold            same entry, no stop, exit at the force-close bar
    stop1 / stop3   fixed -1% / -3% stop, no trailing, EOD exit
    stop2_flat      -2% stop, no milestones (isolates the trailing rules)
    bracket         -2% stop with the signal's target as a limit (when given)

Every row also gets MFE/MAE from entry to the force-close bar.  Rows that
produced a live lifecycle are compared with it (fill, exit reason, P&L) so
the gap between model and reality is measured, not assumed.

    python -m bot.replay --dataset data/signals_dataset.jsonl
    python -m bot.replay --dataset ... --json out.json --trades trades.jsonl

Approximations (1-minute bars vs 5-second live polls)
-----------------------------------------------------
- A cross is detected on the first bar whose high reaches the level; the
  fill is the level itself (or the bar open when the bar opens beyond it),
  capped by the entry guard.  A bar that *opens* beyond the guard is a gap.
- Stops fire on bar lows (or the open when the bar opens through the stop).
- Milestones confirm on bar closes: one close for the first, two for the
  rest.  Live confirms on 5-second polls, so live locks in a little sooner.
- For leveraged routes the ETF fill is the ETF bar close of the minute the
  underlying rule fired.  Real fills are a few seconds after the cross.
"""

from __future__ import annotations

import argparse
import json
import statistics as st
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date, datetime, time as dtime
from pathlib import Path
from typing import Any

from bot.market_data import ET, Bar, load_bars, next_trading_day
from bot.signal_dataset import SignalRow, read_dataset

ENTRY_CAP_PCT = 0.2
INITIAL_STOP_PCT = 2.0
MILESTONES = [(1.0, -0.5), (2.0, 0.2), (3.0, 0.5), (6.0, 3.0), (9.0, 6.0), (12.0, 9.0), (15.0, 12.0), (18.0, 15.0), (21.0, 18.0)]
FIRST_MILESTONE_CONFIRM = 1
CONFIRM_BARS = 2
EOD_TIGHTEN = dtime(15, 30)
FORCE_CLOSE = dtime(15, 50)
BUDGET_USD = 20.0


@dataclass
class Policy:
    name: str
    stop_pct: float | None = INITIAL_STOP_PCT
    trailing: bool = True
    eod_tighten: bool = True
    use_target: bool = False
    # "touch": the bar's high reaching the level counts as a cross (optimistic:
    # a one-second wick is enough).  "close": the bar must also close beyond
    # the level, closer to what a 5-15 s poll actually observes.
    entry_mode: str = "touch"
    # Research knobs (defaults reproduce the live rules):
    milestones: tuple[tuple[float, float], ...] | None = None   # override MILESTONES
    exit_time: dtime | None = None                               # flatten at this ET time
    trim_pct: float | None = None                                # take part off at +trim_pct
    trim_frac: float = 0.5                                       # fraction sold at the trim


# Sessions a level stays live in the replay, per source.  Discord plans carry
# into the next session; Heat and manual watches are good-til-cancelled and
# have filled up to two weeks after posting.
SESSIONS_BY_SOURCE = {"discord": 2, "heat": 10, "manual": 10, "scan": 10, "random": 10}


POLICIES = [
    Policy("live", INITIAL_STOP_PCT, True, True, True),
    Policy("hold", None, False, False, False),
    Policy("stop1", 1.0, False, False, False),
    Policy("stop2_flat", 2.0, False, False, False),
    Policy("stop3", 3.0, False, False, False),
    Policy("bracket", 2.0, False, False, True),
]


@dataclass
class ReplayResult:
    signal_id: str
    source: str
    ticker: str
    execution: str
    leverage: float
    policy: str
    session: str
    entered: bool
    skip_reason: str | None
    entry_ts: str | None
    entry_price: float | None       # execution symbol
    entry_risk_price: float | None  # underlying mark at entry
    exit_ts: str | None
    exit_price: float | None
    exit_reason: str | None
    pnl_pct: float | None           # on the execution symbol
    pnl_usd: float | None           # BUDGET_USD sized
    mfe_pct: float | None           # on the risk basis, entry -> force close
    mae_pct: float | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Core simulation
# ---------------------------------------------------------------------------

def _at(bars: list[Bar], ts: datetime) -> Bar | None:
    for b in bars:
        if b.ts == ts:
            return b
    return None


def _exec_price(exec_bars: list[Bar], ts: datetime, fallback: float) -> float:
    b = _at(exec_bars, ts)
    return b.close if b else fallback


def simulate(
    row: SignalRow,
    risk_bars: list[Bar],
    exec_bars: list[Bar],
    policy: Policy,
    *,
    session: date,
    armed: bool,
) -> ReplayResult:
    short = row.direction == "short"
    trigger = float(row.trigger)
    posted = datetime.fromisoformat(row.posted_at).astimezone(ET)
    direct = row.execution.upper() == row.ticker.upper()
    base = dict(signal_id=row.id, source=row.source, ticker=row.ticker, execution=row.execution,
                leverage=row.leverage, policy=policy.name, session=session.isoformat())

    def skipped(reason: str) -> ReplayResult:
        return ReplayResult(**base, entered=False, skip_reason=reason, entry_ts=None, entry_price=None,
                            entry_risk_price=None, exit_ts=None, exit_price=None, exit_reason=None,
                            pnl_pct=None, pnl_usd=None, mfe_pct=None, mae_pct=None)

    if not risk_bars:
        return skipped("no_bars")
    if not direct and not exec_bars:
        return skipped("no_execution_bars")

    def crossed(px: float) -> bool:
        return px <= trigger if row.operator == "below" else px >= trigger

    def arms(px: float) -> bool:
        return px > trigger if row.operator == "below" else px < trigger

    guard = round(trigger * (1 - ENTRY_CAP_PCT / 100), 2) if row.operator == "below" else round(trigger * (1 + ENTRY_CAP_PCT / 100), 2)

    entry_idx: int | None = None
    entry_risk: float | None = None
    for i, b in enumerate(risk_bars):
        if b.ts < posted:
            continue
        if not armed:
            if arms(b.close):
                armed = True
            continue
        if policy.entry_mode == "close":
            # Confirmation rule: act only once a full minute has closed beyond
            # the level, and pay that close (not the level) for the delay.
            if crossed(b.close):
                past_guard = b.close < guard if row.operator == "below" else b.close > guard
                if past_guard:
                    armed = False
                    continue
                entry_idx = i
                entry_risk = b.close
                break
            continue
        beyond = b.open < guard if row.operator == "below" else b.open > guard
        if crossed(b.high if row.operator == "above" else b.low):
            if beyond:
                # Gapped past the entry cap at this bar's open: live re-arms
                # (GTC) or expires (Discord).  Either way no fill this bar.
                armed = False
                continue
            entry_idx = i
            entry_risk = trigger if (b.open <= trigger if row.operator == "above" else b.open >= trigger) else b.open
            break
    if entry_idx is None:
        return skipped("never_triggered" if not armed else "never_triggered")
    if risk_bars[entry_idx].ts.time() >= FORCE_CLOSE:
        return skipped("triggered_after_force_close")

    entry_bar = risk_bars[entry_idx]
    if direct:
        entry_px = float(entry_risk)
        anchor = entry_px
    else:
        entry_px = _exec_price(exec_bars, entry_bar.ts, float("nan"))
        if entry_px != entry_px:
            return skipped("no_execution_bar_at_entry")
        anchor = float(entry_risk)

    # ---- manage -----------------------------------------------------------
    def level(pct: float) -> float:
        return round(anchor * (1 - pct / 100), 4) if short else round(anchor * (1 + pct / 100), 4)

    stop = level(-policy.stop_pct) if policy.stop_pct is not None else None
    target = row.target if (policy.use_target and row.target) else None
    milestones = list(policy.milestones) if policy.milestones is not None else MILESTONES
    trimmed_pnl = 0.0          # realised pnl% contribution of the trimmed part
    remaining = 1.0            # fraction of the position still open
    milestone_idx = 0
    confirm = 0
    confirm_idx: int | None = None
    tightened = False
    mfe = 0.0
    mae = 0.0
    exit_ts: datetime | None = None
    exit_px: float | None = None
    exit_reason: str | None = None

    def favorable(px: float) -> float:
        return (anchor - px) / anchor * 100 if short else (px - anchor) / anchor * 100

    for b in risk_bars[entry_idx + 1:]:
        hi_move = favorable(b.low if short else b.high)
        lo_move = favorable(b.high if short else b.low)
        mfe = max(mfe, hi_move)
        mae = min(mae, lo_move)

        if stop is not None:
            hit = (b.high >= stop) if short else (b.low <= stop)
            if hit:
                gapped = (b.open >= stop) if short else (b.open <= stop)
                risk_exit = b.open if gapped else stop
                exit_ts, exit_reason = b.ts, "stop"
                exit_px = risk_exit if direct else _exec_price(exec_bars, b.ts, risk_exit)
                break
        if target is not None:
            hit = (b.low <= target) if short else (b.high >= target)
            if hit:
                exit_ts, exit_reason = b.ts, "target"
                exit_px = target if direct else _exec_price(exec_bars, b.ts, target)
                break
        if b.ts.time() >= FORCE_CLOSE or (policy.exit_time is not None and b.ts.time() >= policy.exit_time):
            exit_ts, exit_reason = b.ts, "eod" if b.ts.time() >= FORCE_CLOSE else "time"
            exit_px = b.close if direct else _exec_price(exec_bars, b.ts, b.close)
            break
        if policy.trim_pct is not None and remaining == 1.0 and hi_move >= policy.trim_pct:
            trim_risk = level(policy.trim_pct)
            trim_px = trim_risk if direct else _exec_price(exec_bars, b.ts, trim_risk)
            part = (trim_px - entry_px) / entry_px * 100
            if short and direct:
                part = -part
            trimmed_pnl = part * policy.trim_frac
            remaining = 1.0 - policy.trim_frac
        if policy.eod_tighten and not tightened and b.ts.time() >= EOD_TIGHTEN:
            new_stop = round(b.close * 1.01, 4) if short else round(b.close * 0.99, 4)
            if stop is None or (new_stop < stop if short else new_stop > stop):
                stop = new_stop
            tightened = True
            continue
        if policy.trailing:
            eligible = None
            for idx in range(milestone_idx, len(milestones)):
                thr, _ = milestones[idx]
                if favorable(b.close) >= thr:
                    eligible = idx
                else:
                    break
            if eligible is None:
                confirm, confirm_idx = 0, None
            else:
                confirm = confirm + 1 if confirm_idx == eligible else 1
                confirm_idx = eligible
                need = FIRST_MILESTONE_CONFIRM if eligible == 0 else CONFIRM_BARS
                if confirm >= need:
                    _, lock = milestones[eligible]
                    new_stop = level(lock)
                    if stop is None or (new_stop < stop if short else new_stop > stop):
                        stop = new_stop
                    confirm, confirm_idx = 0, None
                    milestone_idx = eligible + 1
    if exit_ts is None:
        last = risk_bars[-1]
        exit_ts, exit_reason = last.ts, "eod"
        exit_px = last.close if direct else _exec_price(exec_bars, last.ts, last.close)

    pnl_pct = (exit_px - entry_px) / entry_px * 100
    if short and direct:
        pnl_pct = -pnl_pct
    pnl_pct = trimmed_pnl + pnl_pct * remaining
    return ReplayResult(**base, entered=True, skip_reason=None, entry_ts=entry_bar.ts.isoformat(),
                        entry_price=round(entry_px, 4), entry_risk_price=round(anchor, 4),
                        exit_ts=exit_ts.isoformat(), exit_price=round(exit_px, 4), exit_reason=exit_reason,
                        pnl_pct=round(pnl_pct, 4), pnl_usd=round(BUDGET_USD * pnl_pct / 100, 4),
                        mfe_pct=round(mfe, 4), mae_pct=round(mae, 4))


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def replay_row(row: SignalRow, policies: list[Policy], *, sessions: int | None = None, root: Path | None = None) -> list[ReplayResult]:
    """Replay one signal; GTC sources may be tried over several sessions.

    The replay stops at the first session with a fill.  A session with no
    cached bars ends the search; the reported skip reason is then
    ``never_triggered`` when at least one session had bars, otherwise
    ``no_bars``.
    """
    results: list[ReplayResult] = []
    posted = datetime.fromisoformat(row.posted_at).astimezone(ET)
    span = sessions or SESSIONS_BY_SOURCE.get(row.source, 1)
    for policy in policies:
        day = date.fromisoformat(row.session)
        armed = row.source == "discord" and posted.date() == day
        outcome: ReplayResult | None = None
        had_bars = False
        for _ in range(span):
            risk = load_bars(row.ticker, day, root) or []
            execb = risk if row.execution.upper() == row.ticker.upper() else (load_bars(row.execution, day, root) or [])
            outcome = simulate(row, risk, execb, policy, session=day, armed=armed)
            if outcome.entered or outcome.skip_reason in ("no_bars", "no_execution_bars"):
                break
            had_bars = True
            day = next_trading_day(day)
            armed = False
        assert outcome is not None
        if not outcome.entered and had_bars and outcome.skip_reason in ("no_bars", "no_execution_bars"):
            outcome.skip_reason = "never_triggered"
        results.append(outcome)
    return results


def summarize(results: list[ReplayResult]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    groups: dict[tuple[str, str], list[ReplayResult]] = defaultdict(list)
    for r in results:
        groups[(r.policy, r.source)].append(r)
        groups[(r.policy, "all")].append(r)
    for (policy, source), items in sorted(groups.items()):
        entered = [r for r in items if r.entered]
        pnls = [r.pnl_pct for r in entered]
        usd = [r.pnl_usd for r in entered]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        gp = sum(usd[i] for i, p in enumerate(pnls) if p > 0)
        gl = -sum(usd[i] for i, p in enumerate(pnls) if p < 0)
        skips = defaultdict(int)
        for r in items:
            if not r.entered:
                skips[r.skip_reason] += 1
        reasons = defaultdict(int)
        for r in entered:
            reasons[r.exit_reason] += 1
        out[f"{policy}/{source}"] = {
            "signals": len(items), "entered": len(entered),
            "win_rate": round(len(wins) / len(pnls) * 100, 1) if pnls else None,
            "avg_pnl_pct": round(st.mean(pnls), 3) if pnls else None,
            "median_pnl_pct": round(st.median(pnls), 3) if pnls else None,
            "net_usd": round(sum(usd), 2), "profit_factor": round(gp / gl, 2) if gl > 0 else None,
            "avg_mfe_pct": round(st.mean(r.mfe_pct for r in entered), 2) if entered else None,
            "avg_mae_pct": round(st.mean(r.mae_pct for r in entered), 2) if entered else None,
            "exits": dict(reasons), "skips": dict(skips),
        }
    return out


def compare_with_live(rows: list[SignalRow], results: list[ReplayResult]) -> dict[str, Any]:
    """Model-vs-reality on lifecycles that actually closed."""
    live = {r.signal_id: r for r in results if r.policy == "live"}
    diffs = []
    for row in rows:
        a = row.actual
        if not a or a.get("status") != "closed" or a.get("fill_price") is None or a.get("realized_pnl") is None:
            continue
        r = live.get(row.id)
        if r is None:
            continue
        cost = float(a["fill_price"]) * float(a.get("fill_qty") or 0)
        live_pct = float(a["realized_pnl"]) / cost * 100 if cost else None
        diffs.append({
            "signal": row.id, "ticker": row.ticker, "execution": row.execution,
            "live_fill": a["fill_price"], "model_fill": r.entry_price,
            "fill_slippage_pct": round((float(a["fill_price"]) - r.entry_price) / r.entry_price * 100, 3) if r.entered and r.entry_price else None,
            "live_exit_reason": a.get("exit_reason"), "model_exit_reason": r.exit_reason,
            "live_pnl_pct": round(live_pct, 3) if live_pct is not None else None,
            "model_pnl_pct": r.pnl_pct, "model_entered": r.entered, "model_skip": r.skip_reason,
        })
    entered_both = [d for d in diffs if d["model_entered"]]
    slip = [d["fill_slippage_pct"] for d in entered_both if d["fill_slippage_pct"] is not None]
    gap = [d["live_pnl_pct"] - d["model_pnl_pct"] for d in entered_both if d["live_pnl_pct"] is not None and d["model_pnl_pct"] is not None]
    same_reason = sum(1 for d in entered_both if d["live_exit_reason"] == d["model_exit_reason"])
    return {
        "closed_live_trades": len(diffs), "model_also_entered": len(entered_both),
        "model_missed": [d["signal"] + ":" + str(d["model_skip"]) for d in diffs if not d["model_entered"]],
        "fill_slippage_pct": {"mean": round(st.mean(slip), 3), "median": round(st.median(slip), 3)} if slip else None,
        "pnl_gap_live_minus_model_pct": {"mean": round(st.mean(gap), 3), "median": round(st.median(gap), 3)} if gap else None,
        "same_exit_reason": f"{same_reason}/{len(entered_both)}",
        "rows": diffs,
    }


def format_summary(summary: dict[str, dict[str, Any]], comparison: dict[str, Any]) -> str:
    lines = ["policy/source        n   in   win%   avg%   med%    net$   PF   mfe%   mae%   exits"]
    for key, s in summary.items():
        lines.append(
            f"{key:<18} {s['signals']:>4} {s['entered']:>4}  {s['win_rate'] if s['win_rate'] is not None else '-':>5}  "
            f"{s['avg_pnl_pct'] if s['avg_pnl_pct'] is not None else '-':>6} {s['median_pnl_pct'] if s['median_pnl_pct'] is not None else '-':>6} "
            f"{s['net_usd']:>7.2f} {s['profit_factor'] if s['profit_factor'] is not None else '-':>5} "
            f"{s['avg_mfe_pct'] if s['avg_mfe_pct'] is not None else '-':>6} {s['avg_mae_pct'] if s['avg_mae_pct'] is not None else '-':>6}  {s['exits']}"
        )
    skips = {k: v["skips"] for k, v in summary.items() if k.startswith("live/")}
    lines.append("")
    lines.append(f"skips (live policy): {json.dumps(skips)}")
    lines.append("")
    lines.append("MODEL vs LIVE (closed lifecycles)")
    for k in ("closed_live_trades", "model_also_entered", "fill_slippage_pct", "pnl_gap_live_minus_model_pct", "same_exit_reason"):
        lines.append(f"  {k}: {comparison[k]}")
    if comparison["model_missed"]:
        lines.append(f"  model_missed: {comparison['model_missed']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Replay day-trade rules on cached minute bars")
    parser.add_argument("--dataset", type=Path, default=Path("data/signals_dataset.jsonl"))
    parser.add_argument("--bars", type=Path, default=None)
    parser.add_argument("--sessions", type=int, default=None,
                        help="override sessions a level stays live (default: per source, see SESSIONS_BY_SOURCE)")
    parser.add_argument("--entry", choices=["touch", "close"], default="touch",
                        help="cross detection: bar high touches the level, or the bar closes beyond it")
    parser.add_argument("--json", type=Path)
    parser.add_argument("--trades", type=Path)
    parser.add_argument("--source", choices=["discord", "heat", "manual"])
    args = parser.parse_args(argv)

    rows = [r for r in read_dataset(args.dataset) if r.trigger is not None]
    if args.source:
        rows = [r for r in rows if r.source == args.source]
    policies = [Policy(**{**asdict(p), "entry_mode": args.entry}) for p in POLICIES]
    results: list[ReplayResult] = []
    for row in rows:
        results.extend(replay_row(row, policies, sessions=args.sessions, root=args.bars))
    summary = summarize(results)
    comparison = compare_with_live(rows, results)
    print(format_summary(summary, comparison))
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps({"summary": summary, "comparison": comparison}, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.trades:
        args.trades.parent.mkdir(parents=True, exist_ok=True)
        with args.trades.open("w", encoding="utf-8") as fh:
            for r in results:
                fh.write(json.dumps(r.to_dict(), ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
