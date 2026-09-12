"""
Broker-agnostic execution interface.

Both strategies (day trader, swing shadow reviewer) talk to a ``Broker``:
quotes, tradability, positions, orders.  Adapters translate to Robinhood's
Agentic MCP or to Interactive Brokers (``ib_async``).  Nothing above this
layer knows which broker is live, so the whole pipeline can be exercised
against the IBKR paper account before any real routing changes.

Conventions
-----------
- Symbols are upper-case US equity tickers.
- Order states are lower-case and Robinhood-shaped so the existing state
  machines keep working: ``queued``, ``confirmed``, ``partially_filled``,
  ``filled``, ``cancelled``, ``rejected``, ``failed``, ``expired``.
- ``OrderResult`` carries the broker order id plus fill details when known.
- A broker that refuses an order *before* acknowledging it raises
  ``OrderRejected`` (a definitive rejection; safe to retry later with the same
  ``ref_id``).  A sell that exceeds the account holding raises the
  ``ShareShortfall`` subclass so the day trader can reconcile ownership.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal

OPEN_STATES = {"new", "queued", "confirmed", "unconfirmed", "partially_filled"}
TERMINAL_FAILURE_STATES = {"cancelled", "canceled", "rejected", "failed", "expired"}
TERMINAL_STATES = TERMINAL_FAILURE_STATES | {"filled"}

Side = Literal["buy", "sell"]
OrderType = Literal["market", "limit", "stop"]


class BrokerError(Exception):
    """Any failure talking to the broker."""


class OrderRejected(BrokerError):
    """The broker refused the order before acknowledging it (definitive)."""


class ShareShortfall(OrderRejected):
    """A sell was refused because the account holds fewer shares."""


class OwnershipBlocked(BrokerError):
    """No order was placed: nothing sellable belongs to this strategy.

    Raised before placement when the account-level quantity is zero, or when
    every share the account holds belongs to another strategy (see
    ``bot.position_ownership``).  Deterministic; not an ambiguous outcome.
    """


@dataclass
class OrderResult:
    """Result of a placed or polled order, including fill details when known."""
    order_id: str
    state: str
    fill_price: float | None = None    # average fill price (None if not yet filled)
    fill_qty: float | None = None      # cumulative filled quantity
    fill_usd: float | None = None      # fill_price x fill_qty
    symbol: str | None = None
    side: str | None = None

    @property
    def is_filled(self) -> bool:
        return self.state.lower() == "filled"

    @property
    def is_terminal(self) -> bool:
        return self.state.lower() in TERMINAL_STATES


@dataclass(frozen=True)
class Quote:
    symbol: str
    last: float | None
    bid: float | None
    ask: float | None
    volume: float | None = None
    average_volume: float | None = None

    @property
    def price(self) -> float | None:
        """Best available mark: last trade, else midpoint, else one side."""
        if self.last is not None and self.last > 0:
            return self.last
        if self.ask is not None and self.bid is not None:
            return (self.ask + self.bid) / 2
        return self.ask if self.ask is not None else self.bid

    @property
    def spread_pct(self) -> float | None:
        if self.bid is None or self.ask is None or self.bid <= 0 or self.ask <= 0:
            return None
        midpoint = (self.ask + self.bid) / 2
        return (self.ask - self.bid) / midpoint * 100


@dataclass(frozen=True)
class Tradability:
    symbol: str
    tradeable: bool
    fractional: bool
    state: str | None = None


@dataclass(frozen=True)
class Position:
    symbol: str
    quantity: float
    avg_cost: float | None = None


@dataclass(frozen=True)
class OrderRequest:
    symbol: str
    side: Side
    order_type: OrderType = "market"
    quantity: float | None = None          # shares (sell / stop / limit)
    dollar_amount: float | None = None     # fractional buy by notional
    limit_price: float | None = None
    stop_price: float | None = None
    time_in_force: str = "gfd"
    regular_hours_only: bool = True
    ref_id: str | None = None              # idempotency key

    def __post_init__(self) -> None:
        if self.quantity is None and self.dollar_amount is None:
            raise ValueError("OrderRequest needs quantity or dollar_amount")
        if self.order_type == "limit" and self.limit_price is None:
            raise ValueError("limit order needs limit_price")
        if self.order_type == "stop" and self.stop_price is None:
            raise ValueError("stop order needs stop_price")


class Broker(ABC):
    """Minimal execution surface shared by every adapter."""

    name: str = "abstract"

    @abstractmethod
    def account_id(self) -> str: ...

    @abstractmethod
    def quotes(self, symbols: list[str]) -> dict[str, Quote]: ...

    @abstractmethod
    def tradability(self, symbols: list[str]) -> dict[str, Tradability]: ...

    @abstractmethod
    def positions(self) -> dict[str, Position]: ...

    @abstractmethod
    def open_orders(self, symbol: str | None = None) -> list[OrderResult]: ...

    @abstractmethod
    def get_order(self, order_id: str, symbol: str | None = None) -> OrderResult | None: ...

    @abstractmethod
    def place_order(self, request: OrderRequest) -> OrderResult: ...

    @abstractmethod
    def cancel_order(self, order_id: str) -> bool: ...

    def position_qty(self, symbol: str) -> float:
        position = self.positions().get(symbol.upper())
        return float(position.quantity) if position else 0.0

    def close(self) -> None:  # pragma: no cover - adapters override when needed
        return None
