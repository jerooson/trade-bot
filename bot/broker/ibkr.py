"""Interactive Brokers adapter (TWS / IB Gateway via ``ib_async``).

Paper first: point ``IBKR_PORT`` at the paper gateway (4002) or paper TWS
(7497).  The adapter is synchronous like the rest of the bot: ``ib_async``
runs its own event loop inside the blocking calls we use.

Behaviour notes
---------------
- Fractional buys use IBKR cash-quantity orders (``cashQty``), which need
  fractional-share permission on the account.
- ib_async reports an API rejection as status ``Cancelled`` plus a
  ``TradeLogEntry`` carrying the error code; the adapter surfaces that as
  ``OrderRejected`` at placement and as state ``rejected`` when polled, so
  a refused order is never mistaken for a user cancellation.
- Sells are guarded: IBKR margin accounts would happily open a short if a
  sell exceeds the holding.  Positions are refreshed from the gateway before
  the check and the adapter refuses with ``ShareShortfall``.
- ``ref_id`` is stored in ``orderRef``; placing the same ``ref_id`` again
  returns the existing order in *any* state (filled included) instead of a
  duplicate, matching Robinhood's server-side idempotency.
- Order ids are IBKR ``permId`` strings (stable across sessions); before the
  gateway assigns one, the session ``orderId`` is used.  After a reconnect
  the adapter pulls open + completed orders and executions from the gateway
  so earlier orders can still be polled with their fills.
"""

from __future__ import annotations

import logging
import math
import os
import time
from typing import Any

from bot.broker.base import (
    Broker,
    BrokerError,
    OrderRejected,
    OrderRequest,
    OrderResult,
    Position,
    Quote,
    ShareShortfall,
    Tradability,
)

log = logging.getLogger("bot.broker.ibkr")

_STATE_MAP = {
    "pendingsubmit": "queued",
    "apipending": "queued",
    "presubmitted": "confirmed",
    "submitted": "confirmed",
    # A validation warning leaves the order live (ib_async keeps it active).
    "validationerror": "confirmed",
    "filled": "filled",
    "cancelled": "cancelled",
    "apicancelled": "cancelled",
    "pendingcancel": "confirmed",
    "inactive": "rejected",
}
# Mirrors ib_async.wrapper.Wrapper.error(): these codes never change status.
_WARNING_CODES = frozenset({105, 110, 165, 321, 329, 399, 404, 434, 492, 10167})
_ACK_WAIT_S = 0.5
_ACK_POLLS = 6
_ORDER_REF_MAX = 60
_FORCED_SYNC_MIN_INTERVAL_S = 60.0


