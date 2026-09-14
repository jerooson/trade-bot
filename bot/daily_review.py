"""
Daily review: one page per session assembled from the ledgers.

Sections: Heat feed (what he posted, what got approved, what stayed in
review), chart analyzer output, day trades (fills, slippage, exits, P&L),
"Heat said / bot did" reconciliation, recorded-only Discord plans, option
shadow results, swing activity, running totals and service health.

    python -m bot.daily_review                 # today (ET), writes logs/reviews/<date>.md + .json
    python -m bot.daily_review --date 2026-09-14
"""

from __future__ import annotations

import argparse
import json
import statistics as st
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from bot import performance
from bot.heat_ideas import materialize_heat_ideas, read_jsonl
from bot.position_ownership import read_latest_day_positions

ET = ZoneInfo("America/New_York")
PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Paths:
    root: Path = PROJECT_ROOT
    def __post_init__(self) -> None:
        logs, state = self.root / "logs", self.root / "state"
        self.positions = logs / "day_trade_positions.jsonl"
        self.signals = logs / "signals.jsonl"
        self.swings = logs / "swings.jsonl"
        self.pnl = logs / "trade_pnl.jsonl"
        self.reviews = logs / "robinhood_shadow_reviews.jsonl"
        self.heat_ideas = logs / "heat_ideas.jsonl"
        self.heat_decisions = state / "heat_idea_decisions.jsonl"
        self.chart_analyses = state / "heat_chart_analyses.jsonl"
        self.option_shadow = logs / "option_shadow.jsonl"
        self.option_state = state / "option_shadow.json"
        self.heartbeat = logs / "day_trader.heartbeat"
        self.out_dir = logs / "reviews"


def _ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ET)


def _on(value: Any, day: date) -> bool:
    dt = _ts(value)
    return dt is not None and dt.date() == day


def _hm(value: Any) -> str:
    dt = _ts(value)
    return dt.strftime("%H:%M") if dt else "--:--"


def _pct(a: float | None, b: float | None) -> float | None:
    if a is None or b is None or b == 0:
        return None
    return (a / b - 1) * 100


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------

def heat_section(ideas: list[dict[str, Any]], day: date) -> dict[str, Any]:
    today = [i for i in ideas if _on(i.get("created_at"), day)]
    by_class = Counter(str(i.get("classification")) for i in today)
    by_status = Counter(str(i.get("status")) for i in today)
    approved = [i for i in today if i.get("decision") == "approved"]
    review = [i for i in today if i.get("status") == "needs_review" and i.get("classification") in ("needs_level", "actionable_setup")]
    def row(i: dict[str, Any]) -> dict[str, Any]:
        return {"id": i.get("id"), "time": _hm(i.get("created_at")), "ticker": i.get("ticker"),
                "direction": i.get("direction"), "trigger": i.get("trigger_price"),
                "operator": i.get("trigger_operator"), "target": i.get("target_price"),
                "status": i.get("status"), "classification": i.get("classification"),
                "attachments": len(i.get("attachments") or []),
                "text": str(i.get("text") or "")[:90].replace("\n", " ")}
    return {"count": len(today), "by_classification": dict(by_class), "by_status": dict(by_status),
            "approved": [row(i) for i in approved], "needs_review": [row(i) for i in review],
            "option_posts": [row(i) for i in today if i.get("classification") == "option_post"]}


def chart_section(analyses: list[dict[str, Any]], day: date) -> dict[str, Any]:
    today = [a for a in analyses if _on(a.get("analyzed_at"), day) and a.get("reason") != "backlog_skipped_2026-09-12"]
    return {"count": len(today),
            "rows": [{"ticker": a.get("ticker"), "status": a.get("status"), "trigger": a.get("trigger_price"),
                      "operator": a.get("trigger_operator"), "confidence": a.get("confidence"),
                      "reason": a.get("reason"), "rationale": str(a.get("rationale") or "")[:120]} for a in today]}


