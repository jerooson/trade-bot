"""Place a swing proposal through whichever broker is configured.

Ported from the Robinhood-only ``place_order``: the same pre-placement
checks (tradability, no duplicate open order, ownership-sized sells) now run
against the ``bot.broker`` interface, so the swing path can be exercised on
the IBKR paper account without touching the decision logic.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from bot import position_ownership
from bot.broker import create_broker
from bot.broker.base import (
    OPEN_STATES,
    Broker,
    BrokerError,
    OrderRequest,
    OrderResult,
    OwnershipBlocked,
)

log = logging.getLogger("bot.swing_orders")

REF_ID_NAMESPACE = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")

_BUY_KINDS = {"ENTRY", "ADD"}
_SELL_KINDS = {"REDUCE", "CLOSE", "STOP_TRIGGER"}

# Poll this many times (x interval) waiting for a market order to fill.
FILL_POLL_ATTEMPTS = 10
FILL_POLL_INTERVAL_S = 2.0


def resolve_sell_quantity(proposal: dict[str, Any], actual_shares: float) -> float:
    """Shares the swing strategy may sell for this proposal.

    Ownership comes from the proposal's immutable ``book_before`` snapshot
    (the executor has already removed a CLOSEd ticker from the live book by
    the time the order is placed) plus the broker-confirmed swing fills since
    that holding's first entry.  The day trader's shares in the same symbol
    are excluded; see ``position_ownership.sellable_quantity``.
    """
    ticker = str(proposal["ticker"]).upper()
    kind = proposal["signal_kind"]
    book_before = proposal.get("book_before") or {}
    book_position = book_before.get("ticker_position") or None
    virtual_est = float(proposal.get("shares_estimate") or 0.0)
    if book_position is None:
        # Legacy proposal without a snapshot: only the estimate is available.
        own = virtual_est
    else:
        # May legitimately be 0.0 (fills say the holding was already sold);
        # that is not a reason to fall back to the estimate.
        own = position_ownership.swing_owned(ticker, book_position)
    requested = own if kind in ("CLOSE", "STOP_TRIGGER") else min(virtual_est, own)
    others = position_ownership.day_owned(ticker)
    quantity, note = position_ownership.sellable_quantity(
        requested, own=own, others=others, actual=actual_shares
    )
    if note:
        log.warning("%s %s: %s", kind, ticker, note)
    log.info(
        "%s %s: virtual_est=%.6f swing_owned=%.6f day_owned=%.6f actual=%.6f -> sell=%.6f",
        kind, ticker, virtual_est, own, others, actual_shares, quantity,
    )
    return round(quantity, 6)


def proposal_ref_id(proposal: dict[str, Any]) -> str:
    ticker = proposal["ticker"]
    kind = proposal["signal_kind"]
    signal = proposal.get("signal") or {}
    msg_id = signal.get("message_id")
    dedupe_key = f"{msg_id}:{ticker}:{kind}" if msg_id else str(proposal.get("id") or "")
    return str(uuid.uuid5(REF_ID_NAMESPACE, dedupe_key))


def place_swing_order(
    proposal: dict[str, Any],
    expected_usd: float,
    broker: Broker | None = None,
) -> OrderResult:
    """Place a swing proposal and poll briefly for its fill.

    Raises BrokerError on any failure; OwnershipBlocked (a subclass) when a
    sell was refused before placement because nothing sellable belongs to
    the swing strategy.
    """
    ticker = str(proposal["ticker"]).upper()
    kind = proposal["signal_kind"]
    amount = float(proposal["usd_amount"])
    ref_id = proposal_ref_id(proposal)
    own_broker = broker is None
    broker = broker or create_broker(role="swing")

    log.info("%s: %s %s $%.4f ref_id=%s", broker.name, kind, ticker, amount, ref_id)
    try:
        account = broker.account_id()
        log.info("account: %s", account)

        # Step 1: tradability.
        tradability = broker.tradability([ticker]).get(ticker)
        if tradability is not None:
            if not tradability.tradeable:
                raise BrokerError(f"{ticker} is not tradable: state={tradability.state}")
            if kind in _BUY_KINDS and not tradability.fractional:
                raise BrokerError(
                    f"{ticker} does not support fractional/dollar-amount orders. "
                    "Increase the per-ticker budget to cover at least 1 whole share, "
                    "or exclude this ticker from automated trading."
                )

        # Step 2: no duplicate open order for the symbol.
        open_orders = [o for o in broker.open_orders(ticker) if o.state in OPEN_STATES]
        if open_orders:
            raise BrokerError(
                f"Existing open order for {ticker}: {open_orders[0].order_id} — skipping to avoid duplicate"
            )

        # Step 3: size sells from swing-owned shares, never the account total.
        request: OrderRequest
        if kind in _SELL_KINDS:
            actual_shares = broker.position_qty(ticker)
            quantity = resolve_sell_quantity(proposal, actual_shares)
            if quantity <= position_ownership.QTY_EPSILON:
                raise OwnershipBlocked(
                    f"{kind} for {ticker}: nothing sellable that belongs to the swing "
                    f"strategy (broker holds {actual_shares:.6f}, "
                    f"day trader owns {position_ownership.day_owned(ticker):.6f})"
                )
            request = OrderRequest(ticker, "sell", "market", quantity=quantity, ref_id=ref_id)
        else:
            request = OrderRequest(ticker, "buy", "market", dollar_amount=amount, ref_id=ref_id)

        # Step 4: place and poll for the fill.
        result = broker.place_order(request)
        log.info("Order placed: id=%s state=%s", result.order_id, result.state)
        for attempt in range(FILL_POLL_ATTEMPTS):
            if result.is_filled and result.fill_price:
                break
            time.sleep(FILL_POLL_INTERVAL_S)
            latest = broker.get_order(result.order_id, ticker)
            if latest is None:
                continue
            result = latest
            if result.is_filled or result.state == "partially_filled":
                if result.fill_price and result.fill_qty:
                    log.info(
                        "Order filled: id=%s price=%.4f qty=%.6f usd=%.4f (attempt %d)",
                        result.order_id, result.fill_price, result.fill_qty,
                        result.fill_usd or 0, attempt + 1,
                    )
                    break
            if result.is_terminal:
                break
        if result.fill_price is None:
            log.warning(
                "Order %s not filled within poll window (state=%s); fill price unavailable",
                result.order_id, result.state,
            )
        return result
    finally:
        if own_broker:
            broker.close()
