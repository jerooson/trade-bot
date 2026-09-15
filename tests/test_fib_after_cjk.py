"""Fibonacci ratios glued to CJK text are not price levels."""

from bot.heat_ideas import _trigger_from, parse_heat_idea


def test_fib_ratio_directly_after_cjk_is_rejected():
    assert _trigger_from("NFLX可以关注一下，需要站上fib 0.236") is None
    assert _trigger_from("ARM 需要站上 fib 1.414，目前再次受阻") is None


def test_price_after_fib_label_is_the_level():
    assert _trigger_from("ASTS站上fib 1.618 64.21了，下一个目标关注50日线") == 64.21


def test_indicator_words_after_cjk_are_ratio_context():
    assert _trigger_from("站上ema 21") is None
    assert _trigger_from("需要站上 64.2") == 64.2


def test_idea_with_only_a_ratio_is_not_auto_eligible():
    idea = parse_heat_idea("NFLX可以关注一下。今天站上了8日线和21日线，需要站上fib 0.236",
                           idea_id="n1", created_at="2026-09-14T16:33:00+00:00")
    assert idea is not None and idea["trigger_price"] is None and idea["auto_eligible"] is False


def test_bare_fib_ratio_values_are_not_levels():
    assert _trigger_from("TSLA 跌破0.886又收回，关注反弹力度") is None
    assert _trigger_from("NFLX 站上0.236,目标上方黄线") is None
    assert _trigger_from("突破0.886可以看多") is None
    assert _trigger_from("BTE 站上 5.02") == 5.02          # a real sub-$10 level survives
    assert _trigger_from("站上 1.5 的位置") == 1.5           # not a fib ratio


def test_reclaim_wording_is_a_long_level():
    idea = parse_heat_idea("QQQ 站回705，开始反弹", idea_id="r1", created_at="2026-09-15T15:05:00+00:00")
    assert idea is not None and idea["trigger_price"] == 705.0
    assert idea["direction"] == "long" and idea["trigger_operator"] == "above" and idea["auto_eligible"] is True
    assert _trigger_from("SPY 收回 770 了") == 770.0


def test_break_then_reclaim_is_traded_from_above():
    idea = parse_heat_idea("QQQ又跌破706了，必须站上去", idea_id="q1", created_at="2026-09-14T14:23:00+00:00")
    assert idea["trigger_price"] == 706.0 and idea["direction"] == "long" and idea["trigger_operator"] == "above"
    idea = parse_heat_idea("TSLA 又跌破 fib 0.886 353.19，今天收盘需要站上去", idea_id="t1", created_at="2026-09-14T14:23:00+00:00")
    assert idea["trigger_price"] == 353.19 and idea["trigger_operator"] == "above"


def test_plain_break_without_reclaim_stays_below():
    idea = parse_heat_idea("SMH 跌破 554.66 可以考虑做空", idea_id="s1", created_at="2026-09-14T14:23:00+00:00")
    assert idea["trigger_price"] == 554.66 and idea["trigger_operator"] == "below" and idea["direction"] == "short"
