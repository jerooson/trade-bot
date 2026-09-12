"""Connectivity check for the configured broker (paper-safe by default).

    python -m bot.broker.smoke                 # account, quotes, positions, open orders
    python -m bot.broker.smoke --symbols SPY SPXL
    python -m bot.broker.smoke --round-trip SPXL --usd 5   # buy $5 then sell it (IBKR paper only)

The round trip refuses to run unless BROKER=ibkr and the port is a paper
port (4002 gateway / 7497 TWS), so it can never touch a live account.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time

from dotenv import load_dotenv

from bot.broker import broker_name, create_broker
from bot.broker.base import OrderRequest

PAPER_PORTS = {4002, 7497}


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Broker connectivity smoke test")
    parser.add_argument("--symbols", nargs="*", default=["SPY", "SPXL", "QQQ"])
    parser.add_argument("--round-trip", metavar="SYMBOL", help="buy then sell a tiny notional (paper only)")
    parser.add_argument("--usd", type=float, default=5.0)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    name = broker_name()
    print(f"broker: {name}")
    broker = create_broker(name, role="smoke")
    try:
        account = broker.account_id()
        print(f"account: {account}")
        quotes = broker.quotes([s.upper() for s in args.symbols])
        for symbol in args.symbols:
            q = quotes.get(symbol.upper())
            if q is None:
                print(f"quote {symbol}: (none)")
                continue
            spread = f"{q.spread_pct:.3f}%" if q.spread_pct is not None else "n/a"
            print(f"quote {q.symbol}: last={q.last} bid={q.bid} ask={q.ask} spread={spread} volume={q.volume}")
        print("tradability:", {s: (t.tradeable, t.fractional) for s, t in broker.tradability([s.upper() for s in args.symbols]).items()})
        positions = broker.positions()
        print(f"positions ({len(positions)}):")
        for p in positions.values():
            print(f"  {p.symbol} qty={p.quantity} avg_cost={p.avg_cost}")
        open_orders = broker.open_orders()
        print(f"open orders ({len(open_orders)}):")
        for o in open_orders:
            print(f"  {o.order_id} {o.symbol} {o.side} state={o.state} filled={o.fill_qty}")

        if args.round_trip:
            port = int(os.environ.get("IBKR_PORT", "4002"))
            # IBKR paper account ids start with "DU"; check the live session,
            # not only the env, so a remapped port cannot reach a real account.
            if name != "ibkr" or port not in PAPER_PORTS or not str(account).upper().startswith("DU"):
                print("refusing round trip: only allowed with BROKER=ibkr on a paper port (4002/7497) "
                      f"and a paper account (DU...), got account {account}")
                return 2
            symbol = args.round_trip.upper()
            ref = f"smoke:{int(time.time())}"
            print(f"round trip: buying ${args.usd:.2f} of {symbol} ...")
            buy = broker.place_order(OrderRequest(symbol, "buy", "market", dollar_amount=args.usd, ref_id=ref + ":buy"))
            print(f"  buy order {buy.order_id} state={buy.state}")
            for _ in range(15):
                time.sleep(2)
                buy = broker.get_order(buy.order_id, symbol) or buy
                if buy.is_terminal:
                    break
            print(f"  buy final state={buy.state} qty={buy.fill_qty} price={buy.fill_price}")
            if not buy.fill_qty:
                print("  no fill (market closed or fractional not enabled); nothing to sell")
                return 1
            sell = broker.place_order(OrderRequest(symbol, "sell", "market", quantity=buy.fill_qty, ref_id=ref + ":sell"))
            for _ in range(15):
                time.sleep(2)
                sell = broker.get_order(sell.order_id, symbol) or sell
                if sell.is_terminal:
                    break
            print(f"  sell final state={sell.state} qty={sell.fill_qty} price={sell.fill_price}")
        return 0
    finally:
        broker.close()


if __name__ == "__main__":
    sys.exit(main())
