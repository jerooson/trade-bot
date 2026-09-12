"""Broker factory: ``BROKER=robinhood`` (default) or ``BROKER=ibkr``."""

from __future__ import annotations

import os

from bot.broker.base import (  # noqa: F401 - re-exported for callers
    Broker,
    BrokerError,
    OrderRejected,
    OrderRequest,
    OrderResult,
    OwnershipBlocked,
    Position,
    Quote,
    ShareShortfall,
    Tradability,
)

SUPPORTED = ("robinhood", "ibkr")


def broker_name() -> str:
    name = (os.environ.get("BROKER") or "robinhood").strip().lower()
    if name not in SUPPORTED:
        raise BrokerError(f"unsupported BROKER={name!r}; expected one of {SUPPORTED}")
    return name


# Each process that talks to IB Gateway needs its own client id.  The role
# picks a stable offset from IBKR_CLIENT_ID (or IBKR_CLIENT_ID_<ROLE> wins).
CLIENT_ID_OFFSETS = {"day_trader": 0, "swing": 1, "smoke": 2, "default": 3}


def ibkr_client_id(role: str = "default") -> int:
    explicit = os.environ.get(f"IBKR_CLIENT_ID_{role.upper()}")
    try:
        if explicit:
            return int(explicit)
        base = int(os.environ.get("IBKR_CLIENT_ID", "17"))
    except ValueError as exc:
        raise BrokerError(f"IBKR client id must be an integer: {exc}") from exc
    return base + CLIENT_ID_OFFSETS.get(role, CLIENT_ID_OFFSETS["default"])


def create_broker(name: str | None = None, *, role: str = "default") -> Broker:
    """Connect to the configured broker and return an adapter.

    ``role`` identifies the calling service (``day_trader``, ``swing``,
    ``smoke``) so concurrent IBKR sessions get distinct client ids.
    """
    name = (name or broker_name()).lower()
    if name == "ibkr":
        from bot.broker.ibkr import IBKRBroker
        return IBKRBroker.from_env(client_id=ibkr_client_id(role))
    from bot.broker.robinhood import RobinhoodBroker
    return RobinhoodBroker.connect()
