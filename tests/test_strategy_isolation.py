"""Strategy isolation: one Robinhood account, two strategies, no shared shares.

Covers the 2026-09-02 SPXL collision end to end:

- ownership ledger (``bot.position_ownership``)
- swing sells sized to swing-owned shares (``robinhood_mcp_client``)
- ``BLOCKED`` review outcome and the stop monitor no longer placing orders
- executor ENTRY guard for symbols the day trader holds
- day-trader exit shortfall reconciliation, entry guard and trigger quarantine
- Heat parser ratio/indicator handling and in-place repair of old captures
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bot import day_trader, executor, position_ownership, robinhood_mcp_client, shadow_reviewer
from bot.day_trader import DayPosition, ET, _start_or_retry_exit, _submit_or_recover_entry, _sync_heat_ideas, run_once
from bot.executor import ExecutorConfig, VirtualBook, decide
from bot.heat_ideas import is_plausible_trigger, materialize_heat_ideas, parse_heat_idea
from bot.robinhood_mcp_client import OrderResult, OwnershipBlocked, RobinhoodMCPError
from bot.shadow_reviewer import ShadowConfig, review_one


NOT_ENOUGH = (
    "'place_equity_order' returned isError: API error 400: "
    '{"detail":"Not enough shares to sell."}'
)


def _write_jsonl(path: Path, rows: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


def _spxl_pnl_rows() -> list[dict]:
    """Two swing rounds in SPXL: Sep 1 fully closed, Sep 2 entry open."""
    return [
        {"timestamp": "2026-09-01T13:38:01+00:00", "ticker": "SPXL", "kind": "ENTRY", "action": "BUY",
         "order_id": "b1", "fill_price": 281.9499, "fill_qty": 0.017733},
        {"timestamp": "2026-09-01T16:30:55+00:00", "ticker": "SPXL", "kind": "STOP_TRIGGER", "action": "SELL",
         "order_id": "s1", "fill_price": 281.7301, "fill_qty": 0.017733, "realized_pnl": -0.0039},
        {"timestamp": "2026-09-02T14:19:24+00:00", "ticker": "SPXL", "kind": "ENTRY", "action": "BUY",
         "order_id": "b2", "fill_price": 284.8423, "fill_qty": 0.035107},
    ]


def _day_spxl_position(status: str = "pending_exit", **extra) -> dict:
    row = {
        "id": "day-spxl", "ticker": "SPY", "execution_ticker": "SPXL", "source": "heat",
        "status": status, "fill_price": 282.1399, "fill_qty": 0.070886,
        "exit_filled_qty": 0.0, "exit_filled_value": 0.0, "unreconciled_qty": 0.0,
        "entered_at": "2026-09-02T13:30:06+00:00",
    }
    row.update(extra)
    return row


@pytest.fixture
def ledgers(tmp_path, monkeypatch):
    pnl = _write_jsonl(tmp_path / "trade_pnl.jsonl", _spxl_pnl_rows())
    day = _write_jsonl(tmp_path / "day_trade_positions.jsonl", [_day_spxl_position()])
    book = tmp_path / "virtual_book.json"
    book.write_text(json.dumps({"positions": {}}), encoding="utf-8")
    monkeypatch.setattr(position_ownership, "SWING_PNL_PATH", pnl)
    monkeypatch.setattr(position_ownership, "DAY_POSITIONS_PATH", day)
    monkeypatch.setattr(position_ownership, "SWING_BOOK_PATH", book)
    return {"pnl": pnl, "day": day, "book": book}


# ---------------------------------------------------------------------------
# position_ownership
# ---------------------------------------------------------------------------

def test_day_owned_counts_only_shares_still_held(ledgers):
    _write_jsonl(ledgers["day"], [
        _day_spxl_position(),
        _day_spxl_position(id="closed", status="closed", exit_filled_qty=0.070886),
        _day_spxl_position(id="partial", status="open", fill_qty=0.1, exit_filled_qty=0.02,
                           unreconciled_qty=0.03),
        {"id": "pending", "ticker": "SPY", "execution_ticker": "SPXL", "status": "pending_entry",
         "entry_filled_qty": 0.005},
        {"id": "other", "ticker": "NVDA", "status": "open", "fill_qty": 1.0, "exit_filled_qty": 0.0},
    ])
    assert position_ownership.day_owned("SPXL") == pytest.approx(0.070886 + 0.05 + 0.005)
    assert position_ownership.day_owned("NVDA") == pytest.approx(1.0)
    assert position_ownership.day_owned("SPY") == 0.0
    assert position_ownership.day_holdings() == {"SPXL", "NVDA"}


def test_swing_owned_replays_fills_since_current_holding(ledgers):
    book_position = {"shares": 0.035168, "first_entry_at": "2026-09-02T14:19:19+00:00"}
    # Broker-confirmed fill, not the virtual estimate, and no Sep 1 residue.
    assert position_ownership.swing_owned("SPXL", book_position) == pytest.approx(0.035107)
    # No fill recorded for this holding -> virtual estimate.
    legacy = {"shares": 0.05, "first_entry_at": "2026-09-05T00:00:00+00:00"}
    assert position_ownership.swing_owned("SPXL", legacy) == pytest.approx(0.05)
    # No book position -> the swing strategy owns nothing, whatever the ledger says.
    assert position_ownership.swing_owned("SPXL", None) == 0.0
    # Fills say the Sep 1 holding was fully sold before Sep 2's entry: replaying
    # from Sep 1 counts both rounds, so only the open Sep 2 quantity remains.
    assert position_ownership.swing_owned_from_ledger(
        "SPXL", since="2026-09-01T13:00:00+00:00") == pytest.approx(0.035107)
    # A BUY without a recorded fill makes the ledger untrustworthy -> estimate.
    _write_jsonl(ledgers["pnl"], _spxl_pnl_rows() + [
        {"timestamp": "2026-09-02T15:00:00+00:00", "ticker": "SPXL", "kind": "ADD", "action": "BUY",
         "order_id": "b-nofill", "fill_price": None, "fill_qty": None},
    ])
    assert position_ownership.swing_owned("SPXL", book_position) == pytest.approx(0.035168)


def test_swing_sell_uses_zero_ownership_when_fills_show_holding_already_sold(ledgers):
    _write_jsonl(ledgers["pnl"], _spxl_pnl_rows() + [
        {"timestamp": "2026-09-02T16:00:00+00:00", "ticker": "SPXL", "kind": "REDUCE", "action": "SELL",
         "order_id": "s-all", "fill_price": 284.5, "fill_qty": 0.035107},
    ])
    # Broker still holds the day trader's 0.070886: nothing of ours -> BLOCKED.
    with pytest.raises(OwnershipBlocked):
        _place(_swing_sell_proposal("CLOSE"), broker_qty=0.070886)


def test_ownership_paths_follow_environment_after_dotenv(tmp_path, monkeypatch):
    monkeypatch.setattr(position_ownership, "DAY_POSITIONS_PATH", None)
    monkeypatch.setenv("DAY_TRADE_POSITIONS_PATH", str(tmp_path / "custom.jsonl"))
    _write_jsonl(tmp_path / "custom.jsonl", [_day_spxl_position(status="open")])
    assert position_ownership.day_owned("SPXL") == pytest.approx(0.070886)


def test_sellable_quantity_never_touches_the_other_strategy():
    sell = position_ownership.sellable_quantity
    # Broker holds both strategies' shares: sell exactly our own.
    assert sell(0.035107, own=0.035107, others=0.070886, actual=0.105993) == (pytest.approx(0.035107), None)
    # Requested less than owned (REDUCE).
    assert sell(0.01, own=0.035107, others=0.070886, actual=0.105993)[0] == pytest.approx(0.01)
    # Drift: broker only holds what the other strategy owns -> nothing for us.
    qty, note = sell(0.035107, own=0.035107, others=0.070886, actual=0.070886)
    assert qty == 0.0 and "drift" in note
    # Drift with some slack: sell only the slack.
    qty, note = sell(0.035107, own=0.035107, others=0.070886, actual=0.09)
    assert qty == pytest.approx(0.09 - 0.070886)
    assert sell(0.0, own=1.0, others=0.0, actual=1.0) == (0.0, None)


# ---------------------------------------------------------------------------
# robinhood_mcp_client.place_order
# ---------------------------------------------------------------------------

class _FakeSession:
    def __init__(self, broker_qty: float):
        self.broker_qty = broker_qty
        self.placed: list[dict] = []

    def call(self, tool, **kwargs):
        if tool == "get_accounts":
            return {"data": {"accounts": [{"account_number": "A1", "agentic_allowed": True}]}}
        if tool == "get_equity_tradability":
            return {"data": {"results": [{"tradeable": True, "fractional_tradability": "tradable"}]}}
        if tool == "get_equity_orders":
            if "order_id" in kwargs:
                return {"data": {"orders": [{
                    "id": kwargs["order_id"], "state": "filled",
                    "average_price": "284.4425", "cumulative_quantity": self.placed[-1]["quantity"],
                }]}}
            return {"data": {"orders": []}}
        if tool == "get_equity_positions":
            return {"data": {"positions": [{"symbol": "SPXL", "quantity": str(self.broker_qty)}]}}
        if tool == "place_equity_order":
            self.placed.append(kwargs)
            return {"data": {"order": {"id": "ord-1", "state": "confirmed"}}}
        raise AssertionError(f"unexpected tool {tool}")


def _swing_sell_proposal(kind: str = "STOP_TRIGGER", shares_estimate: float = 0.035168) -> dict:
    return {
        "id": "ord_20260902_172240_SPXL_STOP_TRIGGER_0e0031",
        "signal_kind": kind, "ticker": "SPXL", "action": "SELL",
        "usd_amount": 10.0, "shares_estimate": shares_estimate, "signal_price": 284.34,
        "book_before": {"open_count": 11, "total_deployed_usd": 130.0, "ticker_position": {
            "ticker": "SPXL", "shares": 0.035168, "deployed_usd": 10.0,
            "first_entry_at": "2026-09-02T14:19:19+00:00",
        }},
        "signal": {"message_id": 1544759209457885244, "price": 284.34},
    }


def _place(proposal, broker_qty):
    session = _FakeSession(broker_qty)
    with patch.object(robinhood_mcp_client, "_load_token", return_value="tok"), \
         patch.object(robinhood_mcp_client, "_MCPSession", return_value=session), \
         patch.object(robinhood_mcp_client.time, "sleep"):
        result = robinhood_mcp_client.place_order(proposal, 10.0)
    return session, result


def test_swing_stop_trigger_sells_only_swing_owned_shares(ledgers):
    # Account holds swing (0.035107) + day trader (0.070886).  Before the fix
    # the CLOSE sold all 0.105993.
    session, result = _place(_swing_sell_proposal(), broker_qty=0.105993)
    assert len(session.placed) == 1
    assert session.placed[0]["side"] == "sell"
    assert float(session.placed[0]["quantity"]) == pytest.approx(0.035107)
    assert result.fill_qty == pytest.approx(0.035107)


def test_swing_sell_blocked_when_only_day_trader_shares_remain(ledgers):
    with pytest.raises(OwnershipBlocked):
        _place(_swing_sell_proposal(), broker_qty=0.070886)


def test_swing_sell_blocked_when_broker_holds_nothing(ledgers):
    with pytest.raises(OwnershipBlocked):
        _place(_swing_sell_proposal(), broker_qty=0.0)


def test_swing_reduce_caps_at_estimate_then_ownership(ledgers):
    session, _ = _place(_swing_sell_proposal("REDUCE", shares_estimate=0.01), broker_qty=0.105993)
    assert float(session.placed[0]["quantity"]) == pytest.approx(0.01)
    session, _ = _place(_swing_sell_proposal("REDUCE", shares_estimate=0.5), broker_qty=0.105993)
    assert float(session.placed[0]["quantity"]) == pytest.approx(0.035107)


def test_swing_close_without_recorded_fill_uses_virtual_estimate(ledgers):
    proposal = _swing_sell_proposal("CLOSE")
    proposal["book_before"]["ticker_position"]["first_entry_at"] = "2026-09-05T00:00:00+00:00"
    # Broker holds enough for both ledgers: the virtual estimate is used as is.
    session, _ = _place(proposal, broker_qty=0.11)
    assert float(session.placed[0]["quantity"]) == pytest.approx(0.035168)
    # Broker holds slightly less than both ledgers claim: the estimate is
    # trimmed so the day trader's 0.070886 are never touched.
    session, _ = _place(proposal, broker_qty=0.105993)
    assert float(session.placed[0]["quantity"]) == pytest.approx(0.105993 - 0.070886)


# ---------------------------------------------------------------------------
# shadow_reviewer
# ---------------------------------------------------------------------------

def _shadow_config(tmp_path: Path, **overrides) -> ShadowConfig:
    values = dict(
        orders_path=tmp_path / "orders.jsonl", ledger_path=tmp_path / "ledger.jsonl",
        codex_command="codex", budget_per_ticker=20.0, max_age_s=300.0, poll_interval_s=1.0,
        codex_timeout_s=10.0, place_orders=True, pnl_path=tmp_path / "pnl.jsonl",
        swings_path=tmp_path / "swings.jsonl", book_path=tmp_path / "book.json",
    )
    values.update(overrides)
    return ShadowConfig(**values)


def test_review_one_records_blocked_when_ownership_refuses(tmp_path):
    proposal = _swing_sell_proposal()
    proposal["decided_at"] = datetime.now(timezone.utc).isoformat()
    with patch.object(
        shadow_reviewer.robinhood_mcp_client, "place_order",
        side_effect=OwnershipBlocked("nothing sellable"),
    ):
        record = review_one(proposal, _shadow_config(tmp_path), _append_pending=False)
    assert record.status == "BLOCKED"
    assert record.broker_order_id is None
    assert "nothing sellable" in record.rationale
    # BLOCKED is not a non-placement status for the executor: the CLOSE stays applied.
    assert "BLOCKED" not in executor._NON_PLACEMENT_REVIEW_STATUSES


def test_stop_monitor_emits_stop_trigger_without_placing_an_order(tmp_path):
    config = _shadow_config(tmp_path)
    config.book_path.write_text(json.dumps({"positions": {"NOK": {
        "ticker": "NOK", "shares": 0.93, "avg_price": 6.5, "deployed_usd": 6.0,
        "stop_loss": 6.2, "stop_loss_label": "$6.20",
    }}}), encoding="utf-8")
    session = MagicMock()
    session.call.side_effect = lambda tool, **kw: (
        {"data": {"results": [{"quote": {"last_trade_price": "6.10"}}]}}
        if tool == "get_equity_quotes" else (_ for _ in ()).throw(AssertionError(tool))
    )
    with patch.object(shadow_reviewer, "_is_market_open", return_value=True), \
         patch.object(shadow_reviewer.robinhood_mcp_client, "_load_token", return_value="tok"), \
         patch.object(shadow_reviewer.robinhood_mcp_client, "_MCPSession", return_value=session):
        shadow_reviewer._monitor_swing_stops(config)
        # Second check with the executor still down: no duplicate trigger.
        shadow_reviewer._monitor_swing_stops(config)
    tools = [c.args[0] for c in session.call.call_args_list]
    assert tools == ["get_equity_quotes", "get_equity_quotes"]
    rows = [json.loads(line) for line in config.swings_path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0]["kind"] == "STOP_TRIGGER" and rows[0]["ticker"] == "NOK"
    assert rows[0]["posted_by"] == "bot-stop-monitor"
    shadow_reviewer._emitted_stop_triggers.clear()


# ---------------------------------------------------------------------------
# executor ENTRY guard
# ---------------------------------------------------------------------------

def _executor_config(tmp_path: Path, **overrides) -> ExecutorConfig:
    values = dict(
        budget_per_ticker=20.0, max_open_tickers=5, mode="DRY_RUN",
        book_path=tmp_path / "book.json", orders_path=tmp_path / "orders.jsonl",
        poll_interval_s=1.0, swing_live_path=tmp_path / "swings.jsonl",
        swing_history_path=tmp_path / "swings_history.jsonl",
        notify_after=datetime.now(timezone.utc), replay_from=None,
        review_ledger_path=tmp_path / "reviews.jsonl",
        day_positions_path=tmp_path / "day.jsonl",
    )
    values.update(overrides)
    return ExecutorConfig(**values)


def _entry(ticker: str) -> dict:
    return {"kind": "ENTRY", "ticker": ticker, "side": "LONG", "price": 284.35,
            "position_size": "1/2", "position_fraction": 0.5,
            "received_at": "2026-09-02T14:19:19+00:00"}


def test_executor_rejects_entry_for_symbol_held_by_day_trader(tmp_path):
    config = _executor_config(tmp_path)
    _write_jsonl(config.day_positions_path, [_day_spxl_position(status="open")])
    holdings = executor._current_day_holdings(config)
    assert holdings == {"SPXL"}

    book = VirtualBook(mode="DRY_RUN", budget_per_ticker=20.0, max_open_tickers=5)
    blocked = decide(_entry("SPXL"), book, config, day_holdings=holdings)
    assert blocked.action == "REJECT"
    assert "day trader currently holds SPXL" in blocked.rationale
    assert "SPXL" not in book.positions

    allowed = decide(_entry("NVDA"), book, config, day_holdings=holdings)
    assert allowed.action == "BUY"
    # Replay never passes holdings, so history is judged as it happened.
    assert decide(_entry("SPXL"), book, config).action == "BUY"


def test_executor_replay_excludes_isolation_rejects_without_review_row(tmp_path):
    config = _executor_config(tmp_path)
    entry = dict(_entry("SPXL"), discord={"message_id": 777})
    _write_jsonl(config.swing_live_path, [entry])
    # The live executor rejected this ENTRY (day trader held SPXL) and logged it,
    # but the shadow reviewer never wrote a SKIPPED row.
    book = VirtualBook(mode="DRY_RUN", budget_per_ticker=20.0, max_open_tickers=5)
    decision = decide(entry, book, config, day_holdings={"SPXL"})
    assert decision.action == "REJECT"
    executor.append_decision(decision, config.orders_path)

    replayed = VirtualBook(mode="DRY_RUN", budget_per_ticker=20.0, max_open_tickers=5)
    executor.replay_history(config, replayed)
    assert "SPXL" not in replayed.positions


def test_executor_fails_closed_when_day_ledger_is_unreadable(tmp_path):
    config = _executor_config(tmp_path)
    with patch.object(position_ownership, "day_holdings", side_effect=OSError("disk")):
        holdings = executor._current_day_holdings(config)
    book = VirtualBook(mode="DRY_RUN", budget_per_ticker=20.0, max_open_tickers=5)
    assert decide(_entry("NVDA"), book, config, day_holdings=holdings).action == "REJECT"


def test_executor_isolation_can_be_disabled(tmp_path):
    config = _executor_config(tmp_path, block_shared_tickers=False)
    _write_jsonl(config.day_positions_path, [_day_spxl_position(status="open")])
    assert executor._current_day_holdings(config) is None


# ---------------------------------------------------------------------------
# day_trader: exit shortfall reconciliation
# ---------------------------------------------------------------------------

def _open_spxl() -> DayPosition:
    return DayPosition(
        id="day-spxl", ticker="SPY", execution_ticker="SPXL", source="heat", status="open",
        fill_price=282.1399, fill_qty=0.070886, stop_price=757.7856,
        plan_received_at="2026-09-01T19:20:07+00:00", entered_at="2026-09-02T13:30:06+00:00",
    )


def _session_with_broker_qty(qty: float) -> MagicMock:
    session = MagicMock()
    session.call.return_value = {"data": {"positions": [{"symbol": "SPXL", "quantity": str(qty)}]}}
    return session


def test_exit_shortfall_with_no_shares_ends_unreconciled_without_pnl(ledgers):
    pos = _open_spxl()
    session = _session_with_broker_qty(0.0)
    with patch.object(day_trader, "_market_sell_all", side_effect=RobinhoodMCPError(NOT_ENOUGH)) as sell:
        assert _start_or_retry_exit(session, "acct", pos, "eod") is True
    assert sell.call_count == 1
    assert pos.status == "unreconciled"
    assert pos.realized_pnl is None
    assert pos.exit_order_id is None
    assert pos.exit_last_error is None
    assert pos.unreconciled_qty == pytest.approx(0.070886)
    assert "could not sell 0.070886" in (pos.reconciliation_note or "")
    assert pos.closed_at is not None
    # The lifecycle no longer owns shares.
    assert position_ownership.day_position_owned_qty(pos.to_dict()) == 0.0


def test_shortfall_resell_failure_keeps_position_pending_for_retry(ledgers):
    pos = _open_spxl()
    session = _session_with_broker_qty(0.03)
    calls: list[float] = []

    def fake_sell(_s, _a, symbol, qty, _ref):
        calls.append(qty)
        if len(calls) == 1:
            raise RobinhoodMCPError(NOT_ENOUGH)
        raise RuntimeError("temporary broker error")

    with patch.object(day_trader, "_market_sell_all", side_effect=fake_sell):
        _start_or_retry_exit(session, "acct", pos, "stop")
    assert pos.status == "pending_exit"
    assert pos.unreconciled_qty == pytest.approx(0.070886 - 0.03)
    assert "temporary broker error" in (pos.exit_last_error or "")
    # Next poll retries only the sellable remainder, not the lost shares.
    with patch.object(day_trader, "_market_sell_all",
                      return_value=OrderResult("sell-3", "filled", 284.0, 0.03, 8.52)) as sell:
        _start_or_retry_exit(session, "acct", pos, "stop")
    assert sell.call_args.args[3] == pytest.approx(0.03)
    assert pos.status == "closed"


def test_exit_shortfall_sells_only_the_part_that_cannot_be_the_swings(ledgers):
    pos = _open_spxl()
    session = _session_with_broker_qty(0.03)  # swing owns nothing (empty book)
    calls: list[float] = []

    def fake_sell(_s, _a, symbol, qty, _ref):
        calls.append(qty)
        if len(calls) == 1:
            raise RobinhoodMCPError(NOT_ENOUGH)
        return OrderResult("sell-2", "filled", 284.0, qty, 284.0 * qty)

    with patch.object(day_trader, "_market_sell_all", side_effect=fake_sell):
        _start_or_retry_exit(session, "acct", pos, "stop")
    assert calls == [pytest.approx(0.070886), pytest.approx(0.03)]
    assert pos.status == "closed"
    assert pos.exit_filled_qty == pytest.approx(0.03)
    assert pos.unreconciled_qty == pytest.approx(0.070886 - 0.03)
    assert pos.realized_pnl == pytest.approx((284.0 - 282.1399) * 0.03, abs=1e-4)


def test_exit_shortfall_respects_swing_ownership_of_remaining_shares(ledgers):
    # Broker holds 0.05 but the swing book says those are the swing's shares.
    ledgers["book"].write_text(json.dumps({"positions": {"SPXL": {
        "ticker": "SPXL", "shares": 0.05, "first_entry_at": "2026-09-05T00:00:00+00:00",
    }}}), encoding="utf-8")
    pos = _open_spxl()
    session = _session_with_broker_qty(0.05)
    with patch.object(day_trader, "_market_sell_all", side_effect=RobinhoodMCPError(NOT_ENOUGH)) as sell:
        _start_or_retry_exit(session, "acct", pos, "eod")
    assert sell.call_count == 1
    assert pos.status == "unreconciled"
    assert "swing strategy owns 0.050000" in (pos.reconciliation_note or "")


def test_stale_shortfall_error_is_reconciled_instead_of_resubmitted(ledgers):
    pos = _open_spxl()
    pos.status = "pending_exit"
    pos.exit_reason = "eod"
    pos.exit_last_error = NOT_ENOUGH
    session = _session_with_broker_qty(0.0)
    with patch.object(day_trader, "_market_sell_all") as sell:
        _start_or_retry_exit(session, "acct", pos, "eod")
    sell.assert_not_called()
    assert pos.status == "unreconciled"


def test_unreconciled_positions_are_ignored_by_the_poll_loop(ledgers):
    pos = _open_spxl()
    pos.status = "unreconciled"
    with patch.object(day_trader, "_load_new_plans", return_value=[]), \
         patch.object(day_trader, "load_plans", return_value=[]), \
         patch.object(day_trader, "_MCPSession") as session, \
         patch.object(day_trader, "_load_token", return_value="tok"), \
         patch.object(day_trader, "_flush_positions"), \
         patch.object(day_trader, "datetime") as m_dt:
        m_dt.now.return_value = datetime(2026, 9, 3, 10, 0, tzinfo=ET)
        m_dt.fromisoformat.side_effect = datetime.fromisoformat
        run_once([pos], set())
    session.assert_not_called()


# ---------------------------------------------------------------------------
# day_trader: entry guard
# ---------------------------------------------------------------------------

def test_day_trade_entry_waits_while_swing_holds_the_symbol(ledgers):
    ledgers["book"].write_text(json.dumps({"positions": {"SPXL": {
        "ticker": "SPXL", "shares": 0.035, "first_entry_at": "2026-09-02T14:19:19+00:00",
    }}}), encoding="utf-8")
    pos = DayPosition(
        ticker="SPXL", source="manual", manual_plan_id="plan-1", status="watching",
        trigger_price=284.0, entry_limit_price=284.57, good_til_cancelled=True, armed=True,
        plan_received_at="2026-09-02T13:00:00+00:00",
    )
    with patch.object(day_trader, "_validate_entry_preflight", return_value=(284.1, 284.0, 284.2, 0.05)), \
         patch.object(day_trader, "_place_fractional_market_buy") as buy, \
         patch.object(day_trader, "_append_position"):
        assert _submit_or_recover_entry(MagicMock(), "acct", pos) is True
    buy.assert_not_called()
    assert pos.status == "watching"
    assert pos.armed is False
    assert pos.exit_reason == "waiting_rearm_after_shared_ticker_with_swing"
    assert pos.buy_order_id is None


def test_day_trade_entry_proceeds_when_symbol_is_free(ledgers):
    pos = DayPosition(
        ticker="SPXL", source="manual", manual_plan_id="plan-1", status="watching",
        trigger_price=284.0, entry_limit_price=284.57, good_til_cancelled=True, armed=True,
        plan_received_at="2026-09-02T13:00:00+00:00",
    )
    result = OrderResult("buy-1", "filled", 284.2, 0.0704, 20.0)
    with patch.object(day_trader, "_validate_entry_preflight", return_value=(284.1, 284.0, 284.2, 0.05)), \
         patch.object(day_trader, "_place_fractional_market_buy", return_value=result) as buy, \
         patch.object(day_trader, "_place_stop_order", return_value=None), \
         patch.object(day_trader, "_append_position"):
        _submit_or_recover_entry(MagicMock(), "acct", pos)
    buy.assert_called_once()
    assert pos.status == "open"


# ---------------------------------------------------------------------------
# day_trader: trigger quarantine + Heat sync
# ---------------------------------------------------------------------------

def _run_watch(pos: DayPosition, price: float) -> None:
    plans = [{
        "id": pos.manual_plan_id, "ticker": pos.ticker, "trigger_price": pos.trigger_price,
        "target_price": None, "setup": None, "status": "active",
        "created_at": pos.plan_received_at,
    }]
    with patch.object(day_trader, "_load_new_plans", return_value=[]), \
         patch.object(day_trader, "load_plans", return_value=plans), \
         patch.object(day_trader, "_load_token", return_value="tok"), \
         patch.object(day_trader, "_MCPSession", return_value=MagicMock()), \
         patch.object(day_trader, "_get_agentic_account", return_value="acct"), \
         patch.object(day_trader, "_get_prices", return_value={pos.ticker: price}), \
         patch.object(day_trader, "_place_fractional_market_buy") as buy, \
         patch.object(day_trader, "_flush_positions"), \
         patch.object(day_trader, "_append_position"), \
         patch.object(day_trader, "datetime") as m_dt:
        m_dt.now.return_value = datetime(2026, 9, 9, 12, 0, tzinfo=ET)
        m_dt.fromisoformat.side_effect = datetime.fromisoformat
        run_once([pos], set())
        buy.assert_not_called()


def test_watch_with_ratio_trigger_is_quarantined_not_armed(ledgers, tmp_path, monkeypatch):
    monkeypatch.setattr(day_trader, "HEAT_IDEAS_PATH", tmp_path / "none.jsonl")
    monkeypatch.setattr(day_trader, "HEAT_DECISIONS_PATH", tmp_path / "none-d.jsonl")
    monkeypatch.setattr(day_trader, "HEAT_SETTINGS_PATH", tmp_path / "none-s.json")
    pos = DayPosition(
        ticker="ARM", source="manual", manual_plan_id="arm-1", status="watching",
        trigger_price=1.414, good_til_cancelled=True, armed=False,
        plan_received_at="2026-09-09T15:50:49+00:00",
    )
    _run_watch(pos, price=265.0)
    assert pos.status == "expired"
    assert pos.exit_reason == "implausible_trigger"
    assert "1.414" in (pos.quarantine_reason or "")
    # A legitimate distant level (20% away) is still accepted.
    ok = DayPosition(
        ticker="ARM", source="manual", manual_plan_id="arm-2", status="watching",
        trigger_price=318.0, good_til_cancelled=True, armed=False,
        plan_received_at="2026-09-09T15:50:49+00:00",
    )
    _run_watch(ok, price=265.0)
    assert ok.status == "watching" and ok.armed is True


def test_heat_sync_keeps_quarantined_watch_parked_until_level_is_corrected(tmp_path, monkeypatch):
    ideas = _write_jsonl(tmp_path / "heat.jsonl", [{
        "event_type": "idea", "id": "arm-1", "ticker": "ARM", "trigger_price": 1.414,
        "direction": "long", "trigger_operator": "above", "auto_eligible": True,
        "created_at": "2026-09-09T15:50:49+00:00",
    }])
    decisions = tmp_path / "decisions.jsonl"
    settings = tmp_path / "settings.json"
    settings.write_text('{"auto_trading_enabled": true}', encoding="utf-8")
    monkeypatch.setattr(day_trader, "HEAT_IDEAS_PATH", ideas)
    monkeypatch.setattr(day_trader, "HEAT_DECISIONS_PATH", decisions)
    monkeypatch.setattr(day_trader, "HEAT_SETTINGS_PATH", settings)
    pos = DayPosition(
        ticker="ARM", source="heat", heat_idea_id="arm-1", status="expired",
        exit_reason="implausible_trigger", quarantine_reason="trigger 1.414 vs 265",
        trigger_price=1.414, good_til_cancelled=True,
    )
    _sync_heat_ideas([pos], now=datetime(2026, 9, 10, 10, 0, tzinfo=ET))
    assert pos.status == "expired"

    _write_jsonl(decisions, [{
        "idea_id": "arm-1", "decision": "approved", "ticker": "ARM", "trigger_price": 270.0,
        "direction": "long", "trigger_operator": "above", "good_til_cancelled": True,
        "decided_at": "2026-09-10T14:00:00+00:00",
    }])
    assert _sync_heat_ideas([pos], now=datetime(2026, 9, 10, 10, 5, tzinfo=ET)) is True
    assert pos.status == "watching"
    assert pos.trigger_price == 270.0
    assert pos.quarantine_reason is None
    assert pos.armed is False


def test_heat_sync_expires_watch_whose_idea_now_needs_review(tmp_path, monkeypatch):
    ideas = _write_jsonl(tmp_path / "heat.jsonl", [{
        "event_type": "idea", "id": "arm-1", "ticker": "ARM", "trigger_price": 1.414,
        "text": "ARM 需要站上 fib 1.414，目前再次受阻", "direction": "long",
        "trigger_operator": "above", "auto_eligible": True,
        "created_at": "2026-09-09T15:50:49+00:00",
    }])
    settings = tmp_path / "settings.json"
    settings.write_text('{"auto_trading_enabled": true}', encoding="utf-8")
    monkeypatch.setattr(day_trader, "HEAT_IDEAS_PATH", ideas)
    monkeypatch.setattr(day_trader, "HEAT_DECISIONS_PATH", tmp_path / "decisions.jsonl")
    monkeypatch.setattr(day_trader, "HEAT_SETTINGS_PATH", settings)
    pos = DayPosition(
        ticker="ARM", source="heat", heat_idea_id="arm-1", status="watching",
        trigger_price=1.414, good_til_cancelled=True, armed=False,
    )
    assert _sync_heat_ideas([pos], now=datetime(2026, 9, 10, 10, 0, tzinfo=ET)) is True
    assert pos.status == "expired"
    assert pos.exit_reason == "heat_needs_review"


# ---------------------------------------------------------------------------
# heat_ideas parser
# ---------------------------------------------------------------------------

def test_heat_parser_does_not_turn_fibonacci_ratio_into_price():
    idea = parse_heat_idea(
        "ARM 需要站上 fib 1.414，目前再次受阻",
        idea_id="1547273079888289882",
        created_at="2026-09-09T15:50:49+00:00",
    )
    assert idea is not None
    assert idea["ticker"] == "ARM"
    assert idea["trigger_price"] is None
    assert idea["classification"] == "needs_level"
    assert idea["auto_eligible"] is False


@pytest.mark.parametrize("text", [
    "SPY 站上 fibonacci 0.618 就加仓",
    "NVDA 突破 5% 以上再看",
    "TSLA 站上 21 EMA 才考虑",
    "AMD 站上 200日线",
    "PLTR 站上 1.618 倍",
])
def test_heat_parser_ignores_ratio_percentage_and_indicator_numbers(text):
    idea = parse_heat_idea(text, idea_id="x", created_at="2026-09-09T15:50:49+00:00")
    assert idea is not None
    assert idea["trigger_price"] is None
    assert idea["auto_eligible"] is False


def test_heat_parser_still_reads_explicit_dollar_levels():
    idea = parse_heat_idea(
        "ARM 站上 fib 1.414 也就是 270 之后再看，目前站上 268.5 就可以买入",
        idea_id="x", created_at="2026-09-09T15:50:49+00:00",
    )
    assert idea is not None
    assert idea["trigger_price"] == 268.5


def test_materializer_repairs_legacy_ratio_capture_in_place():
    ideas = materialize_heat_ideas([{
        "event_type": "idea", "id": "arm-1", "ticker": "ARM", "trigger_price": 1.414,
        "text": "ARM 需要站上 fib 1.414，目前再次受阻", "direction": "long",
        "trigger_operator": "above", "auto_eligible": True, "classification": "actionable_setup",
        "created_at": "2026-09-09T15:50:49+00:00",
    }])
    assert ideas[0]["trigger_price"] is None
    assert ideas[0]["auto_eligible"] is False
    assert ideas[0]["decision"] is None
    assert ideas[0]["status"] == "needs_review"
    assert ideas[0]["classification"] == "needs_level"


def test_materializer_keeps_operator_decision_over_reparse():
    ideas = materialize_heat_ideas(
        [{
            "event_type": "idea", "id": "arm-1", "ticker": "ARM", "trigger_price": 1.414,
            "text": "ARM 需要站上 fib 1.414，目前再次受阻", "direction": "long",
            "auto_eligible": True, "created_at": "2026-09-09T15:50:49+00:00",
        }],
        [{"idea_id": "arm-1", "decision": "approved", "ticker": "ARM", "trigger_price": 270.0,
          "decided_at": "2026-09-10T14:00:00+00:00"}],
    )
    assert ideas[0]["trigger_price"] == 270.0
    assert ideas[0]["decision"] == "approved"


def test_is_plausible_trigger():
    assert is_plausible_trigger(1.414, 265.0) is False
    assert is_plausible_trigger(318.0, 265.0) is True
    assert is_plausible_trigger(600.0, 265.0) is False
    assert is_plausible_trigger(140.0, 265.0) is True
    assert is_plausible_trigger(100.0, 265.0) is False
    assert is_plausible_trigger(100.0, 265.0, max_ratio=3.0) is True
    assert is_plausible_trigger(270.0, None) is True
    assert is_plausible_trigger(None, 265.0) is False
