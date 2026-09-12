"""In-memory Broker for tests: scripted quotes, positions and fills."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from bot.broker.base import (
    Broker,
    BrokerError,
    OrderRequest,
    OrderResult,
    Position,
    Quote,
    ShareShortfall,
    Tradability,
)


def quote(symbol: str, last: float, bid: float | None = None, ask: float | None = None,
          volume: float | None = None, average_volume: float | None = None) -> Quote:
    return Quote(symbol.upper(), last, bid if bid is not None else last,
                 ask if ask is not None else last, volume, average_volume)


@dataclass
class FakeBroker(Broker):
    name = "fake"
    account: str = "acct"
    quote_book: dict[str, Quote] = field(default_factory=dict)
    tradable: dict[str, Tradability] = field(default_factory=dict)
    holdings: dict[str, float] = field(default_factory=dict)
    # Scripted results returned by successive place_order calls; when empty a
    # queued order is returned and can be advanced with ``fill``.
    place_results: list[OrderResult | Exception] = field(default_factory=list)
    orders: dict[str, OrderResult] = field(default_factory=dict)
    requests: list[OrderRequest] = field(default_factory=list)
    calls: list[tuple[str, Any]] = field(default_factory=list)
    quote_error: Exception | None = None
    guard_sells: bool = False
    _seq: int = 0

    # -- setup helpers ---------------------------------------------------
    def set_quote(self, symbol: str, last: float, bid: float | None = None, ask: float | None = None, **kw) -> None:
        self.quote_book[symbol.upper()] = quote(symbol, last, bid, ask, **kw)

    def fill(self, order_id: str, price: float, qty: float, state: str = "filled") -> None:
        current = self.orders[order_id]
        self.orders[order_id] = OrderResult(order_id, state, price, qty, round(price * qty, 4), current.symbol, current.side)

    def set_state(self, order_id: str, state: str) -> None:
        current = self.orders[order_id]
        self.orders[order_id] = OrderResult(order_id, state, current.fill_price, current.fill_qty, current.fill_usd, current.symbol, current.side)

    # -- Broker API ------------------------------------------------------
    def account_id(self) -> str:
        return self.account

    def quotes(self, symbols: list[str]) -> dict[str, Quote]:
        self.calls.append(("quotes", list(symbols)))
        if self.quote_error:
            raise self.quote_error
        return {s.upper(): self.quote_book[s.upper()] for s in symbols if s.upper() in self.quote_book}

    def tradability(self, symbols: list[str]) -> dict[str, Tradability]:
        self.calls.append(("tradability", list(symbols)))
        out = {}
        for s in symbols:
            s = s.upper()
            out[s] = self.tradable.get(s, Tradability(s, True, True))
        return out

    def positions(self) -> dict[str, Position]:
        self.calls.append(("positions", None))
        return {s: Position(s, q) for s, q in self.holdings.items() if q > 0}

    def open_orders(self, symbol: str | None = None) -> list[OrderResult]:
        self.calls.append(("open_orders", symbol))
        return [o for o in self.orders.values()
                if not o.is_terminal and (symbol is None or o.symbol == symbol.upper())]

    def get_order(self, order_id: str, symbol: str | None = None) -> OrderResult | None:
        self.calls.append(("get_order", order_id))
        return self.orders.get(order_id)

    def place_order(self, request: OrderRequest) -> OrderResult:
        self.calls.append(("place_order", request))
        self.requests.append(request)
        if self.guard_sells and request.side == "sell":
            held = self.holdings.get(request.symbol.upper(), 0.0)
            if float(request.quantity or 0) > held + 1e-6:
                raise ShareShortfall(f"Not enough shares to sell: {request.symbol}")
        if self.place_results:
            scripted = self.place_results.pop(0)
            if isinstance(scripted, Exception):
                raise scripted
            result = OrderResult(scripted.order_id, scripted.state, scripted.fill_price, scripted.fill_qty,
                                 scripted.fill_usd, request.symbol.upper(), request.side)
        else:
            self._seq += 1
            result = OrderResult(f"ord-{self._seq}", "queued", None, None, None, request.symbol.upper(), request.side)
        self.orders[result.order_id] = result
        return result

    def cancel_order(self, order_id: str) -> bool:
        self.calls.append(("cancel_order", order_id))
        if order_id not in self.orders:
            raise BrokerError(f"unknown order {order_id}")
        self.set_state(order_id, "cancelled")
        return True
