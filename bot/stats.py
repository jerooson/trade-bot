"""
Quick statistics over a parsed signal log (JSONL).

Run:
    python -m bot.stats logs/history.jsonl
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit("Usage: python -m bot.stats <path-to.jsonl>")
    path = Path(sys.argv[1])
    if not path.exists():
        sys.exit(f"File not found: {path}")

    by_kind: Counter[str] = Counter()
    by_kind_ticker: dict[str, Counter[str]] = defaultdict(Counter)
    triggers_per_ticker: dict[str, list[float]] = defaultdict(list)
    sides: Counter[str] = Counter()
    earliest: datetime | None = None
    latest: datetime | None = None
    has_target = 0
    no_target = 0

    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            kind = r["kind"]
            ticker = r["ticker"]
            by_kind[kind] += 1
            by_kind_ticker[kind][ticker] += 1
            if r.get("side"):
                sides[r["side"]] += 1
            if kind == "TRIGGER" and r.get("trigger") is not None:
                triggers_per_ticker[ticker].append(r["trigger"])
            if kind in ("PLAN", "TRIGGER"):
                if r.get("target") is not None:
                    has_target += 1
                else:
                    no_target += 1
            ts = r.get("discord", {}).get("created_at")
            if ts:
                dt = datetime.fromisoformat(ts)
                earliest = dt if earliest is None or dt < earliest else earliest
                latest = dt if latest is None or dt > latest else latest

    total = sum(by_kind.values())
    print(f"\nTotal parsed signals : {total}")
    if earliest and latest:
        print(f"Time range           : {earliest.isoformat()}  to  {latest.isoformat()}")
    print(f"Side distribution    : {dict(sides)}")
    print(f"Has target           : {has_target}  (PLAN/TRIGGER only)")
    print(f"No target            : {no_target}  (PLAN/TRIGGER only)")

    print("\nBy kind:")
    for k, n in by_kind.most_common():
        print(f"  {k:8s} {n}")

    print("\nUnique tickers per kind:")
    for k, c in by_kind_ticker.items():
        print(f"  {k:8s} {len(c)} unique  top5={c.most_common(5)}")

    if triggers_per_ticker:
        print("\nTRIGGER posts per ticker (top 10):")
        sorted_triggers = sorted(
            triggers_per_ticker.items(), key=lambda x: -len(x[1])
        )
        for ticker, prices in sorted_triggers[:10]:
            distinct_prices = sorted(set(prices))
            print(f"  {ticker:6s} fired {len(prices)} time(s) at price(s) {distinct_prices}")


if __name__ == "__main__":
    main()
