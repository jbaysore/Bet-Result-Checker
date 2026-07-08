"""Tests for manual cashout P/L derivation."""

from resolver import derive_cashout_pl
from config import BET_CATEGORY_BONUS_BET, BET_CATEGORY_STANDARD


def test_cashout_fee_kept():
    assert derive_cashout_pl(95, 100, 0.5, BET_CATEGORY_STANDARD) == -5.5


def test_cashout_fee_before_odds():
    assert derive_cashout_pl(
        108, 100, 2, BET_CATEGORY_STANDARD, fee_before_odds=True
    ) == 8.0


def test_cashout_bonus_bet():
    assert derive_cashout_pl(50, 100, 0, BET_CATEGORY_BONUS_BET) == 50.0


def test_cashout_locked_profit_and_partial_loss():
    assert derive_cashout_pl(120, 100, 0, BET_CATEGORY_STANDARD) == 20.0
    assert derive_cashout_pl(80, 100, 0, BET_CATEGORY_STANDARD) == -20.0
