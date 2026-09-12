"""Robinhood Agentic-account adapter over the direct MCP session."""

from __future__ import annotations

import logging
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
from bot.leveraged_etfs import result_by_symbol
from bot.robinhood_mcp_client import RobinhoodMCPError, _load_token, _MCPSession

log = logging.getLogger("bot.broker.robinhood")

_ORDER_TYPES = {"market": "market", "limit": "limit", "stop": "stop_market"}


def _float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed


def _order_result(order: dict[str, Any], symbol: str | None = None) -> OrderResult:
    fill_price = _float(order.get("average_price"))
    fill_qty = _float(order.get("cumulative_quantity"))
    return OrderResult(
        order_id=str(order.get("id") or ""),
        state=str(order.get("state") or "unknown").lower(),
        fill_price=fill_price,
        fill_qty=fill_qty,
        fill_usd=round(fill_price * fill_qty, 4) if fill_price and fill_qty else None,
        symbol=(order.get("symbol") or symbol or None),
        side=order.get("side"),
    )


def _classify(exc: RobinhoodMCPError) -> BrokerError:
    """Map an MCP failure onto the broker-agnostic exception hierarchy."""
    message = str(exc)
    lowered = message.lower()
    if "not enough shares" in lowered:
        return ShareShortfall(message)
    if "returned iserror:" in lowered:
        return OrderRejected(message)
    return exc


class RobinhoodBroker(Broker):
    name = "robinhood"

    def __init__(self, session: _MCPSession, account_number: str | None = None) -> None:
        self._session = session
        self._account = account_number

    @classmethod
    def connect(cls) -> "RobinhoodBroker":
        return cls(_MCPSession(_load_token()))

    # -- account ---------------------------------------------------------
    def account_id(self) -> str:
        if self._account:
            return self._account
        data = self._session.call("get_accounts")
        accounts = data.get("data", {}).get("accounts", [])
        agentic = [a for a in accounts if a.get("agentic_allowed")]
        if not agentic:
            raise BrokerError(
                "No Agentic account found (agentic_allowed=true). "
                "Complete Robinhood Agentic onboarding in the Robinhood app."
            )
        self._account = str(agentic[0]["account_number"])
        return self._account

    # -- market data -----------------------------------------------------
    def quotes(self, symbols: list[str]) -> dict[str, Quote]:
        wanted = list(dict.fromkeys(s.upper() for s in symbols))
        if not wanted:
            return {}
        data = self._session.call("get_equity_quotes", symbols=wanted)
        rows = data.get("data", {}).get("results", [])
        out: dict[str, Quote] = {}
        for symbol, item in result_by_symbol(rows, wanted).items():
            q = item.get("quote") or item
            out[symbol] = Quote(
                symbol=symbol,
                last=_float(q.get("last_trade_price")),
                bid=_float(q.get("bid_price")),
                ask=_float(q.get("ask_price")),
                volume=_float(q.get("volume")),
                average_volume=_float(
                    q.get("average_volume_30_days") or q.get("average_volume")
                ),
            )
        return out

    def tradability(self, symbols: list[str]) -> dict[str, Tradability]:
        wanted = list(dict.fromkeys(s.upper() for s in symbols))
        if not wanted:
            return {}
        data = self._session.call(
            "get_equity_tradability", account_number=self.account_id(), symbols=wanted
        )
        rows = data.get("data", {}).get("results", [])
        out: dict[str, Tradability] = {}
        for symbol, item in result_by_symbol(rows, wanted).items():
            trade = item.get("tradability") or item
            out[symbol] = Tradability(
                symbol=symbol,
                tradeable=bool(trade.get("tradeable", True)),
                fractional=trade.get("fractional_tradability", "tradable") != "untradable",
                state=trade.get("state"),
            )
        return out

    # -- positions / orders ---------------------------------------------
    def positions(self) -> dict[str, Position]:
        data = self._session.call("get_equity_positions", account_number=self.account_id())
        out: dict[str, Position] = {}
        for p in data.get("data", {}).get("positions", []):
            symbol = str(p.get("symbol") or "").upper()
            if symbol:
                out[symbol] = Position(
                    symbol=symbol,
                    quantity=_float(p.get("quantity")) or 0.0,
                    avg_cost=_float(p.get("average_buy_price")),
                )
        return out

    def _orders(self, symbol: str | None = None, order_id: str | None = None) -> list[dict[str, Any]]:
        kwargs: dict[str, Any] = {"account_number": self.account_id()}
        if symbol:
            kwargs["symbol"] = symbol.upper()
        if order_id:
            kwargs["order_id"] = order_id
        data = self._session.call("get_equity_orders", **kwargs)
        return list(data.get("data", {}).get("orders", []))

    def open_orders(self, symbol: str | None = None) -> list[OrderResult]:
        from bot.broker.base import OPEN_STATES
        return [
            _order_result(o, symbol)
            for o in self._orders(symbol)
            if str(o.get("state") or "").lower() in OPEN_STATES
        ]

    def get_order(self, order_id: str, symbol: str | None = None) -> OrderResult | None:
        # Filtering by symbol keeps the response small; Robinhood also accepts
        # order_id directly, which we use when no symbol is known.
        rows = self._orders(symbol) if symbol else self._orders(order_id=order_id)
        order = next((o for o in rows if str(o.get("id")) == order_id), None)
        return _order_result(order, symbol) if order else None

    def place_order(self, request: OrderRequest) -> OrderResult:
        kwargs: dict[str, Any] = {
            "account_number": self.account_id(),
            "symbol": request.symbol.upper(),
            "side": request.side,
            "type": _ORDER_TYPES[request.order_type],
            "time_in_force": request.time_in_force,
        }
        if request.regular_hours_only and request.order_type == "market":
            kwargs["market_hours"] = "regular_hours"
        if request.ref_id:
            kwargs["ref_id"] = request.ref_id
        if request.dollar_amount is not None:
            kwargs["dollar_amount"] = f"{request.dollar_amount:.2f}"
        else:
            kwargs["quantity"] = f"{float(request.quantity):.6f}"
        if request.limit_price is not None:
            kwargs["limit_price"] = str(round(request.limit_price, 2))
        if request.stop_price is not None:
            kwargs["stop_price"] = str(round(request.stop_price, 2))
        log.info("Placing order: %s", {k: v for k, v in kwargs.items() if k != "account_number"})
        try:
            resp = self._session.call("place_equity_order", **kwargs)
        except RobinhoodMCPError as exc:
            raise _classify(exc) from exc
        order = resp.get("data", {}).get("order", {}) if isinstance(resp, dict) else {}
        if not order.get("id"):
            raise BrokerError(f"place_equity_order response missing order id: {resp}")
        return _order_result(order, request.symbol.upper())

    def cancel_order(self, order_id: str) -> bool:
        self._session.call(
            "cancel_equity_order", account_number=self.account_id(), order_id=order_id
        )
        return True