def day_trade_section(positions: list[dict[str, Any]], day: date) -> dict[str, Any]:
    touched = [p for p in positions if _on(p.get("entered_at"), day) or _on(p.get("closed_at"), day)
               or _on(p.get("entry_submitted_at"), day)]
    closed, open_, failed = [], [], []
    for p in touched:
        fill, trig = p.get("fill_price"), p.get("trigger_price")
        lev = p.get("execution_leverage") or 1.0
        exec_tk = p.get("execution_ticker") or p.get("ticker")
        # slippage on the executed instrument vs the entry cap the bot set
        # the entry cap is set on the signal ticker; only comparable when we bought it directly
        slip = _pct(fill, p.get("entry_limit_price")) if fill and exec_tk == p.get("ticker") else None
        row = {"ticker": p.get("ticker"), "execution": exec_tk, "leverage": lev, "source": p.get("source"),
               "trigger": trig, "entered": _hm(p.get("entered_at")), "fill_price": fill,
               "fill_qty": p.get("fill_qty"), "fill_usd": p.get("entry_filled_value"),
               "entry_cap": p.get("entry_limit_price"), "slippage_vs_cap_pct": round(slip, 2) if slip is not None else None,
               "exit": _hm(p.get("closed_at")), "exit_price": p.get("exit_price"), "exit_reason": p.get("exit_reason"),
               "pnl_usd": p.get("realized_pnl"), "pnl_pct": p.get("realized_pnl_pct"), "status": p.get("status"),
               "error": p.get("entry_last_error") or p.get("exit_last_error") or p.get("reconciliation_note")}
        if p.get("status") == "closed" and p.get("realized_pnl") is not None:
            closed.append(row)
        elif p.get("status") in ("open", "pending_exit", "unreconciled"):
            open_.append(row)
        elif p.get("entry_last_error") or p.get("status") in ("blocked", "quarantined"):
            failed.append(row)
    pnl = [r["pnl_usd"] for r in closed]
    watching = [p for p in positions if p.get("status") == "watching" and p.get("armed")]
    return {"closed": closed, "open": open_, "failed": failed,
            "summary": {"n": len(closed), "pnl_usd": round(sum(pnl), 2) if pnl else 0.0,
                        "win_rate": round(sum(1 for x in pnl if x > 0) / len(pnl) * 100, 1) if pnl else None,
                        "avg_pct": round(st.mean(r["pnl_pct"] for r in closed if r["pnl_pct"] is not None), 2)
                        if any(r["pnl_pct"] is not None for r in closed) else None},
            "still_watching": [{"ticker": p.get("ticker"), "source": p.get("source"), "trigger": p.get("trigger_price"),
                                "since": _ts(p.get("plan_received_at")).date().isoformat() if _ts(p.get("plan_received_at")) else None}
                               for p in watching]}


def heat_vs_bot(ideas: list[dict[str, Any]], positions: list[dict[str, Any]], day: date) -> list[dict[str, Any]]:
    """For every Heat idea approved on or before ``day`` that was still live, what did the bot do?"""
    by_idea: dict[str, dict[str, Any]] = {}
    for p in positions:
        if p.get("heat_idea_id"):
            by_idea[str(p["heat_idea_id"])] = p
    rows = []
    for i in ideas:
        created = _ts(i.get("created_at"))
        if created is None or created.date() > day or (day - created.date()).days > 14:
            continue
        if i.get("classification") in ("option_post", "market_context", "position_update", "swing_dca"):
            continue
        p = by_idea.get(str(i.get("id")))
        if i.get("decision") != "approved":
            outcome = "not_approved:" + str(i.get("status"))
        elif not i.get("mapping_supported"):
            outcome = "unsupported_mapping"
        elif p is None:
            outcome = "no_watch_created"
        else:
            s = p.get("status")
            if s == "closed":
                outcome = f"traded:{p.get('exit_reason')} {p.get('realized_pnl_pct'):+.2f}%" if p.get("realized_pnl_pct") is not None else "traded"
            elif s == "watching":
                outcome = "watching_not_triggered"
            elif p.get("entry_last_error"):
                outcome = "entry_failed:" + str(p.get("entry_last_error"))[:60]
            else:
                outcome = str(s)
        if created.date() == day or (p and (_on(p.get("entered_at"), day) or _on(p.get("closed_at"), day))):
            rows.append({"time": _hm(i.get("created_at")), "ts": created.isoformat(), "ticker": i.get("ticker"),
                         "trigger": i.get("trigger_price"), "operator": i.get("trigger_operator"),
                         "direction": i.get("direction"), "outcome": outcome,
                         "parse": str(i.get("status")), "attachments": len(i.get("attachments") or []),
                         "pnl_pct": p.get("realized_pnl_pct") if p else None,
                         "exit_reason": p.get("exit_reason") if p else None,
                         "text": str(i.get("text") or "")[:70].replace("\n", " ")})
    rows.sort(key=lambda r: r["ts"])
    return rows


def discord_plans_section(signals: list[dict[str, Any]], day: date) -> dict[str, Any]:
    today = [s for s in signals if s.get("kind") == "PLAN" and _on(s.get("received_at"), day)]
    return {"count": len(today),
            "rows": [{"time": _hm(s.get("received_at")), "ticker": s.get("ticker"), "side": s.get("side"),
                      "trigger": s.get("trigger"), "target": s.get("target")} for s in today]}


