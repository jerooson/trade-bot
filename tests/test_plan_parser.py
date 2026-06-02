"""Tests for the swing-trade plan parser, using verbatim channel fixtures."""

from bot.plan_parser import TradePlan, parse_plan
from tests import fixtures_plans as f


def test_akam_ticker_and_range_levels():
    plan = parse_plan(f.PLAN_AKAM, embed_title=f.PLAN_AKAM_EMBED_TITLE)
    assert plan is not None
    assert plan.ticker == "AKAM"
    # "150-151附近区间" -> both endpoints captured
    assert 150.0 in plan.watch_levels
    assert 151.0 in plan.watch_levels
    # Indicator number "8EMA" must NOT show up as a price level.
    assert 8.0 not in plan.watch_levels
    assert plan.chart_url == "https://www.tradingview.com/x/jWs91aoz/"
    # Glossary parsed.
    assert "8EMA" in plan.glossary
    assert "盘整" in plan.glossary
    # Boilerplate footer stripped.
    assert "@everyone" not in plan.narrative
    assert "以上为个人观点分享" not in plan.narrative
    assert plan.is_actionable


def test_okta_single_level():
    plan = parse_plan(f.PLAN_OKTA, embed_title=f.PLAN_OKTA_EMBED_TITLE)
    assert plan is not None
    assert plan.ticker == "OKTA"
    assert 127.57 in plan.watch_levels
    assert plan.chart_url == "https://www.tradingview.com/x/FSxn85uC/"
    assert plan.is_actionable


def test_joby_two_levels():
    plan = parse_plan(f.PLAN_JOBY, embed_title=f.PLAN_JOBY_EMBED_TITLE)
    assert plan is not None
    assert plan.ticker == "JOBY"
    assert 11.38 in plan.watch_levels
    assert 13.38 in plan.watch_levels


def test_qcom_invalidation_level():
    plan = parse_plan(f.PLAN_QCOM, embed_title=f.PLAN_QCOM_EMBED_TITLE)
    assert plan is not None
    assert plan.ticker == "QCOM"
    # The downside invalidation level still counts as a watch level.
    assert 191.0 in plan.watch_levels
    # 8EMA -> not a level
    assert 8.0 not in plan.watch_levels


def test_meta_minimal_no_glossary():
    plan = parse_plan(f.PLAN_META, embed_title=f.PLAN_META_EMBED_TITLE)
    assert plan is not None
    assert plan.ticker == "META"
    # No explicit price in the body -> empty levels but plan still returned.
    assert plan.watch_levels == []
    assert plan.chart_url == "https://www.tradingview.com/x/ReKaEvbf/"
    # An incomplete plan is NOT actionable.
    assert plan.is_actionable is False


def test_noise_no_chart_rejected():
    assert parse_plan(f.PLAN_NOISE_NO_CHART) is None
    assert parse_plan(f.PLAN_NOISE_REPLY) is None


def test_empty_input():
    assert parse_plan("") is None
    assert parse_plan("   \n  ") is None


def test_to_dict_is_json_safe():
    import json
    plan = parse_plan(f.PLAN_OKTA, embed_title=f.PLAN_OKTA_EMBED_TITLE)
    d = plan.to_dict()
    json.dumps(d)
    assert d["ticker"] == "OKTA"
    assert isinstance(d["watch_levels"], list)
    assert isinstance(d["received_at"], str)


def test_embed_title_takes_precedence_over_body():
    """If the body's first uppercase token is misleading, embed wins."""
    body = "RIVN 纯粹是技术性突破... 触发位置在16.60左右。 https://www.tradingview.com/x/HBngI1KF/"
    plan = parse_plan(body, embed_title="BATS:RIVN Chart Image by PaxisTrading")
    assert plan is not None
    assert plan.ticker == "RIVN"
    assert 16.60 in plan.watch_levels


def test_no_embed_falls_back_to_body():
    body = "AAPL has reclaimed the 8EMA at 220.50, watching for breakout. https://www.tradingview.com/x/abc123/"
    plan = parse_plan(body)
    assert plan is not None
    assert plan.ticker == "AAPL"
    assert 220.50 in plan.watch_levels
