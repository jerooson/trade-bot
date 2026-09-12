"""Broker adapters: Robinhood MCP mapping, IBKR mapping, factory, swing placement."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from bot import swing_orders
from bot.broker import CLIENT_ID_OFFSETS, create_broker, ibkr_client_id
from bot.broker.base import (
    BrokerError,
    OrderRejected,
    OrderRequest,
    OrderResult,
    Quote,
    ShareShortfall,
)
from bot.broker.robinhood import RobinhoodBroker
from bot.robinhood_mcp_client import RobinhoodMCPError
from tests.fake_broker import FakeBroker


# ---------------------------------------------------------------------------
# Robinhood adapter
# ---------------------------------------------------------------------------

class _Session:
    def __init__(self, responses: dict[str, object]):
        self.responses = responses
        self.calls: list[tuple[str, dict]] = []

    def call(self, tool, **kwargs):
        self.calls.append((tool, kwargs))
        value = self.responses[tool]
        if isinstance(value, Exception):
            raise value
        return value


def test_robinhood_quotes_keep_request_order_fallback_and_volume_fields():
    session = _Session({"get_equity_quotes": {"data": {"results": [
        {"quote": {"last_trade_price": "317.30", "bid_price": "317.20", "ask_price": "317.40",
                   "average_volume_30_days": "5000000"}},
        {"symbol": "NVDA", "quote": {"last_trade_price": "190.50", "volume": "1000"}},
    ]}}})
    quotes = RobinhoodBroker(session, "A1").quotes(["aapl", "NVDA", "AAPL"])
    assert quotes["AAPL"] == Quote("AAPL", 317.30, 317.20, 317.40, None, 5_000_000.0)
    assert quotes["NVDA"].last == 190.50 and quotes["NVDA"].volume == 1000.0
    assert session.calls == [("get_equity_quotes", {"symbols": ["AAPL", "NVDA"]})]


def test_robinhood_account_prefers_agentic_and_caches():
    session = _Session({"get_accounts": {"data": {"accounts": [
        {"account_number": "X", "agentic_allowed": False},
        {"account_number": "AG", "agentic_allowed": True},
    ]}}})
    broker = RobinhoodBroker(session)
    assert broker.account_id() == "AG"
    assert broker.account_id() == "AG"
    assert len(session.calls) == 1


def test_robinhood_place_order_maps_request_and_classifies_errors():
    session = _Session({"place_equity_order": {"data": {"order": {"id": "o1", "state": "queued"}}}})
    broker = RobinhoodBroker(session, "A1")
    result = broker.place_order(OrderRequest("spxl", "buy", "market", dollar_amount=20, ref_id="r1"))
    assert result == OrderResult("o1", "queued", None, None, None, "SPXL", None)
    kwargs = session.calls[0][1]
    assert kwargs["dollar_amount"] == "20.00" and kwargs["market_hours"] == "regular_hours"
    assert kwargs["ref_id"] == "r1" and "quantity" not in kwargs

    broker.place_order(OrderRequest("SPXL", "sell", "stop", quantity=0.5, stop_price=10.123))
    kwargs = session.calls[1][1]
    assert kwargs["type"] == "stop_market" and kwargs["quantity"] == "0.500000"
    assert kwargs["stop_price"] == "10.12" and "market_hours" not in kwargs

    session.responses["place_equity_order"] = RobinhoodMCPError(
        "'place_equity_order' returned isError: API error 400: {\"detail\":\"Not enough shares to sell.\"}"
    )
    with pytest.raises(ShareShortfall):
        broker.place_order(OrderRequest("SPXL", "sell", "market", quantity=1))
    session.responses["place_equity_order"] = RobinhoodMCPError(
        "'place_equity_order' returned isError: buying power unavailable"
    )
    with pytest.raises(OrderRejected):
        broker.place_order(OrderRequest("SPXL", "buy", "market", dollar_amount=20))
    session.responses["place_equity_order"] = RobinhoodMCPError("HTTP 502 from MCP server")
    with pytest.raises(BrokerError) as raised:
        broker.place_order(OrderRequest("SPXL", "buy", "market", dollar_amount=20))
    assert not isinstance(raised.value, OrderRejected)


def test_robinhood_positions_orders_and_tradability():
    session = _Session({
        "get_equity_positions": {"data": {"positions": [{"symbol": "SPXL", "quantity": "0.1", "average_buy_price": "280"}]}},
        "get_equity_orders": {"data": {"orders": [
            {"id": "o1", "state": "confirmed", "symbol": "SPXL"},
            {"id": "o2", "state": "filled", "average_price": "281", "cumulative_quantity": "0.1"},
        ]}},
        "get_equity_tradability": {"data": {"results": [
            {"symbol": "SPXL", "tradeable": True, "fractional_tradability": "untradable"},
        ]}},
    })
    broker = RobinhoodBroker(session, "A1")
    assert broker.position_qty("spxl") == 0.1
    assert [o.order_id for o in broker.open_orders("SPXL")] == ["o1"]
    filled = broker.get_order("o2", "SPXL")
    assert filled.state == "filled" and filled.fill_usd == pytest.approx(28.1)
    assert broker.get_order("missing", "SPXL") is None
    assert broker.tradability(["SPXL"])["SPXL"].fractional is False


# ---------------------------------------------------------------------------
# IBKR adapter (ib_async objects, fake gateway)
# ---------------------------------------------------------------------------

ib_async = pytest.importorskip("ib_async")
from bot.broker.ibkr import IBKRBroker, map_state, rejection_message, trade_result  # noqa: E402


def _make_trade(symbol, order, status="Submitted", filled=0.0, remaining=None, avg=0.0):
    contract = ib_async.Stock(symbol, "SMART", "USD")
    st = ib_async.OrderStatus(
        orderId=order.orderId, status=status, filled=filled,
        remaining=float(order.totalQuantity or 0) if remaining is None else remaining,
        avgFillPrice=avg,
    )
    return ib_async.Trade(contract=contract, order=order, orderStatus=st, fills=[], log=[])


class _FakeIB:
    """Enough of ib_async.IB for the adapter, including its quirks:

    - a rejection is status "Cancelled" plus a TradeLogEntry with errorCode
    - orders from earlier sessions appear only after reqAllOpenOrders /
      reqCompletedOrders and carry no fill fields; fills come from
      reqExecutions
    """

    def __init__(self, positions=None, last=300.0):
        self._positions = positions or {}
        self._trades: list = []
        self._prior: list = []          # orders from a previous session
        self._executions: list = []
        self._next_id = 100
        self.last = last
        self.cancelled: list = []
        self.data_type = None
        self.assign_perm_id = True
        self.reject_next: tuple[int, str] | None = None
        self.sync_calls = 0

    # connection / account
    def managedAccounts(self):
        return ["DU123"]

    def disconnect(self):
        pass

    def sleep(self, _s):
        return None

    # contracts / data
    def qualifyContracts(self, contract):
        contract.conId = 1
        return [contract]

    def reqMarketDataType(self, n):
        self.data_type = n

    def reqTickers(self, *contracts):
        out = []
        for c in contracts:
            t = ib_async.Ticker(contract=c)
            t.last, t.bid, t.ask, t.volume = self.last, self.last - 0.01, self.last + 0.01, 1234.0
            out.append(t)
        return out

    def positions(self):
        return [
            ib_async.Position("DU123", ib_async.Stock(sym, "SMART", "USD"), qty, 280.0)
            for sym, qty in self._positions.items()
        ]

    def reqPositions(self):
        return self.positions()

    # orders
    def trades(self):
        return list(self._trades)

    def openTrades(self):
        return [t for t in self._trades if t.orderStatus.status not in ib_async.OrderStatus.DoneStates]

    def reqAllOpenOrders(self):
        self.sync_calls += 1
        for t in self._prior:
            if t not in self._trades:
                self._trades.append(t)
        return self.openTrades()

    def reqCompletedOrders(self, _api_only):
        return [t for t in self._trades if t.orderStatus.status in ib_async.OrderStatus.DoneStates]

    def reqExecutions(self, _filter=None):
        # Like the real wrapper: streamed executions are also appended to
        # the matching trade's fills.
        for fill in self._executions:
            for trade in self._trades:
                if trade.order.permId == fill.execution.permId and fill not in trade.fills:
                    trade.fills.append(fill)
        return list(self._executions)

    def placeOrder(self, contract, order):
        order.orderId = self._next_id
        order.permId = self._next_id * 10 if self.assign_perm_id else 0
        self._next_id += 1
        trade = _make_trade(contract.symbol, order)
        if self.reject_next:
            code, message = self.reject_next
            self.reject_next = None
            trade.orderStatus.status = "Cancelled"
            trade.log.append(ib_async.TradeLogEntry(time=None, status="Cancelled", message=message, errorCode=code))
        self._trades.append(trade)
        return trade

    def cancelOrder(self, order):
        self.cancelled.append(order.orderId)
        for t in self._trades:
            if t.order.orderId == order.orderId:
                t.orderStatus.status = "Cancelled"


def test_ibkr_state_mapping():
    assert map_state("Submitted", 0, 1) == "confirmed"
    assert map_state("PreSubmitted", 0, 1) == "confirmed"
    assert map_state("ValidationError", 0, 1) == "confirmed"
    assert map_state("Submitted", 0.5, 0.5) == "partially_filled"
    assert map_state("Filled", 1, 0) == "filled"
    assert map_state("ApiCancelled", 0, 1) == "cancelled"
    assert map_state("Cancelled", 0, 1, rejected=True) == "rejected"
    assert map_state("Inactive", 0, 1) == "rejected"
    assert map_state("PendingSubmit", 0, 1) == "queued"


def test_ibkr_rejection_is_cancelled_plus_error_log_entry():
    order = ib_async.MarketOrder("BUY", 1)
    order.orderId = 5
    trade = _make_trade("SPXL", order, status="Cancelled")
    trade.log.append(ib_async.TradeLogEntry(time=None, status="Cancelled", message="Error 201: Order rejected", errorCode=201))
    assert rejection_message(trade) == "Error 201: Order rejected (code 201)"
    assert trade_result(trade).state == "rejected"
    # Warning codes never count as a rejection; a user cancel has no error code.
    warned = _make_trade("SPXL", order, status="Submitted")
    warned.log.append(ib_async.TradeLogEntry(time=None, status="Submitted", message="warn", errorCode=399))
    assert rejection_message(warned) is None
    cancelled = _make_trade("SPXL", order, status="Cancelled")
    assert trade_result(cancelled).state == "cancelled"


def test_ibkr_place_order_raises_order_rejected_on_gateway_error():
    ib = _FakeIB()
    ib.reject_next = (10268, "cashQty not allowed without fractional permission")
    with pytest.raises(OrderRejected, match="fractional permission"):
        IBKRBroker(ib).place_order(OrderRequest("SPXL", "buy", "market", dollar_amount=20))


def test_ibkr_recovers_prior_session_order_and_fills_after_reconnect():
    ib = _FakeIB()
    order = ib_async.MarketOrder("BUY", 0)
    order.orderId, order.permId, order.cashQty = 7, 700, 20.0
    # completedOrder callback shape: Filled, but no filled/avgFillPrice.
    ib._prior.append(_make_trade("SPXL", order, status="Filled", filled=0.0, remaining=0.0))
    ib._executions.append(ib_async.Fill(
        ib_async.Stock("SPXL", "SMART", "USD"),
        ib_async.Execution(execId="e1", permId=700, shares=0.0709, price=282.14, avgPrice=282.14, side="BOT", cumQty=0.0709),
        ib_async.CommissionReport(), None,
    ))
    broker = IBKRBroker(ib)
    polled = broker.get_order("700", "SPXL")
    assert polled is not None
    assert polled.state == "filled"
    # The wrapper appends streamed executions to trade.fills too: no double count.
    assert polled.fill_qty == pytest.approx(0.0709)
    assert polled.fill_price == pytest.approx(282.14)
    assert broker.get_order("700", "SPXL").fill_qty == pytest.approx(0.0709)
    assert ib.sync_calls >= 1
    # An unknown id forces one resync, then is throttled on later polls.
    before = ib.sync_calls
    assert broker.get_order("nope") is None
    assert broker.get_order("nope") is None
    assert broker.get_order("nope") is None
    assert ib.sync_calls == before + 1


def test_ibkr_user_cancel_ack_and_pending_min_tick_are_classified_correctly():
    order = ib_async.MarketOrder("BUY", 1)
    order.orderId = 5
    cancelled = _make_trade("SPXL", order, status="Cancelled")
    cancelled.log.append(ib_async.TradeLogEntry(time=None, status="Cancelled", message="Error 202: Order Canceled", errorCode=202))
    assert rejection_message(cancelled) is None
    assert trade_result(cancelled).state == "cancelled"
    min_tick = _make_trade("SPXL", order, status="Cancelled")
    min_tick.log.append(ib_async.TradeLogEntry(time=None, status="Cancelled", message="Error 110: price does not conform", errorCode=110))
    assert trade_result(min_tick).state == "rejected"


def test_ibkr_ref_reuse_covers_filled_orders_from_this_and_prior_sessions():
    ib = _FakeIB()
    broker = IBKRBroker(ib)
    first = broker.place_order(OrderRequest("SPXL", "buy", "market", dollar_amount=20, ref_id="dayentry:abc"))
    trade = ib._trades[0]
    trade.orderStatus.status, trade.orderStatus.filled, trade.orderStatus.remaining = "Filled", 0.07, 0.0
    trade.orderStatus.avgFillPrice = 282.0
    again = broker.place_order(OrderRequest("SPXL", "buy", "market", dollar_amount=20, ref_id="dayentry:abc"))
    assert again.order_id == first.order_id and again.state == "filled" and len(ib._trades) == 1
    # Reconnect: the same ref must still be found among prior-session orders.
    ib2 = _FakeIB()
    ib2._prior.append(trade)
    reused = IBKRBroker(ib2).place_order(OrderRequest("SPXL", "buy", "market", dollar_amount=20, ref_id="dayentry:abc"))
    assert reused.order_id == first.order_id and ib2._trades == [trade]


def test_ibkr_order_id_falls_back_to_session_order_id_before_perm_id():
    ib = _FakeIB()
    ib.assign_perm_id = False
    broker = IBKRBroker(ib)
    result = broker.place_order(OrderRequest("SPXL", "buy", "market", dollar_amount=20))
    assert result.order_id == str(ib._trades[0].order.orderId)
    assert broker.get_order(result.order_id, "SPXL") is not None


def test_ibkr_quote_without_book_falls_back_to_last():
    ib = _FakeIB(last=283.18)
    original = ib.reqTickers

    def no_book(*contracts):
        tickers = original(*contracts)
        for t in tickers:
            t.bid = t.ask = float("nan")
        return tickers

    ib.reqTickers = no_book  # type: ignore[assignment]
    quote = IBKRBroker(ib).quotes(["SPXL"])["SPXL"]
    assert quote.last == 283.18 and quote.bid == 283.18 and quote.ask == 283.18
    assert quote.spread_pct == 0.0


def test_ibkr_quotes_positions_and_tradability():
    ib = _FakeIB(positions={"SPXL": 0.070886})
    broker = IBKRBroker(ib, market_data_type=3)
    quotes = broker.quotes(["spxl", "SPY"])
    assert ib.data_type == 3
    assert quotes["SPXL"].last == 300.0 and quotes["SPXL"].spread_pct == pytest.approx(0.02 / 300 * 100)
    assert broker.account_id() == "DU123"
    assert broker.position_qty("SPXL") == pytest.approx(0.070886)
    assert broker.position_qty("QQQ") == 0.0
    assert broker.tradability(["SPXL"])["SPXL"].fractional is True


def test_ibkr_market_buy_uses_cash_quantity_and_ref():
    ib = _FakeIB()
    broker = IBKRBroker(ib)
    result = broker.place_order(OrderRequest("SPXL", "buy", "market", dollar_amount=20, ref_id="dayentry:abc"))
    order = ib._trades[0].order
    assert order.action == "BUY" and order.orderType == "MKT"
    assert order.cashQty == 20.0 and order.totalQuantity == 0
    assert order.tif == "DAY" and order.outsideRth is False
    assert order.orderRef == "dayentry:abc" and order.account == "DU123"
    assert result.state == "confirmed" and result.order_id == str(order.permId)
    # Same ref while the order is open -> the existing order is returned, no duplicate.
    again = broker.place_order(OrderRequest("SPXL", "buy", "market", dollar_amount=20, ref_id="dayentry:abc"))
    assert again.order_id == result.order_id and len(ib._trades) == 1


def test_ibkr_sell_guard_refuses_to_open_a_short_using_fresh_positions():
    ib = _FakeIB(positions={"SPXL": 0.035})
    broker = IBKRBroker(ib)
    with pytest.raises(ShareShortfall):
        broker.place_order(OrderRequest("SPXL", "sell", "market", quantity=0.070886))
    assert ib._trades == []
    # A position push that arrived after the cached snapshot is honoured.
    ib._positions["SPXL"] = 0.070886
    ok = broker.place_order(OrderRequest("SPXL", "sell", "market", quantity=0.070886, regular_hours_only=False))
    assert ib._trades[0].order.totalQuantity == 0.070886 and ok.side == "sell"
    assert ib._trades[0].order.outsideRth is False  # market orders never opt out of RTH


def test_ibkr_stop_limit_cancel_and_poll():
    ib = _FakeIB(positions={"SPXL": 1.0})
    broker = IBKRBroker(ib)
    stop = broker.place_order(OrderRequest("SPXL", "sell", "stop", quantity=1.0, stop_price=290.555))
    limit = broker.place_order(OrderRequest("SPXL", "sell", "limit", quantity=1.0, limit_price=310.0))
    assert ib._trades[0].order.orderType == "STP" and ib._trades[0].order.auxPrice == 290.56
    assert ib._trades[1].order.orderType == "LMT" and ib._trades[1].order.lmtPrice == 310.0
    assert ib._trades[0].order.outsideRth is False
    assert {o.order_id for o in broker.open_orders("SPXL")} == {stop.order_id, limit.order_id}
    assert broker.cancel_order(stop.order_id) is True
    assert broker.get_order(stop.order_id, "SPXL").state == "cancelled"
    # Simulate a fill reported by the gateway.
    trade = ib._trades[1]
    trade.orderStatus.status, trade.orderStatus.filled, trade.orderStatus.remaining = "Filled", 1.0, 0.0
    trade.orderStatus.avgFillPrice = 310.0
    polled = broker.get_order(limit.order_id, "SPXL")
    assert polled.state == "filled" and polled.fill_usd == 310.0
    assert broker.get_order("nope") is None


def test_ibkr_inactive_order_raises_order_rejected():
    ib = _FakeIB()

    def reject(contract, order):
        trade = _FakeIB.placeOrder(ib, contract, order)
        trade.orderStatus.status = "Inactive"
        return trade

    ib.placeOrder = reject  # type: ignore[assignment]
    with pytest.raises(OrderRejected, match="inactive order"):
        IBKRBroker(ib).place_order(OrderRequest("SPXL", "buy", "market", dollar_amount=20))


# ---------------------------------------------------------------------------
# factory + swing placement
# ---------------------------------------------------------------------------

def test_factory_reads_env_and_role_client_ids(monkeypatch):
    monkeypatch.setenv("BROKER", "ibkr")
    monkeypatch.setenv("IBKR_CLIENT_ID", "40")
    assert ibkr_client_id("day_trader") == 40
    assert ibkr_client_id("swing") == 40 + CLIENT_ID_OFFSETS["swing"]
    monkeypatch.setenv("IBKR_CLIENT_ID_SWING", "99")
    assert ibkr_client_id("swing") == 99
    with patch("bot.broker.ibkr.IBKRBroker.from_env", return_value="ib-broker") as from_env:
        assert create_broker(role="swing") == "ib-broker"
    from_env.assert_called_once_with(client_id=99)
    monkeypatch.setenv("BROKER", "robinhood")
    with patch("bot.broker.robinhood.RobinhoodBroker.connect", return_value="rh-broker"):
        assert create_broker() == "rh-broker"
    monkeypatch.setenv("BROKER", "etrade")
    with pytest.raises(BrokerError):
        create_broker()


def test_swing_entry_places_dollar_market_buy_and_polls_fill(monkeypatch):
    broker = FakeBroker(account="DU123")
    calls = {"n": 0}
    original = broker.get_order

    def get_order(order_id, symbol=None):
        calls["n"] += 1
        if calls["n"] == 2:
            broker.fill(order_id, 284.84, 0.0351)
        return original(order_id, symbol)

    broker.get_order = get_order  # type: ignore[assignment]
    proposal = {
        "id": "ord_1", "signal_kind": "ENTRY", "ticker": "SPXL", "action": "BUY",
        "usd_amount": 10.0, "shares_estimate": 0.0352, "signal_price": 284.35,
        "signal": {"message_id": 5, "price": 284.35}, "book_before": {},
    }
    with patch.object(swing_orders.time, "sleep"):
        result = swing_orders.place_swing_order(proposal, 10.0, broker=broker)
    assert broker.requests[0].dollar_amount == 10.0 and broker.requests[0].side == "buy"
    assert broker.requests[0].ref_id == swing_orders.proposal_ref_id(proposal)
    assert result.state == "filled" and result.fill_qty == 0.0351


def test_swing_placement_refuses_duplicate_open_order():
    broker = FakeBroker()
    broker.orders["open-1"] = OrderResult("open-1", "confirmed", symbol="SPXL", side="buy")
    proposal = {
        "id": "ord_1", "signal_kind": "ENTRY", "ticker": "SPXL", "action": "BUY",
        "usd_amount": 10.0, "shares_estimate": 0.0352, "signal": {}, "book_before": {},
    }
    with pytest.raises(BrokerError, match="Existing open order"):
        swing_orders.place_swing_order(proposal, 10.0, broker=broker)
    assert not broker.requests