def option_shadow_section(events: list[dict[str, Any]], state: dict[str, Any], day: date) -> dict[str, Any]:
    voided = {e.get("idea_id") for e in events if e.get("event") == "void"}
    today = [e for e in events if _on(e.get("ts"), day) and e.get("idea_id") not in voided]
    opens = [e for e in today if e["event"] == "open"]
    closed = [e for e in today if e["event"] == "closed"]
    pct = [c["realized_pct"] for c in closed]
    return {"watches_added": sum(1 for e in today if e["event"] == "watch"),
            "opened": [{"time": _hm(e["ts"]), "ticker": e["ticker"], "contract": f"{e['contract']['expiration']} {e['contract']['strike']}{e['contract']['kind'][0].upper()}",
                        "price": e["price"], "underlying": e["underlying"]} for e in opens],
            "closed": [{"time": _hm(e["ts"]), "ticker": e["ticker"], "exit_reason": e["exit_reason"],
                        "realized_pct": e["realized_pct"], "realized_usd": e["realized_usd"], "max_gain_pct": e["max_gain_pct"]} for e in closed],
            "summary": {"n": len(closed), "avg_pct": round(st.mean(pct), 1) if pct else None,
                        "usd": round(sum(c["realized_usd"] for c in closed), 2) if closed else 0.0},
            "open_now": sum(1 for s in state.values() if s.get("status") == "open"),
            "watching_now": sum(1 for s in state.values() if s.get("status") == "watching")}


def swing_section(swings: list[dict[str, Any]], reviews: list[dict[str, Any]], pnl: list[dict[str, Any]], day: date) -> dict[str, Any]:
    acts = [s for s in swings if _on(s.get("received_at"), day)]
    revs = [r for r in reviews if _on(r.get("reviewed_at"), day)]
    fills = [p for p in pnl if _on(p.get("sold_at") or p.get("filled_at") or p.get("ts"), day)]
    return {"signals": [{"time": _hm(s.get("received_at")), "ticker": s.get("ticker"), "action": s.get("action") or s.get("kind"),
                         "price": s.get("price")} for s in acts],
            "reviews_by_status": dict(Counter(str(r.get("status")) for r in revs)),
            "placed": [{"ticker": r.get("ticker"), "kind": r.get("signal_kind"), "usd": r.get("expected_usd"),
                        "rationale": str(r.get("rationale") or "")[:80]} for r in revs if r.get("status") == "PLACED"],
            "fills": len(fills)}


def health_section(paths: Paths, positions: list[dict[str, Any]], now: datetime) -> dict[str, Any]:
    hb = None
    if paths.heartbeat.exists():
        hb = round((now.timestamp() - paths.heartbeat.stat().st_mtime) / 60, 1)
    stuck = [p for p in positions if p.get("status") in ("pending_exit", "pending_entry")
             and (_ts(p.get("exit_requested_at") or p.get("entry_submitted_at")) or now) < now - timedelta(minutes=30)]
    from bot.acknowledgements import load as load_acks
    acked = load_acks(paths.root / "state" / "acknowledged_positions.json")
    return {"day_trader_heartbeat_age_min": hb,
            "unreconciled": [p.get("ticker") for p in positions
                             if p.get("status") == "unreconciled" and str(p.get("id")) not in acked],
            "stuck_pending": [f"{p.get('ticker')}:{p.get('status')}" for p in stuck]}


# ---------------------------------------------------------------------------
# Assemble / render
# ---------------------------------------------------------------------------

def _load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, json.JSONDecodeError):
        return {}


def build(day: date, paths: Paths | None = None, now: datetime | None = None) -> dict[str, Any]:
    paths = paths or Paths()
    now = now or datetime.now(ET)
    ideas = materialize_heat_ideas(read_jsonl(paths.heat_ideas), read_jsonl(paths.heat_decisions))
    positions = read_latest_day_positions(paths.positions) if paths.positions.exists() else []
    report = {
        "date": day.isoformat(),
        "generated_at": now.isoformat(),
        "heat": heat_section(ideas, day),
        "chart_analyzer": chart_section(read_jsonl(paths.chart_analyses), day),
        "day_trades": day_trade_section(positions, day),
        "heat_vs_bot": heat_vs_bot(ideas, positions, day),
        "discord_plans_recorded": discord_plans_section(read_jsonl(paths.signals), day),
        "option_shadow": option_shadow_section(read_jsonl(paths.option_shadow), _load_json(paths.option_state), day),
        "swing": swing_section(read_jsonl(paths.swings), read_jsonl(paths.reviews), read_jsonl(paths.pnl), day),
        "health": health_section(paths, positions, now),
    }
    try:
        perf = performance.build_report(positions_path=paths.positions, pnl_path=paths.pnl, reviews_path=paths.reviews, now=now.astimezone(timezone.utc))
        report["totals"] = {"day": perf.get("day", {}).get("all_time"), "day_by_month": perf.get("day", {}).get("by_month"),
                            "swing": perf.get("swing", {}).get("all_time"), "combined": perf.get("totals"),
                            "omissions": perf.get("omissions")}
    except Exception as exc:  # noqa: BLE001 - totals are optional
        report["totals"] = {"error": str(exc)[:200]}
    return report


