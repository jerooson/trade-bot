from __future__ import annotations

import shutil
import uuid
from pathlib import Path
from unittest.mock import patch

import bot.manual_day_plans as registry
import pytest


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


def test_create_load_cancel_round_trip(workspace_tmp):
    path = workspace_tmp / "manual" / "plans.json"
    created = registry.create_plan(
        "gtlb", 34.06, setup=" Breakout ", path=path
    )

    plans = registry.load_plans(path)
    assert plans == [created]
    assert created["ticker"] == "GTLB"
    assert created["setup"] == "Breakout"

    cancelled = registry.cancel_plan(created["id"], path=path)
    assert cancelled is not None
    assert cancelled["status"] == "cancelled"
    assert registry.load_plans(path)[0]["cancelled_at"] is not None


def test_duplicate_active_ticker_is_rejected(workspace_tmp):
    path = workspace_tmp / "plans.json"
    registry.create_plan("AAPL", 100, path=path)
    try:
        registry.create_plan("aapl", 101, path=path)
    except ValueError as exc:
        assert "already exists" in str(exc)
    else:
        raise AssertionError("duplicate ticker should be rejected")


def test_consumed_plan_does_not_count_against_limit_or_duplicate(workspace_tmp):
    path = workspace_tmp / "plans.json"
    first = registry.create_plan("AAPL", 100, path=path)
    with patch.object(registry, "MAX_ACTIVE_PLANS", 1):
        second = registry.create_plan(
            "AAPL", 101, path=path, consumed_plan_ids={first["id"]}
        )
    assert second["id"] != first["id"]