def _num(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(parsed) or parsed < 0:
        return None
    return parsed


def _is_warning(code: int) -> bool:
    return code in _WARNING_CODES or 2100 <= code < 2200


def rejection_message(trade: Any) -> str | None:
    """Text of the gateway error that killed this order, if any.

    Error 202 is TWS acknowledging *our* cancel request and is never a
    rejection.  A normally-warning code (for example 110 min-tick) counts
    when ib_async recorded it with status ``Cancelled``, because that is how
    the wrapper reports a hard cancel of a pending order.
    """
    message: str | None = None
    for entry in getattr(trade, "log", []) or []:
        code = int(getattr(entry, "errorCode", 0) or 0)
        if not code or code == 202:
            continue
        entry_status = str(getattr(entry, "status", "") or "").lower()
        if not _is_warning(code) or entry_status == "cancelled":
            message = f"{getattr(entry, 'message', '') or 'order error'} (code {code})"
    return message


def map_state(status: str, filled: float | None, remaining: float | None, *, rejected: bool = False) -> str:
    state = _STATE_MAP.get(str(status or "").lower(), str(status or "unknown").lower())
    if state == "cancelled" and rejected:
        return "rejected"
    if state in ("confirmed", "queued") and (filled or 0) > 0 and (remaining or 0) > 0:
        return "partially_filled"
    return state


def _fill_totals(fills: list[Any]) -> tuple[float | None, float | None]:
    """Weighted (avg_price, qty) over ib_async Fill objects, deduped by execId.

    ib_async appends executions returned by ``reqExecutions`` to
    ``trade.fills`` as well, so the same fill can reach us twice.
    """
    qty = 0.0
    notional = 0.0
    seen: set[str] = set()
    for fill in fills or []:
        execution = getattr(fill, "execution", None)
        exec_id = str(getattr(execution, "execId", "") or "") or f"id:{id(fill)}"
        if exec_id in seen:
            continue
        seen.add(exec_id)
        shares = _num(getattr(execution, "shares", None)) or 0.0
        price = _num(getattr(execution, "price", None)) or _num(getattr(execution, "avgPrice", None)) or 0.0
        if shares > 0 and price > 0:
            qty += shares
            notional += shares * price
    if qty <= 0:
        return None, None
    return notional / qty, qty


def trade_result(trade: Any, extra_fills: list[Any] | None = None) -> OrderResult:
    status = trade.orderStatus
    filled = _num(status.filled)
    avg = _num(status.avgFillPrice)
    if not filled or not avg:
        # Orders learned through openOrder/completedOrder callbacks carry no
        # fill fields; rebuild them from executions when we have them.
        fills = list(getattr(trade, "fills", []) or []) + list(extra_fills or [])
        fill_avg, fill_qty = _fill_totals(fills)
        if fill_qty:
            filled, avg = fill_qty, fill_avg
    order_id = str(trade.order.permId or trade.order.orderId)
    return OrderResult(
        order_id=order_id,
        state=map_state(
            status.status, filled, _num(status.remaining),
            rejected=rejection_message(trade) is not None,
        ),
        fill_price=avg if avg else None,
        fill_qty=filled if filled else None,
        fill_usd=round(avg * filled, 4) if avg and filled else None,
        symbol=getattr(trade.contract, "symbol", None),
        side=str(trade.order.action or "").lower() or None,
    )


class IBKRBroker(Broker):
    name = "ibkr"

    def __init__(
        self,
        ib: Any,
        *,
        account: str | None = None,
        market_data_type: int = 3,
        fractional_symbols: set[str] | None = None,
    ) -> None:
        self._ib = ib
        self._account = account
        self._market_data_type = market_data_type
        self._fractional = fractional_symbols  # None = assume enabled for all
        self._contracts: dict[str, Any] = {}
        self._data_type_set = False
        self._orders_synced = False
        self._last_forced_sync = 0.0

    @classmethod
    def connect(
        cls,
        host: str = "127.0.0.1",
        port: int = 4002,
        client_id: int = 17,
        *,
        account: str | None = None,
        market_data_type: int = 3,
        timeout: float = 20.0,
        readonly: bool = False,
    ) -> "IBKRBroker":
        try:
            from ib_async import IB
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise BrokerError(
                "ib_async is not installed; run `pip install ib_async` (or `pip install .[ibkr]`)"
            ) from exc
        ib = IB()
        try:
            ib.connect(host, port, clientId=client_id, timeout=timeout, readonly=readonly)
        except Exception as exc:
            raise BrokerError(f"cannot connect to IB Gateway/TWS at {host}:{port}: {exc}") from exc
        log.info("Connected to IBKR at %s:%d clientId=%d accounts=%s", host, port, client_id, ib.managedAccounts())
        return cls(ib, account=account, market_data_type=market_data_type)

    @classmethod
    def from_env(cls, client_id: int | None = None) -> "IBKRBroker":
        return cls.connect(
            host=os.environ.get("IBKR_HOST", "127.0.0.1"),
            port=int(os.environ.get("IBKR_PORT", "4002")),
            client_id=client_id if client_id is not None else int(os.environ.get("IBKR_CLIENT_ID", "17")),
            account=os.environ.get("IBKR_ACCOUNT") or None,
            market_data_type=int(os.environ.get("IBKR_MARKET_DATA_TYPE", "3")),
        )

    def close(self) -> None:
        try:
            self._ib.disconnect()
        except Exception:  # pragma: no cover
            pass

    # -- helpers ---------------------------------------------------------
    def _contract(self, symbol: str) -> Any:
        symbol = symbol.upper()
        contract = self._contracts.get(symbol)
        if contract is None:
            from ib_async import Stock
            contract = Stock(symbol, "SMART", "USD")
            qualified = self._ib.qualifyContracts(contract)
            if not qualified:
                raise BrokerError(f"IBKR cannot qualify contract for {symbol}")
            contract = qualified[0]
            self._contracts[symbol] = contract
        return contract

    def _ensure_data_type(self) -> None:
        if not self._data_type_set:
            self._ib.reqMarketDataType(self._market_data_type)
            self._data_type_set = True

    def _sync_orders(self, force: bool = False) -> None:
        """Load orders from earlier sessions (open and completed) once."""
        if self._orders_synced and not force:
            return
        now = time.monotonic()
        if force and now - self._last_forced_sync < _FORCED_SYNC_MIN_INTERVAL_S:
            # An id the gateway does not know (e.g. an order from the other
            # broker) must not trigger a full resync on every 5-second poll.
            return
        try:
            self._ib.reqAllOpenOrders()
            self._ib.reqCompletedOrders(False)
        except Exception as exc:  # pragma: no cover - best effort
            log.warning("IBKR: could not sync earlier orders: %s", exc)
        self._orders_synced = True
        if force:
            self._last_forced_sync = now

    def _trades(self) -> list[Any]:
        return list(self._ib.trades())

    def _find_trade(self, order_id: str) -> Any | None:
        for trade in self._trades():
            if str(trade.order.permId) == order_id or str(trade.order.orderId) == order_id:
                return trade
        return None

    def _find_by_ref(self, ref_id: str) -> Any | None:
        wanted = ref_id[:_ORDER_REF_MAX]
        for trade in self._trades():
            if trade.order.orderRef == wanted:
                return trade
        return None

    def _executions_for(self, trade: Any) -> list[Any]:
        """Fills for this order from the gateway (needed after a reconnect)."""
        perm = int(trade.order.permId or 0)
        order_id = int(trade.order.orderId or 0)
        try:
            fills = list(self._ib.reqExecutions())
        except Exception as exc:  # pragma: no cover - best effort
            log.warning("IBKR: reqExecutions failed: %s", exc)
            return []
        matched = []
        for fill in fills:
            execution = getattr(fill, "execution", None)
            if execution is None:
                continue
            if (perm and int(getattr(execution, "permId", 0) or 0) == perm) or (
                not perm and order_id and int(getattr(execution, "orderId", 0) or 0) == order_id
            ):
                matched.append(fill)
        return matched

    def _fresh_positions(self) -> dict[str, Position]:
        try:
            rows = list(self._ib.reqPositions())
        except Exception as exc:  # pragma: no cover - best effort
            log.warning("IBKR: reqPositions failed, using cached positions: %s", exc)
            rows = list(self._ib.positions())
        return self._positions_from(rows)

    def _positions_from(self, rows: list[Any]) -> dict[str, Position]:
        out: dict[str, Position] = {}
        account = self.account_id()
        for p in rows:
            if getattr(p, "account", account) != account:
                continue
            if getattr(p.contract, "secType", "STK") != "STK":
                continue
            symbol = str(p.contract.symbol).upper()
            qty = float(p.position)
            if abs(qty) <= 0:
                continue
            out[symbol] = Position(symbol, qty, _num(getattr(p, "avgCost", None)))
        return out

    # -- Broker API ------------------------------------------------------
    def account_id(self) -> str:
        if self._account:
            return self._account
        accounts = list(self._ib.managedAccounts())
        if not accounts:
            raise BrokerError("IBKR session reports no managed accounts")
        self._account = str(accounts[0])
        return self._account

    def quotes(self, symbols: list[str]) -> dict[str, Quote]:
        wanted = list(dict.fromkeys(s.upper() for s in symbols))
        if not wanted:
            return {}
        self._ensure_data_type()
        contracts = [self._contract(s) for s in wanted]
        tickers = self._ib.reqTickers(*contracts)
        out: dict[str, Quote] = {}
        for contract, ticker in zip(contracts, tickers):
            symbol = str(contract.symbol).upper()
            last = _num(getattr(ticker, "last", None)) or _num(getattr(ticker, "close", None))
            bid = _num(getattr(ticker, "bid", None)) or None
            ask = _num(getattr(ticker, "ask", None)) or None
            if last and (bid is None or ask is None):
                # Delayed / frozen snapshots outside the session carry no
                # book.  Use the last trade for both sides so preflight can
                # still compute a (zero) spread; the log says it is synthetic.
                bid = bid or last
                ask = ask or last
                log.debug("IBKR quote for %s has no bid/ask; using last=%.4f for both", symbol, last)
            out[symbol] = Quote(
                symbol=symbol,
                last=last if last else None,
                bid=bid,
                ask=ask,
                volume=_num(getattr(ticker, "volume", None)),
                # IBKR snapshots carry no 30-day average volume; curated
                # leveraged routes do not need it, others fall back to volume.
                average_volume=None,
            )
        return out

    def tradability(self, symbols: list[str]) -> dict[str, Tradability]:
        out: dict[str, Tradability] = {}
        for symbol in dict.fromkeys(s.upper() for s in symbols):
            try:
                self._contract(symbol)
            except BrokerError as exc:
                out[symbol] = Tradability(symbol, False, False, state=str(exc))
                continue
            fractional = True if self._fractional is None else symbol in self._fractional
            out[symbol] = Tradability(symbol, True, fractional, state="active")
        return out

    def positions(self) -> dict[str, Position]:
        return self._positions_from(list(self._ib.positions()))

    def open_orders(self, symbol: str | None = None) -> list[OrderResult]:
        self._sync_orders()
        wanted = symbol.upper() if symbol else None
        results = []
        for trade in self._ib.openTrades():
            if wanted and str(trade.contract.symbol).upper() != wanted:
                continue
            results.append(trade_result(trade))
        return results

    def get_order(self, order_id: str, symbol: str | None = None) -> OrderResult | None:
        self._sync_orders()
        trade = self._find_trade(order_id)
        if trade is None:
            self._sync_orders(force=True)
            trade = self._find_trade(order_id)
        if trade is None:
            return None
        result = trade_result(trade)
        if result.state in ("filled", "partially_filled") and not result.fill_qty:
            result = trade_result(trade, self._executions_for(trade))
            if not result.fill_qty:
                log.error(
                    "IBKR order %s reports %s but no executions are available yet",
                    order_id, result.state,
                )
        return result

    def place_order(self, request: OrderRequest) -> OrderResult:
        from ib_async import LimitOrder, MarketOrder, Order, StopOrder

        self._sync_orders()
        symbol = request.symbol.upper()
        action = request.side.upper()
        if request.ref_id:
            existing = self._find_by_ref(request.ref_id)
            if existing is not None:
                log.info("IBKR: reusing order with ref %s (state=%s)", request.ref_id, existing.orderStatus.status)
                return self.get_order(str(existing.order.permId or existing.order.orderId), symbol) or trade_result(existing)
        contract = self._contract(symbol)

        if action == "SELL":
            held = self._fresh_positions().get(symbol)
            held_qty = float(held.quantity) if held else 0.0
            qty = float(request.quantity or 0.0)
            if qty > held_qty + 1e-6:
                raise ShareShortfall(
                    f"Not enough shares to sell: {symbol} sell {qty:.6f} but account holds {held_qty:.6f}"
                )

        order: Order
        if request.order_type == "market":
            if request.dollar_amount is not None:
                order = MarketOrder(action, 0)
                order.cashQty = round(float(request.dollar_amount), 2)
            else:
                order = MarketOrder(action, float(request.quantity))
        elif request.order_type == "limit":
            order = LimitOrder(action, float(request.quantity), round(float(request.limit_price), 2))
        else:
            order = StopOrder(action, float(request.quantity), round(float(request.stop_price), 2))
        order.tif = "DAY" if request.time_in_force.lower() in ("gfd", "day") else request.time_in_force.upper()
        # IBKR does not run market orders outside regular hours; only resting
        # stop/limit orders may opt in.
        order.outsideRth = (not request.regular_hours_only) and request.order_type != "market"
        order.account = self.account_id()
        if request.ref_id:
            order.orderRef = request.ref_id[:_ORDER_REF_MAX]

        log.info("IBKR placing %s %s %s qty=%s cash=%s ref=%s", action, request.order_type, symbol,
                 request.quantity, request.dollar_amount, request.ref_id)
        trade = self._ib.placeOrder(contract, order)
        for _ in range(_ACK_POLLS):
            self._ib.sleep(_ACK_WAIT_S)
            status = str(trade.orderStatus.status or "").lower()
            if status and status not in ("pendingsubmit", "apipending"):
                break
        rejection = rejection_message(trade)
        if rejection is not None or str(trade.orderStatus.status).lower() == "inactive":
            raise OrderRejected(f"IBKR rejected {action} {symbol}: {rejection or 'inactive order'}")
        return trade_result(trade)

    def cancel_order(self, order_id: str) -> bool:
        self._sync_orders()
        trade = self._find_trade(order_id)
        if trade is None:
            raise BrokerError(f"IBKR order {order_id} not found")
        self._ib.cancelOrder(trade.order)
        self._ib.sleep(0.2)
        return True
