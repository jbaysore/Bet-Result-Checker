"""Per-book fractional-cent rounding: truncate (default) vs nearest (FanDuel)."""

import pytest

from resolver import calculate_pl_and_payout, _american_odds_profit, _decimal_odds_profit
from config import BET_CATEGORY_STANDARD, RESULT_WIN, PAYOUT_ROUND_NEAREST_BOOKS


def test_fanduel_is_configured_nearest():
    assert "fanduel" in PAYOUT_ROUND_NEAREST_BOOKS


def test_prophetx_is_configured_nearest():
    assert "prophetx" in PAYOUT_ROUND_NEAREST_BOOKS


def test_fanatics_is_configured_nearest():
    assert "fanatics" in PAYOUT_ROUND_NEAREST_BOOKS


def test_real_fanatics_settlement_rounds_up():
    # Confirmed real settlement: stake $1,017 @ -270, payout $1,393.67 (profit 376.6666..)
    pl, payout = calculate_pl_and_payout(
        RESULT_WIN, 1017.0, -270, BET_CATEGORY_STANDARD, fee=0.5, round_to_nearest=True)
    assert payout == 1393.67
    assert pl == 376.17


def test_real_fanatics_settlement_truncates_by_default():
    pl, payout = calculate_pl_and_payout(
        RESULT_WIN, 1017.0, -270, BET_CATEGORY_STANDARD, fee=0.5)
    assert payout == 1393.66
    assert pl == 376.16


def test_real_prophetx_settlement_rounds_up():
    # Confirmed real settlement: stake $297.83 @ -470, app +$63.37 before 2% fee.
    pl, payout = calculate_pl_and_payout(
        RESULT_WIN, 297.83, -470, BET_CATEGORY_STANDARD,
        fee_pct_on_win_only=2.0, bet_type="Moneyline", round_to_nearest=True)
    assert payout == 361.20
    assert pl == 62.10


def test_prophetx_same_settlement_truncates_by_default():
    pl, payout = calculate_pl_and_payout(
        RESULT_WIN, 297.83, -470, BET_CATEGORY_STANDARD,
        fee_pct_on_win_only=2.0, bet_type="Moneyline")
    assert payout == 361.19
    assert pl == 62.09


def test_real_fanduel_settlement_rounds_up():
    # Confirmed real settlement: stake $284 @ -430 -> $350.05 (profit 66.0465..)
    pl, payout = calculate_pl_and_payout(
        RESULT_WIN, 284.0, -430, BET_CATEGORY_STANDARD, fee=0.0, round_to_nearest=True)
    assert payout == 350.05
    assert pl == 66.05


def test_same_settlement_truncates_by_default():
    pl, payout = calculate_pl_and_payout(
        RESULT_WIN, 284.0, -430, BET_CATEGORY_STANDARD, fee=0.0)
    assert payout == 350.04
    assert pl == 66.04


@pytest.mark.parametrize("stake,odds,trunc,nearest", [
    (100, -110, 90.90, 90.91),
    (100, -453, 22.07, 22.08),
    (325.76, -255, 127.74, 127.75),
    (123.45, -108, 114.30, 114.31),
])
def test_profit_rounding_directions(stake, odds, trunc, nearest):
    assert _american_odds_profit(stake, odds) == trunc                      # default truncate
    assert _american_odds_profit(stake, odds, round_to_nearest=True) == nearest


def test_exact_cent_unaffected_by_mode():
    # +150 on $50 is exactly $75.00 -- both modes agree, no penny drift.
    assert _american_odds_profit(50, 150) == 75.00
    assert _american_odds_profit(50, 150, round_to_nearest=True) == 75.00


def test_decimal_profit_respects_mode():
    # Parlay path (decimal odds) gets the same per-book rounding.
    # $100 at decimal 1.9091 -> profit 90.91 nearest / 90.90 truncated.
    assert _decimal_odds_profit(100, 1.90909091) == 90.90
    assert _decimal_odds_profit(100, 1.90909091, round_to_nearest=True) == 90.91
