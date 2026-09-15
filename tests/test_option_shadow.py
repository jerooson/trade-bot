"""Paper option shadow: contract selection and Heat's mechanical rules."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from bot import option_shadow as osh
from bot.option_shadow import ET, Shadow, manage_open, nearest_contract, nearest_expiration, run_once


class FakeSession:
    """Scripted Robinhood MCP: underlying price, option bid/ask, chain, instruments."""

    def __init__(self, price=100.0, bid=1.0, ask=1.1):
        self.price, self.bid, self.ask = price, bid, ask
        self.calls = []

    def call(self, tool, **kw):
        self.calls.append((tool, kw))
        if tool == "get_equity_quotes":
            return {"data": {"results": [{"quote": {"symbol": s, "last_trade_price": str(self.price)}} for s in kw["symbols"]]}}
        if tool == "get_option_chains":
            return {"data": {"chains": [{"symbol": kw["underlying_symbol"], "expiration_dates": ["2026-09-11", "2026-09-14", "2026-09-18"]}]}}
        if tool == "get_option_instruments":
            page = kw.get("cursor")
            if page is None:
                return {"data": {"instruments": [
                    {"id": "k95", "strike_price": "95.0000", "tradability": "tradable"},
                    {"id": "k105", "strike_price": "105.0000", "tradability": "tradable"},
                ], "next": "https://x/?cursor=p2"}}
            return {"data": {"instruments": [
                {"id": "k101", "strike_price": "101.0000", "tradability": "tradable"},
                {"id": "k100u", "strike_price": "100.0000", "tradability": "untradable"},
            ], "next": None}}
        if tool == "get_option_quotes":
            return {"data": {"results": [{"quote": {"instrument_id": kw["instrument_ids"][0], "bid_price": str(self.bid), "ask_price": str(self.ask)}}]}}
        raise AssertionError(tool)


@pytest.fixture(autouse=True)
def _paths(tmp_path, monkeypatch):
    monkeypatch.setattr(osh, "LEDGER_PATH", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(osh, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(osh, "CONTRACTS", 10)


def _now(h=10, m=0, day=14):
    return datetime(2026, 9, day, h, m, tzinfo=ET)


def _idea(**over):
    base = {"id": "i1", "ticker": "XYZ", "trigger_price": 100.0, "direction": "long", "decision": "approved",
            "trigger_operator": "above", "target_price": 104.0, "created_at": "2026-09-14T13:00:00+00:00"}
    base.update(over)
    return base


def test_nearest_expiration_and_contract_skip_untradable_and_paginate():
    s = FakeSession()
    assert nearest_expiration(s, "XYZ", _now().date()) == "2026-09-14"
    c = nearest_contract(s, "XYZ", "2026-09-14", "call", 100.4)
    assert c.instrument_id == "k101" and c.strike == 101.0


def test_watch_opens_on_cross_at_ask():
    s = FakeSession(price=99.0)
    shadows = {}
    run_once(s, shadows, _now(), ideas=[_idea()])
    assert shadows["i1"].status == "watching"
    s.price = 100.2
    run_once(s, shadows, _now(10, 1), ideas=[_idea()])
    sh = shadows["i1"]
    assert sh.status == "open" and sh.entry_price == 1.1 and sh.qty_open == 10
    assert sh.contract["kind"] == "call"


def test_short_idea_buys_put():
    s = FakeSession(price=99.0)
    shadows = {}
    run_once(s, shadows, _now(), ideas=[_idea(direction="short", trigger_operator="below")])
    assert shadows["i1"].status == "open" and shadows["i1"].contract["kind"] == "put"


def _open_shadow(qty=10, entry=1.0):
    sh = Shadow(idea_id="i1", ticker="XYZ", direction="long", trigger=100.0, operator="above", target=104.0,
                status="open", contract={"instrument_id": "k101"}, entry_ts=_now().isoformat(),
                entry_price=entry, qty=qty, qty_open=qty)
    return sh


def test_trim_half_at_50_then_runner_at_100():
    sh = _open_shadow()
    manage_open(sh, 101.0, bid=1.5, now=_now(10, 5))
    assert sh.qty_open == 5 and sh.trimmed_half and not sh.trimmed_runner
    manage_open(sh, 102.0, bid=2.0, now=_now(10, 10))
    assert sh.qty_open == 2 and sh.trimmed_runner
    assert sh.realized_usd == pytest.approx(5 * 0.5 * 100 + 3 * 1.0 * 100)


def test_target_reached_goes_straight_to_runner():
    sh = _open_shadow()
    manage_open(sh, 104.0, bid=1.3, now=_now(10, 5))
    assert sh.qty_open == 2 and sh.fills[-1]["reason"] == "trim_runner_target"


def test_stop_30pct_closes_all():
    sh = _open_shadow()
    manage_open(sh, 100.5, bid=0.69, now=_now(10, 5))
    assert sh.status == "closed" and sh.exit_reason == "stop_30pct" and sh.qty_open == 0


def test_level_stop_needs_five_minutes_adverse():
    sh = _open_shadow()
    manage_open(sh, 99.9, bid=0.9, now=_now(10, 5))
    assert sh.status == "open" and sh.adverse_since
    manage_open(sh, 100.1, bid=0.9, now=_now(10, 7))      # back above: timer resets
    assert sh.adverse_since is None
    manage_open(sh, 99.9, bid=0.9, now=_now(10, 8))
    manage_open(sh, 99.8, bid=0.85, now=_now(10, 13))
    assert sh.status == "closed" and sh.exit_reason == "stop_level"


def test_eod_flattens_and_ledger_has_closed_row():
    sh = _open_shadow()
    manage_open(sh, 101.0, bid=1.2, now=_now(15, 50))
    assert sh.status == "closed" and sh.exit_reason == "eod"
    rows = [__import__("json").loads(l) for l in osh.LEDGER_PATH.read_text(encoding="utf-8").splitlines()]
    closed = [r for r in rows if r["event"] == "closed"][0]
    assert closed["realized_pct"] == pytest.approx(20.0)


def test_stale_and_unapproved_ideas_are_not_watched():
    s = FakeSession(price=99.0)
    shadows = {}
    old = _idea(id="old", created_at="2026-08-01T13:00:00+00:00")
    pending = _idea(id="p", decision=None)
    run_once(s, shadows, _now(), ideas=[_idea(), old, pending])
    assert set(shadows) == {"i1"}
    # idea withdrawn -> watch expires and is dropped from state
    run_once(s, shadows, _now(10, 1), ideas=[])
    assert shadows == {}


def test_no_entries_outside_regular_hours():
    s = FakeSession(price=101.0)
    shadows = {}
    run_once(s, shadows, _now(8, 0), ideas=[_idea()])
    assert shadows["i1"].status == "watching"
    assert not any(t == "get_equity_quotes" for t, _ in s.calls)


def test_implausible_trigger_never_opens_a_shadow():
    s = FakeSession(price=57.8)
    shadows = {}
    run_once(s, shadows, _now(), ideas=[_idea(trigger_price=1.618)])   # mis-parsed fib ratio
    assert shadows == {}
    rows = [__import__("json").loads(l) for l in osh.LEDGER_PATH.read_text(encoding="utf-8").splitlines()]
    assert any(r["event"] == "expired" and "implausible" in r.get("reason", "") for r in rows)


def test_idea_is_shadowed_at_most_once():
    s = FakeSession(price=100.2)
    shadows = {}
    run_once(s, shadows, _now(), ideas=[_idea()])
    assert shadows["i1"].status == "open"
    shadows["i1"].status = "closed"
    run_once(s, shadows, _now(10, 1), ideas=[_idea()])       # closed shadow dropped, idea still approved
    assert "i1" not in shadows
    run_once(s, shadows, _now(10, 2), ideas=[_idea()])
    assert "i1" not in shadows                                # ledger says it already opened once


def test_pagination_cursor_is_url_decoded():
    class S(FakeSession):
        def call(self, tool, **kw):
            if tool == "get_option_instruments" and kw.get("cursor") is None:
                return {"data": {"instruments": [{"id": "k95", "strike_price": "95.0000", "tradability": "tradable"}],
                                 "next": "http://x/options/instruments/?chain_symbol=XYZ&cursor=cD02OTMuMDAwMA%3D%3D&type=call"}}
            if tool == "get_option_instruments":
                assert kw["cursor"] == "cD02OTMuMDAwMA=="
                return {"data": {"instruments": [{"id": "k101", "strike_price": "101.0000", "tradability": "tradable"}], "next": None}}
            return super().call(tool, **kw)
    c = nearest_contract(S(), "XYZ", "2026-09-14", "call", 100.4)
    assert c.instrument_id == "k101"


def test_dip_buy_below_level_has_no_level_stop():
    sh = _open_shadow()
    sh.operator, sh.trigger = "below", 706.0          # long entered on a dip below 706
    manage_open(sh, 705.0, bid=1.0, now=_now(10, 5))  # still below the level
    manage_open(sh, 704.0, bid=1.0, now=_now(10, 12)) # 7 minutes later, still below
    assert sh.status == "open" and sh.adverse_since is None
