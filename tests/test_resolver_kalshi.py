"""derive_pl_from_payout — Kalshi / manual payout path."""

import pytest

from resolver import derive_pl_from_payout


@pytest.mark.parametrize(
    "payout,stake,fee,category,expected_pl",
    [
        pytest.param(190.9, 100, 0, "Standard", 90.9, id="standard_win"),
        pytest.param(150.0, 100, 2.5, "Standard", 47.5, id="standard_with_fee"),
        pytest.param(100.0, 50, 0, "Bonus Bet", 100.0, id="bonus_bet_payout_is_profit"),
        pytest.param(75.0, 50, 1.0, "Bonus Bet", 74.0, id="bonus_bet_with_fee"),
        pytest.param(200.0, 100, 0, "Profit Boost", 100.0, id="profit_boost_uses_standard_formula"),
    ],
)
def test_derive_pl_from_payout(payout, stake, fee, category, expected_pl):
    assert derive_pl_from_payout(payout, stake, fee, category) == expected_pl