def _money(v: Any) -> str:
    return f"${v:+.2f}" if isinstance(v, (int, float)) else "-"


def _num(v: Any, fmt: str = ".2f") -> str:
    return format(v, fmt) if isinstance(v, (int, float)) else "-"


def render_markdown(r: dict[str, Any]) -> str:
    L: list[str] = [f"# Daily review {r['date']}", "", f"_generated {r['generated_at'][:16]} ET_", ""]
    if r.get("narrative"):
        L += ["## 总结", r["narrative"].strip(), ""]
    elif r.get("narrative_error"):
        L += [f"_{r['narrative_error']}_", ""]
    h = r["heat"]
    L += ["## Heat feed", f"- {h['count']} posts: " + ", ".join(f"{k} {v}" for k, v in sorted(h["by_classification"].items())) or "- no posts"]
    if h["approved"]:
        L += ["", "| time | ticker | dir | trigger | via | text |", "|---|---|---|---|---|---|"]
        L += [f"| {i['time']} | {i['ticker']} | {i['direction']} | {i['operator']} {_num(i['trigger'])} | {i['status']} | {i['text']} |" for i in h["approved"]]
    if h["needs_review"]:
        L += ["", f"Still needs review ({len(h['needs_review'])}):"] + [f"- {i['time']} {i['ticker']} [{i['attachments']} img] {i['text']}" for i in h["needs_review"]]
    if h["option_posts"]:
        L += ["", f"Option posts recorded: {len(h['option_posts'])}"] + [f"- {i['time']} {i['ticker']} {i['text']}" for i in h["option_posts"]]
    c = r["chart_analyzer"]
    L += ["", "## Chart analyzer", f"- {c['count']} charts analyzed"]
    L += [f"- {a['ticker']} {a['status']} {a['operator']} {_num(a['trigger'])} conf={_num(a['confidence'])} {a['reason'] or ''} — {a['rationale']}" for a in c["rows"]]
    d = r["day_trades"]
    s = d["summary"]
    L += ["", "## Day trades", f"- closed {s['n']}, P&L {_money(s['pnl_usd'])}, win {_num(s['win_rate'], '.0f')}%, avg {_num(s['avg_pct'])}%"]
    if d["closed"]:
        L += ["", "| ticker | via | src | in | fill | cap slip% | out | reason | P&L | % |", "|---|---|---|---|---|---|---|---|---|---|"]
        L += [f"| {t['ticker']} | {t['execution']} x{_num(t['leverage'], '.0f')} | {t['source']} | {t['entered']} | {_num(t['fill_price'])} | {_num(t['slippage_vs_cap_pct'])} | {t['exit']} | {t['exit_reason']} | {_money(t['pnl_usd'])} | {_num(t['pnl_pct'])} |" for t in d["closed"]]
    if d["open"]:
        L += ["", "Open / pending:"] + [f"- {t['ticker']} ({t['execution']}) {t['status']} fill={_num(t['fill_price'])} {t['error'] or ''}" for t in d["open"]]
    if d["failed"]:
        L += ["", "Entry failed / blocked:"] + [f"- {t['ticker']} {t['status']}: {t['error']}" for t in d["failed"]]
    if d["still_watching"]:
        L += ["", f"Watching ({len(d['still_watching'])}): " + ", ".join(f"{w['ticker']}@{_num(w['trigger'])} ({w['source']})" for w in d["still_watching"])]
    L += ["", "## Heat said / bot did"]
    L += [f"- {x['time']} {x['ticker']} {x['direction']} {_num(x['trigger'])} → **{x['outcome']}** — {x['text']}" for x in r["heat_vs_bot"]] or ["- nothing to reconcile"]
    dp = r["discord_plans_recorded"]
    L += ["", "## Discord main-channel plans (record only)", f"- {dp['count']} plans: " + ", ".join(f"{p['ticker']}@{_num(p['trigger'])}" for p in dp["rows"])]
    o = r["option_shadow"]
    L += ["", "## Option shadow (paper)", f"- opened {len(o['opened'])}, closed {o['summary']['n']}, avg {_num(o['summary']['avg_pct'], '.1f')}%, {_money(o['summary']['usd'])}; now open {o['open_now']}, watching {o['watching_now']}"]
    L += [f"- open {x['time']} {x['ticker']} {x['contract']} @ {_num(x['price'])} (und {_num(x['underlying'])})" for x in o["opened"]]
    L += [f"- close {x['time']} {x['ticker']} {x['exit_reason']} {_num(x['realized_pct'], '+.1f')}% (max {_num(x['max_gain_pct'], '+.1f')}%)" for x in o["closed"]]
    sw = r["swing"]
    L += ["", "## Swing", f"- signals {len(sw['signals'])}, reviews " + (", ".join(f"{k} {v}" for k, v in sw["reviews_by_status"].items()) or "none") + f", fills {sw['fills']}"]
    L += [f"- placed {p['ticker']} {p['kind']} ${_num(p['usd'])}: {p['rationale']}" for p in sw["placed"]]
    t = r.get("totals") or {}
    L += ["", "## Running totals"]
    if "error" in t:
        L += [f"- unavailable: {t['error']}"]
    else:
        for k in ("day", "swing"):
            v = t.get(k) or {}
            if isinstance(v, dict) and v.get("count"):
                wr = v["wins"] / v["count"] * 100 if v.get("wins") is not None else None
                L += [f"- {k}: n={v['count']} net {_money(v.get('net'))} win {_num(wr, '.0f')}% avg {_money(v.get('avg'))} PF {_num(v.get('profit_factor'))}"]
        if t.get("combined"):
            L += [f"- combined: {t['combined']}"]
        if t.get("omissions"):
            L += [f"- omissions: {len(t['omissions'])}"]
    hl = r["health"]
    L += ["", "## Health", f"- day trader heartbeat age: {_num(hl['day_trader_heartbeat_age_min'], '.1f')} min",
          f"- unreconciled: {', '.join(hl['unreconciled']) or 'none'}", f"- stuck pending: {', '.join(hl['stuck_pending']) or 'none'}"]
    return "\n".join(L) + "\n"


