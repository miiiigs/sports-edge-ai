from __future__ import annotations

from decimal import Decimal

import pytest

from sports_ai.staking import quarter_kelly_stake_fraction


def test_quarter_kelly_zero_at_break_even():
    stake = quarter_kelly_stake_fraction(
        probability=Decimal("0.50"),
        decimal_price=Decimal("2.00"),
    )
    assert stake.full_kelly_fraction == Decimal("0.000000")
    assert stake.quarter_kelly_fraction == Decimal("0.000000")
    assert stake.suggested_stake_fraction == Decimal("0.000000")


def test_quarter_kelly_positive_below_cap():
    stake = quarter_kelly_stake_fraction(
        probability=Decimal("0.525"),
        decimal_price=Decimal("2.00"),
    )
    assert stake.full_kelly_fraction == Decimal("0.050000")
    assert stake.quarter_kelly_fraction == Decimal("0.012500")
    assert stake.suggested_stake_fraction == Decimal("0.012500")


def test_quarter_kelly_caps_at_two_percent():
    stake = quarter_kelly_stake_fraction(
        probability=Decimal("0.55"),
        decimal_price=Decimal("2.00"),
    )
    assert stake.full_kelly_fraction == Decimal("0.100000")
    assert stake.quarter_kelly_fraction == Decimal("0.025000")
    assert stake.suggested_stake_fraction == Decimal("0.020000")


def test_quarter_kelly_negative_edge_is_zero():
    stake = quarter_kelly_stake_fraction(
        probability=Decimal("0.49"),
        decimal_price=Decimal("2.00"),
    )
    assert stake.suggested_stake_fraction == Decimal("0.000000")


def test_quarter_kelly_rejects_invalid_inputs():
    with pytest.raises(ValueError, match="greater than 1"):
        quarter_kelly_stake_fraction(probability=Decimal("0.50"), decimal_price=Decimal("1.00"))
    with pytest.raises(ValueError, match=r"\[0, 1\]"):
        quarter_kelly_stake_fraction(probability=Decimal("1.01"), decimal_price=Decimal("2.00"))
