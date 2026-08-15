import pytest

from sports_ai.agents.market.devig import (
    consensus,
    devig_power,
    devig_proportional,
    devig_shin,
    overround,
)

ODDS_2WAY = [1.62, 2.35]
ODDS_3WAY = [2.10, 3.40, 3.80]


@pytest.mark.parametrize("fn", [devig_proportional, devig_power, devig_shin])
@pytest.mark.parametrize("odds", [ODDS_2WAY, ODDS_3WAY])
def test_devig_sums_to_one(fn, odds):
    ps = fn(odds)
    assert abs(sum(ps) - 1.0) < 1e-6
    assert all(0 < p < 1 for p in ps)


def test_devig_preserves_order():
    ps = devig_shin(ODDS_3WAY)
    assert ps[0] > ps[1] > ps[2]


def test_shin_favourite_longshot():
    # Shin should give the longshot LESS probability than proportional devig.
    prop = devig_proportional([1.20, 9.0])
    shin = devig_shin([1.20, 9.0])
    assert shin[1] <= prop[1] + 1e-9


def test_overround_positive():
    assert overround(ODDS_2WAY) > 0


def test_consensus_weighting():
    books = {"sharp": [1.70, 2.30], "soft": [1.55, 2.50]}
    tiers = {"sharp": "sharp", "soft": "soft"}
    cons = consensus(books, tiers)
    sharp_p = devig_shin([1.70, 2.30])[0]
    soft_p = devig_shin([1.55, 2.50])[0]

    # Consensus must sit closer to the sharp book.
    assert abs(cons[0] - sharp_p) < abs(cons[0] - soft_p)
