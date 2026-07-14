"""Persistent user-created day-trade watch plans.

The dashboard API is the only writer.  The day-trader process reads the file
and derives execution state from its own persisted positions, which avoids a
cross-process read/modify/write race.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_PATH = Path("state/manual_day_trade_plans.json")
MAX_ACTIVE_PLANS = 25
_WRITE_LOCK = threading.Lock()


def load_plans(path: Path = DEFAULT_PATH) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def _write_plans(path: Path, plans: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(plans, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def create_plan(
    ticker: str,
    trigger_price: float,
    target_price: float | None = None,
    setup: str | None = None,
    *,
    path: Path = DEFAULT_PATH,
    consumed_plan_ids: set[str] | None = None,
) -> dict[str, Any]:
    consumed = consumed_plan_ids or set()
    with _WRITE_LOCK:
        plans = load_plans(path)
        ticker = ticker.upper()
        active = [
            item for item in plans
            if item.get("status") == "active" and item.get("id") not in consumed
        ]
        if len(active) >= MAX_ACTIVE_PLANS:
            raise ValueError(f"maximum {MAX_ACTIVE_PLANS} active manual watches")
        if any(str(item.get("ticker", "")).upper() == ticker for item in active):
            raise ValueError(f"an active manual watch already exists for {ticker}")
        now = datetime.now(timezone.utc).isoformat()
        plan = {
            "id": str(uuid.uuid4()),
            "ticker": ticker,
            "trigger_price": float(trigger_price),
            "target_price": float(target_price) if target_price is not None else None,
            "setup": setup.strip() if setup and setup.strip() else "Manual breakout watch",
            "status": "active",
            "created_at": now,
            "updated_at": now,
            "cancelled_at": None,
        }
        plans.append(plan)
        _write_plans(path, plans)
        return plan


def cancel_plan(plan_id: str, *, path: Path = DEFAULT_PATH) -> dict[str, Any] | None:
    with _WRITE_LOCK:
        plans = load_plans(path)
        found: dict[str, Any] | None = None
        now = datetime.now(timezone.utc).isoformat()
        for plan in plans:
            if plan.get("id") != plan_id:
                continue
            found = plan
            if plan.get("status") == "active":
                plan["status"] = "cancelled"
                plan["cancelled_at"] = now
                plan["updated_at"] = now
            break
        if found is not None:
            _write_plans(path, plans)
        return found
