from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from server import api


@pytest.fixture
def workspace_tmp():
    path = Path.cwd() / ".test-artifacts" / str(uuid.uuid4())
    path.mkdir(parents=True)
    try:
        yield path
    finally:
        shutil.rmtree(path)
        try:
            path.parent.rmdir()
        except OSError:
            pass


def _client(monkeypatch, workspace_tmp):
    plans_path = workspace_tmp / "manual" / "plans.json"
    positions_path = workspace_tmp / "positions.jsonl"
    monkeypatch.setattr(api, "MANUAL_DAY_TRADE_PLANS_PATH", plans_path)
    monkeypatch.setattr(api, "DAY_TRADE_POSITIONS_PATH", positions_path)
    return TestClient(api.app), plans_path, positions_path


def test_manual_plan_create_duplicate_and_cancel(monkeypatch, workspace_tmp):
    client, _, _ = _client(monkeypatch, workspace_tmp)
    response = client.post("/api/daytrader/manual-plans", json={
        "ticker": "gtlb",
        "trigger_price": 34.06,
        "target_price": None,
        "setup": "Breakout",
    })
    assert response.status_code == 200
    plan = response.json()
    assert plan["ticker"] == "GTLB"
    assert plan["derived_status"] == "queued"

    duplicate = client.post("/api/daytrader/manual-plans", json={
        "ticker": "GTLB", "trigger_price": 35,
    })
    assert duplicate.status_code == 409

    cancelled = client.delete(f"/api/daytrader/manual-plans/{plan['id']}")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"


def test_executed_plan_allows_new_watch_for_same_ticker(monkeypatch, workspace_tmp):
    client, _, positions_path = _client(monkeypatch, workspace_tmp)
    first = client.post("/api/daytrader/manual-plans", json={
        "ticker": "AAPL", "trigger_price": 100,
    }).json()
    positions_path.write_text(json.dumps({
        "id": "position-1",
        "ticker": "AAPL",
        "manual_plan_id": first["id"],
        "status": "closed",
        "fill_qty": 0.5,
    }) + "\n", encoding="utf-8")

    second = client.post("/api/daytrader/manual-plans", json={
        "ticker": "AAPL", "trigger_price": 101,
    })
    assert second.status_code == 200
    assert second.json()["id"] != first["id"]

    cannot_cancel_executed = client.delete(
        f"/api/daytrader/manual-plans/{first['id']}"
    )
    assert cannot_cancel_executed.status_code == 409