NARRATIVE_PROMPT = """You are reviewing one trading day of an automated Discord-signal bot for its owner (Chinese speaker).
Below is the day's structured review. Write a short narrative in Chinese, at most 8 bullet points:
what worked, what the bot missed versus what Heat posted and why, anything abnormal (errors, stuck positions,
unusual slippage), and one concrete thing to check tomorrow. Numbers only from the data. No headers. Do not call tools.

"""


def narrative(report: dict[str, Any], codex_command: str = "codex", timeout_s: float = 300.0) -> tuple[str | None, str | None]:
    """Optional model-written summary via the Codex CLI. Returns (text, error)."""
    import subprocess
    prompt = NARRATIVE_PROMPT + json.dumps(report, ensure_ascii=False)[:60000]
    try:
        proc = subprocess.run(
            [codex_command, "exec", "--ephemeral", "--ignore-user-config", "--sandbox", "read-only",
             "--skip-git-repo-check", prompt],
            capture_output=True, text=True, timeout=timeout_s, stdin=subprocess.DEVNULL, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"codex unavailable: {exc}"[:200]
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    limited = "usage limit" in (err + out).lower()
    if proc.returncode != 0 or not out or limited:
        return None, "narrative skipped (usage limit)" if limited else f"narrative skipped (exit {proc.returncode})"
    return out[-4000:], None


def send_email(subject: str, body_md: str) -> str | None:
    """Send by SMTP when REVIEW_EMAIL_TO and REVIEW_SMTP_* are configured. Returns an error string or None."""
    import os
    import smtplib
    from email.message import EmailMessage
    to = os.getenv("REVIEW_EMAIL_TO", "").strip()
    host = os.getenv("REVIEW_SMTP_HOST", "smtp.gmail.com").strip()
    user = os.getenv("REVIEW_SMTP_USER", "").strip()
    pw = os.getenv("REVIEW_SMTP_PASSWORD", "").strip()
    port = int(os.getenv("REVIEW_SMTP_PORT", "587"))
    if not (to and user and pw):
        return "email not configured"
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to
    msg.set_content(body_md)
    try:
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            smtp.starttls()
            smtp.login(user, pw)
            smtp.send_message(msg)
    except (OSError, smtplib.SMTPException) as exc:
        return f"email failed: {exc}"[:200]
    return None


def _field(name: str, lines: list[str], limit: int = 1000) -> dict[str, Any]:
    text = ""
    for ln in lines:
        if len(text) + len(ln) + 1 > limit:
            text += "\n…"
            break
        text += ("\n" if text else "") + ln
    return {"name": name, "value": text or "无", "inline": False}


def _split_field(name: str, lines: list[str], limit: int = 1000) -> list[dict[str, Any]]:
    """Like _field but continues into extra fields instead of truncating."""
    out: list[dict[str, Any]] = []
    chunk: list[str] = []
    for ln in lines:
        if sum(len(x) + 1 for x in chunk) + len(ln) > limit and chunk:
            out.append({"name": name if not out else f"{name} (续)", "value": "\n".join(chunk), "inline": False})
            chunk = []
        chunk.append(ln[:limit])
    out.append({"name": name if not out else f"{name} (续)", "value": "\n".join(chunk) or "无", "inline": False})
    return out


def _w(text: str) -> int:
    import unicodedata
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _pad(text: str, width: int) -> str:
    text = str(text)
    while _w(text) > width and text:
        text = text[:-1]
    return text + " " * (width - _w(text))


def _table_fields(name: str, head: str, cols: list[str], rows: list[list[Any]], widths: list[int]) -> list[dict[str, Any]]:
    """A monospace table split across embed fields (each value <= 1024 chars).

    Discord's monospace font does not render CJK at exactly two ASCII cells,
    so the column legend lives in the head line instead of a header row, and
    callers keep alignment-sensitive columns ASCII (or fixed-length CJK).
    """
    legend = " · ".join(cols)
    head = (head + "\n" if head else "") + f"_{legend}_"
    lines = [" ".join(_pad(str(c), w) for c, w in zip(row, widths)).rstrip() for row in rows]
    out: list[dict[str, Any]] = []
    chunk: list[str] = []
    budget = 1000 - len(head) - 12
    for ln in lines:
        if sum(len(x) + 1 for x in chunk) + len(ln) > budget:
            out.append({"name": name if not out else f"{name} (续)",
                        "value": (head + "\n" if not out else "") + "```\n" + "\n".join(chunk) + "\n```",
                        "inline": False})
            chunk = []
        chunk.append(ln)
    body = ("```\n" + "\n".join(chunk) + "\n```") if chunk else ""
    out.append({"name": name if not out else f"{name} (续)",
                "value": ((head + "\n") if not out else "") + (body or "（无）"), "inline": False})
    return out


def _when(x: dict[str, Any], day: str) -> str:
    """HH:MM for posts made that day, MM-DD HH:MM for older ideas still in play."""
    ts = str(x.get("ts") or "")
    return x["time"] if ts[:10] == day else f"{ts[5:10]} {x['time']}"


def _level(x: dict[str, Any]) -> str:
    if x.get("trigger") is None:
        return "(img)" if x.get("attachments") else "-"
    return f"{'>' if x.get('operator') != 'below' else '<'}{x['trigger']:.2f}"


def _parse_label(x: dict[str, Any]) -> str:
    return {"auto_approved": "自动", "approved": "已批", "needs_review": "待审", "rejected": "拒绝"}.get(x.get("parse") or "", str(x.get("parse")))


def _outcome_label(x: dict[str, Any]) -> str:
    o = str(x.get("outcome") or "")
    if o.startswith("traded"):
        reason = {"eod": "收盘", "stop": "止损", "target": "目标"}.get(x.get("exit_reason"), x.get("exit_reason") or "")
        return f"已交易 {_sign(x.get('pnl_pct'))} {reason}".strip()
    if o == "watching_not_triggered":
        return "等待触发"
    if o == "no_watch_created":
        return "未建watch(旧watch挡住)"
    if o.startswith("not_approved"):
        return "待人工/读图" if x.get("attachments") else "待人工"
    if o == "unsupported_mapping":
        return "无法映射标的"
    if o.startswith("entry_failed"):
        return "下单失败"
    return {"pending_exit": "平仓中", "open": "持仓中", "pending_entry": "下单中", "unreconciled": "待对账"}.get(o, o)


def _sign(v: Any, money: bool = False) -> str:
    if not isinstance(v, (int, float)):
        return "-"
    sign = "+" if v >= 0 else "-"
    return f"{sign}{'$' if money else ''}{abs(v):.2f}{'' if money else '%'}"


def discord_embeds(r: dict[str, Any]) -> list[dict[str, Any]]:
    """Render the structured review as Discord embeds (Chinese, numbers first)."""
    d, h, o, sw, hl = r["day_trades"], r["heat"], r["option_shadow"], r["swing"], r["health"]
    s = d["summary"]
    pnl = s["pnl_usd"]
    color = 0x3BA55D if pnl > 0 else 0xED4245 if pnl < 0 else 0x5865F2
    fields: list[dict[str, Any]] = []

    # --- day trades: one line per trade ---
    trade_lines = [f"**{s['n']} 笔** · 盈亏 **{_sign(pnl, True)}** · 胜率 {s['win_rate'] if s['win_rate'] is not None else '-'}% · 平均 {_sign(s['avg_pct'])}"]
    exit_cn = {"eod": "收盘平仓", "stop": "止损", "target": "到目标", "manual": "手动"}
    src_cn = {"heat": "Heat", "discord": "主频道", "manual": "手动"}
    for t in d["closed"]:
        via = "" if (t["leverage"] or 1) == 1 else f" 经 {t['execution']} x{t['leverage']:.0f}"
        trade_lines.append(f"{'🟢' if (t['pnl_usd'] or 0) > 0 else '🔴'} **{t['ticker']}**{via} · {src_cn.get(t['source'], t['source'])} · {t['entered']}→{t['exit']} {exit_cn.get(t['exit_reason'], t['exit_reason'])} · **{_sign(t['pnl_usd'], True)}** ({_sign(t['pnl_pct'])})")
    for t in d["open"]:
        st_cn = {"pending_exit": "平仓中", "open": "持仓中", "unreconciled": "待对账"}.get(t["status"], t["status"])
        trade_lines.append(f"⏳ **{t['ticker']}** ({t['execution']}) {st_cn}" + (f" — {str(t['error'])[:70]}" if t["error"] else ""))
    for t in d["failed"]:
        trade_lines.append(f"❌ **{t['ticker']}** {t['status']} — {str(t['error'])[:70]}")
    if d["still_watching"]:
        trade_lines.append("👀 挂单等待: " + ", ".join(f"{w['ticker']}@{w['trigger']:.2f}" for w in d["still_watching"][:10]))
    fields.append(_field("📈 日内交易", trade_lines))

    # --- Heat: one entry per post, time-ordered: status line + his words ---
    heat_lines = [f"{h['count']} 条 · 自动批准 {len(h['approved'])} · 待审 {len(h['needs_review'])} · 期权帖 {len(h['option_posts'])}"]
    for x in r["heat_vs_bot"]:
        outcome = x["outcome"]
        icon = ("✅" if outcome.startswith("traded") else "⏳" if outcome == "watching_not_triggered"
                else "📝" if outcome.startswith("not_approved") else "🔵" if outcome in ("open", "pending_exit", "pending_entry")
                else "⚠️")
        lvl = "" if x.get("trigger") is None else f" {'站上' if x.get('operator') != 'below' else '跌破'} {x['trigger']:.2f}"
        heat_lines.append(f"{icon} **{_when(x, r['date'])} {x['ticker'] or '?'}**{lvl} · {_parse_label(x)} → {_outcome_label(x)}")
        if x.get("text"):
            heat_lines.append(f"> {x['text'][:80]}")
    fields += _split_field("🔥 Heat 信号 → bot 动作", heat_lines)

    dp = r["discord_plans_recorded"]
    if dp["count"]:
        fields.append(_field("📋 主频道信号（只记录）", [", ".join(f"{p['ticker']}@{p['trigger']}" for p in dp["rows"][:12])]))

    os_ = o["summary"]
    opt_lines = [f"开仓 {len(o['opened'])} · 平仓 {os_['n']} · 平均 {_sign(os_['avg_pct'])} · {_sign(os_['usd'], True)} · 持仓 {o['open_now']} / 盯盘 {o['watching_now']}"]
    for x in o["opened"]:
        opt_lines.append(f"🔵 {x['time']} **{x['ticker']}** {x['contract']} 开仓 @{x['price']}")
    exit_opt = {"eod": "收盘平仓", "stop_30pct": "止损 -30%", "stop_level": "破位止损", "trim_half_50pct": "减半", "trim_runner_100pct": "留 runner", "trim_runner_target": "到目标"}
    for x in o["closed"]:
        opt_lines.append(f"{'🟢' if x['realized_pct'] > 0 else '🔴'} {x['time']} **{x['ticker']}** {exit_opt.get(x['exit_reason'], x['exit_reason'])} · **{_sign(x['realized_pct'])}** (最高 {_sign(x['max_gain_pct'])})")
    fields.append(_field("🎯 期权影子（纸面）", opt_lines))

    sw_lines = [f"信号 {len(sw['signals'])} · 审核 " + (", ".join(f"{k} {v}" for k, v in sw["reviews_by_status"].items()) or "无") + f" · 成交 {sw['fills']}"]
    for p in sw["placed"]:
        sw_lines.append(f"• **{p['ticker']}** {p['kind']} ${p['usd']}")
    fields.append(_field("🌊 Swing", sw_lines))

    t = r.get("totals") or {}
    tot = []
    for k, label in (("day", "日内"), ("swing", "波段")):
        v = t.get(k) or {}
        if isinstance(v, dict) and v.get("count"):
            tot.append(f"**{label}** {v['count']} 笔 · 净 **{_sign(v.get('net'), True)}** · 胜率 {v['wins'] / v['count'] * 100:.0f}% · PF {v.get('profit_factor')}")
    combined = t.get("combined") if isinstance(t.get("combined"), dict) else {}
    if combined:
        tot.append(f"合计已实现 **{_sign(combined.get('combined_realized'), True)}**")
    fields.append(_field("Σ 累计", tot or ["-"]))

    health = []
    hb = hl["day_trader_heartbeat_age_min"]
    health.append(("✅" if isinstance(hb, (int, float)) and hb < 5 else "⚠️") + f" 日内服务心跳 {hb if hb is not None else '-'} 分钟前")
    if hl["unreconciled"]:
        health.append("⚠️ 未对账仓位: " + ", ".join(hl["unreconciled"]))
    if hl["stuck_pending"]:
        health.append("⚠️ 卡住的挂单: " + ", ".join(hl["stuck_pending"]))
    if r.get("narrative_error"):
        health.append(f"ℹ️ {r['narrative_error']}")
    fields.append(_field("🩺 健康", health))

    embed = {"title": f"📊 每日复盘 {r['date']}", "color": color, "fields": fields,
             "footer": {"text": "详细表格见附件 · dashboard → Review"}}
    if r.get("narrative"):
        embed["description"] = r["narrative"][:2000]
    return [embed]


def send_discord(subject: str, body_md: str, filename: str, report: dict[str, Any] | None = None) -> str | None:
    """Post the review to a Discord webhook (REVIEW_DISCORD_WEBHOOK): embed card plus the markdown as a file."""
    import os
    import httpx
    url = os.getenv("REVIEW_DISCORD_WEBHOOK", "").strip()
    if not url:
        return "discord not configured"
    if report:
        payload: dict[str, Any] = {"embeds": discord_embeds(report)}
    else:
        payload = {"content": f"**{subject}**"}
    try:
        r = httpx.post(url, data={"payload_json": json.dumps(payload, ensure_ascii=False)},
                       files={"file": (filename, body_md.encode("utf-8"), "text/markdown")}, timeout=30)
        if r.status_code >= 300:
            return f"discord failed: {r.status_code} {r.text[:160]}"
    except httpx.HTTPError as exc:
        return f"discord failed: {exc}"[:200]
    return None


def write(day: date, paths: Paths | None = None, *, with_narrative: bool = False, email: bool = False) -> Path:
    paths = paths or Paths()
    r = build(day, paths)
    if with_narrative:
        text, err = narrative(r)
        r["narrative"] = text
        r["narrative_error"] = err
    paths.out_dir.mkdir(parents=True, exist_ok=True)
    md = paths.out_dir / f"{day.isoformat()}.md"
    md.write_text(render_markdown(r), encoding="utf-8")
    if email:
        text = md.read_text(encoding="utf-8")
        r["email_error"] = send_email(f"Trade bot review {day.isoformat()}", text)
        r["discord_error"] = send_discord(f"Trade bot review {day.isoformat()}", text, md.name, report=r)
    (paths.out_dir / f"{day.isoformat()}.json").write_text(json.dumps(r, ensure_ascii=False, indent=1), encoding="utf-8")
    return md


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="Daily review")
    p.add_argument("--date", type=date.fromisoformat, default=datetime.now(ET).date())
    p.add_argument("--print", action="store_true")
    p.add_argument("--narrative", action="store_true", help="add a Codex-written summary (skipped on quota errors)")
    p.add_argument("--email", action="store_true", help="send by SMTP when REVIEW_EMAIL_TO / REVIEW_SMTP_* are set")
    args = p.parse_args(argv)
    from dotenv import load_dotenv
    load_dotenv()
    md = write(args.date, with_narrative=args.narrative, email=args.email)
    print(md)
    if args.email or args.narrative:
        meta = json.loads((md.with_suffix(".json")).read_text(encoding="utf-8"))
        print("narrative:", "ok" if meta.get("narrative") else meta.get("narrative_error", "skipped"))
        print("email:", meta.get("email_error") or "sent")
        print("discord:", meta.get("discord_error") or "sent")
    if args.print:
        print(md.read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
