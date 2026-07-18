from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bot import heat_chart_analyzer as analyzer


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


def _pltr_idea() -> dict:
    return {
        "id": "pltr-1",
        "ticker": "PLTR",
        "trigger_price": None,
        "target_price": None,
        "direction": "long",
        "trigger_operator": "below",
        "classification": "needs_level",
        "setup": "PLTR 值得关注。很多次跌破MA cluster当日都能收回",
        "attachments": ["pltr.png"],
    }


def test_pltr_chart_result_overrides_historical_below_word():
    approved, reason, trigger, operator, confidence = analyzer.validate_analysis(
        _pltr_idea(),
        {
            "ticker": "PLTR",
            "trigger_price": 138.9,
            "trigger_operator": "above",
            "confidence": 0.88,
            "rationale": "yellow horizontal resistance",
        },
    )
    assert approved is True
    assert reason == "approved"
    assert trigger == 138.9
    assert operator == "above"
    assert confidence == 0.88


def test_low_confidence_chart_stays_manual_review():
    approved, reason, *_ = analyzer.validate_analysis(
        _pltr_idea(),
        {
            "ticker": "PLTR",
            "trigger_price": 138.9,
            "trigger_operator": "above",
            "confidence": 0.5,
        },
    )
    assert approved is False
    assert reason == "low_confidence"


def test_parse_analysis_output_accepts_final_json_line():
    parsed = analyzer.parse_analysis_output(
        "progress\n" + json.dumps({
            "ticker": "PLTR", "trigger_price": 138.9,
            "trigger_operator": "above", "confidence": 0.88,
        })
    )
    assert parsed["trigger_price"] == 138.9


def test_parse_analysis_output_accepts_fenced_pretty_json():
    parsed = analyzer.parse_analysis_output(
        "```json\n{\n  \"ticker\": \"PLTR\",\n"
        "  \"trigger_price\": 138.9,\n"
        "  \"trigger_operator\": \"above\",\n"
        "  \"confidence\": 0.88\n}\n```"
    )
    assert parsed["ticker"] == "PLTR"


def test_successful_analysis_persists_approval_once(workspace_tmp, monkeypatch):
    image = workspace_tmp / "pltr.png"
    image.write_bytes(b"image")
    decisions = workspace_tmp / "decisions.jsonl"
    analyses = workspace_tmp / "analyses.jsonl"
    monkeypatch.setattr(analyzer, "HEAT_ATTACHMENTS_DIR", workspace_tmp)
    monkeypatch.setattr(analyzer, "HEAT_DECISIONS_PATH", decisions)
    monkeypatch.setattr(analyzer, "ANALYSES_PATH", analyses)
    result = MagicMock(
        returncode=0,
        stdout=json.dumps({
            "ticker": "PLTR", "trigger_price": 138.9,
            "trigger_operator": "above", "confidence": 0.88,
            "rationale": "yellow resistance",
        }),
    )
    with patch.object(analyzer, "invoke_codex", return_value=result) as invoke:
        complete, reason = analyzer.analyze_one(_pltr_idea())

    assert complete is True and reason == "approved"
    invoke.assert_called_once()
    decision = json.loads(decisions.read_text(encoding="utf-8"))
    assert decision["trigger_price"] == 138.9
    assert decision["trigger_operator"] == "above"
    assert decision["source"] == "heat_chart_analyzer"
    assert decision["good_til_cancelled"] is True
    audit = json.loads(analyses.read_text(encoding="utf-8"))
    assert audit["status"] == "approved"
