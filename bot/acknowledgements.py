"""Operator acknowledgements for day-trade lifecycles that need no further action.

An ``unreconciled`` position (shares sold by another strategy, nothing left to
sell) stays in the ledger as history, but once the owner has read it there is
no reason to keep flagging it on the dashboard and in the daily review.  The
dashboard API is the only writer of this file; the day trader never reads it.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_PATH = Path("state/acknowledged_positions.json")
_LOCK = threading.Lock()


def load(path: Path = DEFAULT_PATH) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def acknowledge(position_id: str, *, ticker: str | None, note: str | None, path: Path = DEFAULT_PATH) -> dict[str, Any]:
    with _LOCK:
        data = load(path)
        entry = {
            "position_id": position_id,
            "ticker": ticker,
            "note": (note or "").strip() or None,
            "acknowledged_at": datetime.now(timezone.utc).isoformat(),
        }
        data[position_id] = entry
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=1)
            os.replace(tmp, path)
        finally:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
        return entry
