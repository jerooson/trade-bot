"""Reconciled performance report over synthetic ledgers."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from bot import performance


def _pnl_rows() -> list[dict]:
    return [
        # SPXL: swing owns 0.035107, then sells 0.105993 (the day trader's 0.070886 too).
        {"timestamp": "2026-09-02T14:19:24+00:00", "ticker": "SPXL", "kind": "ENTRY", "action": "BUY",
         "order_id": "b2", "fill_price": 284.8423, "fill_qty": 0.035107},
        {"timestamp": "2026-09-02T17:22:45+00:00", "ticker": "SPXL", "kind": "STOP_TRIGGER", "action": "SELL",
         "order_id": "s2", "fill_price": 284.4425, "fill_qty": 0.105993, "realized_pnl": -0.0424},
        # A clean swing round in August.
        {"timestamp": "2026-08-10T14:00:00+00:00", "ticker": "AAPL", "kind": "ENTRY", "action": "BUY",
         "order_id": "b3", "fill_price": 200.0, "fill_qty": 0.05},
        {"timestamp": "2026-08-12T14:00:00+00:00", "ticker": "AAPL", "kind": "CLOSE", "action": "SELL",
         "order_id": "s3", "fill_price": 210.0, "fill_qty": 0.05, "realized_pnl": 0.5},
        # A sell whose fill never came back.
        {"timestamp": "2026-09-02T20:09:01+00:00", "ticker": "SNOW", "kind": "CLOSE", "action": "SELL",
         "order_id": "s4", "fill_price": None, "fill_qty": None, "realized_pnl": None},
    ]


def _day_positions() -> list[dict]:
    return [
        {"id": "day-spxl", "ticker": "SPY", "execution_ticker": "SPXL", "execution_leverage": 3.0,
         "source": "heat", "status": "pending_exit", "exit_reason": "eod",
         "fill_price": 282.1399, "fill_qty": 0.070886, "exit_filled_qty": 0.0, "exit_filled_value": 0.0,
         "entered_at": "2026-09-02T13:30:06+00:00", "exit_last_error": "Not enough shares to sell."},
        {"id": "day-nvda", "ticker": "NVDA", "source": "discord", "status": "closed", "exit_reason": "stop",
         "fill_price": 100.0, "fill_qty": 0.2, "exit_filled_qty": 0.2, "exit_filled_value": 19.6,
         "realized_pnl": -0.4, "closed_at": "2026-08-20T19:00:00+00:00"},
        {"id": "day-tsla", "ticker": "TSLA", "source": "manual", "status": "closed", "exit_reason": "eod",
         "fill_price": 50.0, "fill_qty": 0.4, "exit_filled_qty": 0.4, "exit_filled_value": 20.12,
         "realized_pnl": 0.12, "closed_at": "2026-09-11T19:50:00+00:00"},
        {"id": "day-open", "ticker": "AMD", "source": "discord", "status": "open",
         "fill_price": 150.0, "fill_qty": 0.1, "exit_filled_qty": 0.0, "current_price": 151.0},
        {"id": "day-watch", "ticker": "META", "source": "discord", "status": "watching"},
    ]


@pytest.fixture
def report() -> dict:
    return performance.build_report(
        positions=_day_positions(),
        pnl_records=_pnl_rows(),
        review_rows=[
            {"dedupe_key": "k1", "status": "PENDING", "ticker": "SNOW", "signal_kind": "CLOSE"},
            {"dedupe_key": "k1", "status": "UNVERIFIED", "ticker": "SNOW", "signal_kind": "CLOSE",
             "rationale": "existing open order"},
            {"dedupe_key": "k2", "status": "PLACED", "ticker": "AAPL", "signal_kind": "ENTRY"},
        ],
        now=datetime(2026, 9, 11, 23, 0, tzinfo=timezone.utc),
    )


def test_oversold_swing_fill_is_reallocated_to_the_day_trade(report):
    assert len(report["reallocations"]) == 1
    realloc = report["reallocations"][0]
    assert realloc["day_position_id"] == "day-spxl"
    assert realloc["qty"] == pytest.approx(0.070886)
    assert realloc["day_realized_pnl"] == pytest.approx((284.4425 - 282.1399) * 0.070886, abs=1e-4)

    spxl_sell = next(r for r in report["swing"]["sells"] if r["ticker"] == "SPXL")
    assert spxl_sell["reallocated"] is True
    assert spxl_sell["swing_qty"] == pytest.approx(0.035107)
    assert spxl_sell["realized_pnl"] == pytest.approx((284.4425 - 284.8423) * 0.035107, abs=1e-4)
    assert spxl_sell["recorded_realized_pnl"] == -0.0424

    day_spxl = next(r for r in report["day"]["closed"] if r["id"] == "day-spxl")
    assert day_spxl["reallocated"] is True
    assert "sold by swing strategy" in day_spxl["exit_reason"]
    assert not any(o["kind"] == "swing_oversale_unallocated" for o in report["omissions"])


def test_scopes_and_groupings_are_explicit(report):
    day = report["day"]
    assert day["all_time"]["count"] == 3          # nvda, tsla, reallocated spxl
    assert day["today"]["count"] == 1             # tsla closed 2026-09-11 ET
    assert day["today"]["net"] == pytest.approx(0.12)
    assert set(day["by_source"]) == {"discord", "manual", "heat"}
    assert day["by_source"]["discord"]["net"] == pytest.approx(-0.4)
    assert day["by_month"]["2026-08"]["count"] == 1
    assert day["by_month"]["2026-09"]["count"] == 2
    assert day["heat_by_leverage"]["leveraged"]["count"] == 1
    assert day["heat_by_leverage"]["unleveraged"]["count"] == 0

    swing = report["swing"]
    assert swing["all_time"]["count"] == 2
    assert swing["all_time"]["net"] == pytest.approx(0.5 + (284.4425 - 284.8423) * 0.035107, abs=1e-4)
    assert swing["recorded_all_time"] == pytest.approx(0.5 - 0.0424)
    assert swing["reviews"]["latest_status_counts"] == {"PLACED": 1, "UNVERIFIED": 1}

    totals = report["totals"]
    assert totals["combined_realized"] == pytest.approx(totals["day_realized"] + totals["swing_realized_adjusted"])


def test_open_and_missing_data_are_reported_not_summed(report):
    day = report["day"]
    assert len(day["open"]) == 1
    assert day["open"][0]["ticker"] == "AMD"
    assert day["open"][0]["unrealized_pnl"] == pytest.approx(0.1)
    assert day["open_unrealized_pnl"] == pytest.approx(0.1)

    kinds = [o["kind"] for o in report["omissions"]]
    assert "swing_sell_without_fill" in kinds        # SNOW sell with no fill
    assert "review_unverified" in kinds              # SNOW UNVERIFIED review row
    assert report["totals"]["omission_count"] == len(report["omissions"])
    # Realized totals exclude the unknown SNOW sale rather than counting it as 0.
    assert all(r["ticker"] != "SNOW" for r in report["swing"]["sells"])


def test_unreconciled_day_exit_without_matching_sale_is_an_omission():
    positions = [{
        "id": "day-x", "ticker": "SPY", "execution_ticker": "SPXL", "source": "heat",
        "status": "unreconciled", "fill_price": 282.0, "fill_qty": 0.07, "exit_filled_qty": 0.0,
        "unreconciled_qty": 0.07, "reconciliation_note": "broker holds 0",
    }]
    report = performance.build_report(positions=positions, pnl_records=[], review_rows=[])
    assert report["day"]["all_time"]["count"] == 0
    assert [o["kind"] for o in report["omissions"]] == ["day_unreconciled_exit"]


def test_partial_unreconciled_close_counts_only_sold_shares():
    positions = [{
        "id": "day-p", "ticker": "X", "source": "manual", "status": "closed", "exit_reason": "stop",
        "fill_price": 10.0, "fill_qty": 2.0, "exit_filled_qty": 1.5, "exit_filled_value": 14.7,
        "unreconciled_qty": 0.5, "realized_pnl": -0.3, "closed_at": "2026-09-01T19:00:00+00:00",
        "reconciliation_note": "0.5 lost",
    }]
    report = performance.build_report(positions=positions, pnl_records=[], review_rows=[])
    row = report["day"]["closed"][0]
    assert row["realized_pnl"] == pytest.approx(14.7 - 15.0)
    assert row["partial"] is True
    assert [o["kind"] for o in report["omissions"]] == ["day_partial_unreconciled"]


def test_text_rendering_mentions_reallocation_and_caveats(report):
    text = performance.format_report(report)
    assert "FILL RE-ALLOCATIONS" in text
    assert "SPXL" in text
    assert "OMISSIONS" in text
    assert "not broker statements" in text


def _write_jsonl(path, rows):
    import json
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def test_api_exposes_scoped_day_pnl_and_performance_report(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from server import api

    positions_path = tmp_path / "logs" / "day_trade_positions.jsonl"
    _write_jsonl(positions_path, _day_positions())
    _write_jsonl(tmp_path / "logs" / "trade_pnl.jsonl", _pnl_rows())
    _write_jsonl(tmp_path / "logs" / "reviews.jsonl", [])
    monkeypatch.setattr(api, "DAY_TRADE_POSITIONS_PATH", positions_path)
    monkeypatch.setattr(api, "PNL_PATH", tmp_path / "logs" / "trade_pnl.jsonl")
    monkeypatch.setattr(api, "SHADOW_REVIEWS_PATH", tmp_path / "logs" / "reviews.jsonl")
    monkeypatch.setattr(api, "MANUAL_DAY_TRADE_PLANS_PATH", tmp_path / "state" / "plans.json")
    monkeypatch.setattr(api, "HEAT_IDEAS_PATH", tmp_path / "logs" / "heat.jsonl")
    monkeypatch.setattr(api, "HEAT_DECISIONS_PATH", tmp_path / "state" / "decisions.jsonl")
    monkeypatch.setattr(api, "HEAT_SETTINGS_PATH", tmp_path / "state" / "settings.json")
    monkeypatch.setattr(api, "DAY_TRADER_PID_PATH", tmp_path / "logs" / "pid")
    client = TestClient(api.app)

    day = client.get("/api/daytrader").json()["pnl"]
    assert day["scope"] == "today"
    assert day["all_time"]["trades"] == 2
    assert day["all_time"]["total_realized_pnl"] == pytest.approx(-0.28)
    assert day["open_unrealized_pnl"] == pytest.approx(0.1)
    assert [row["ticker"] for row in day["omissions"]["stuck_exits"]] == ["SPY"]
    assert len(day["records"]) == 2

    swing = client.get("/api/pnl").json()
    assert swing["sells_missing_realized_pnl"] == 1
    assert "unadjusted" in swing["scope"]

    report = client.get("/api/performance").json()
    assert report["day"]["all_time"]["count"] == 3
    assert report["reallocations"][0]["day_position_id"] == "day-spxl"
    assert report["totals"]["omission_count"] >= 1
