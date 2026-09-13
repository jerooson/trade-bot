"""MU routes to its 2x ETFs like the other mapped names."""

from bot.leveraged_etfs import candidate_symbols, leveraged_candidates


def test_mu_maps_to_muu_and_muz():
    assert [c.ticker for c in leveraged_candidates("MU", "long")] == ["MUU"]
    assert [c.ticker for c in leveraged_candidates("MU", "short")] == ["MUZ"]
    assert leveraged_candidates("MU", "long")[0].leverage == 2.0
    assert "MUU" in candidate_symbols("MU", "long")
