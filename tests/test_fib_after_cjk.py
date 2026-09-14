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
