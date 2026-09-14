"""Acknowledged (archived) unreconciled lifecycles stop being flagged."""

from __future__ import annotations

import json
from datetime import date

from bot import acknowledgements
from bot import daily_review as dr


def test_acknowledge_roundtrip(tmp_path):
    path = tmp_path / "ack.json"
    assert acknowledgements.load(path) == {}
    entry = acknowledgements.acknowledge("pos-1", ticker="SPY", note=" sold by swing ", path=path)
    assert entry["note"] == "sold by swing"
    assert set(acknowledgements.load(path)) == {"pos-1"}


def test_review_health_skips_acknowledged(tmp_path):
    p = dr.Paths(tmp_path)
    p.positions.parent.mkdir(parents=True, exist_ok=True)
    p.positions.write_text(json.dumps({"id": "pos-1", "ticker": "SPY", "status": "unreconciled"}) + "\n"
                           + json.dumps({"id": "pos-2", "ticker": "MU", "status": "unreconciled"}) + "\n", encoding="utf-8")
    acknowledgements.acknowledge("pos-1", ticker="SPY", note=None, path=tmp_path / "state" / "acknowledged_positions.json")
    r = dr.build(date(2026, 9, 14), p)
    assert r["health"]["unreconciled"] == ["MU"]
