"""Per-book fractional-cent rounding: truncate (default) vs nearest (FanDuel)."""

import math

import pytest

from resolver import (
    calculate_pl_and_payout,
    _american_odds_profit,
    _boosted_american_odds_round_up,
    _decimal_odds_profit,
)
from config import (
    BET_CATEGORY_STANDARD,
    RESULT_WIN,
    PAYOUT_ROUND_NEAREST_BOOKS,
    MANUAL_PAYOUT_REQUIRED_BOOKS,
    PROFIT_BOOST_ROUND_UP_BOOKS,
)


def test_fanduel_is_configured_nearest():
    assert "fanduel" in PAYOUT_ROUND_NEAREST_BOOKS


def test_prophetx_is_configured_nearest():
    assert "prophetx" in PAYOUT_ROUND_NEAREST_BOOKS


def test_fanatics_is_configured_nearest():
    assert "fanatics" in PAYOUT_ROUND_NEAREST_BOOKS


def test_hard_rock_is_configured_nearest_for_every_regional_key():
    assert {
        "hardrockbet", "hardrockbet_az", "hardrockbet_fl", "hardrockbet_oh"
    } <= PAYOUT_ROUND_NEAREST_BOOKS


def test_caesars_is_configured_nearest():
    assert "williamhill_us" in PAYOUT_ROUND_NEAREST_BOOKS


def test_real_hard_rock_settlement_rounds_up():
    # Confirmed real settlement: stake $26 @ -350 -> $33.43 (profit $7.4285...).
    pl, payout = calculate_pl_and_payout(
        RESULT_WIN, 26.0, -350, BET_CATEGORY_STANDARD,
        fee=0.0, round_to_nearest="hardrockbet" in PAYOUT_ROUND_NEAREST_BOOKS)
    assert payout == 33.43
    assert pl == 7.43


def test_real_caesars_bet_297_rounds_to_nearest_cent():
    # Confirmed payout: $50 @ -105 pays $97.62, not truncated $97.61.
    pl, payout = calculate_pl_and_payout(
        RESULT_WIN, 50.0, -105, BET_CATEGORY_STANDARD,
        fee=0.25,
        round_to_nearest="williamhill_us" in PAYOUT_ROUND_NEAREST_BOOKS)
    assert payout == 97.62
    assert pl == 47.37


def test_profit_boost_whole_american_rounding_books_are_configured():
    assert {"draftkings", "fanduel"} <= PROFIT_BOOST_ROUND_UP_BOOKS


def test_real_draftkings_bet_247_profit_boost_pays_56_20():
    # +139 with a 30% boost is raw +180.7, displayed/settled by DK as +181.
    assert _boosted_american_odds_round_up(139, 30) == 181
    pl, payout = calculate_pl_and_payout(
        RESULT_WIN, 20.0, 139, "Profit Boost",
        boost_pct=30, fee=0.25, round_boosted_odds_up=True)
    assert (pl, payout) == (35.95, 56.20)


@pytest.mark.parametrize("odds,expected_pl,expected_payout", [
    (223, 57.75, 78.00),  # Bet 203: raw +289.9 -> displayed +290
    (-102, 25.35, 45.60),  # Bet 224: raw +127.45... -> displayed +128
])
def test_other_real_draftkings_profit_boosts(
        odds, expected_pl, expected_payout):
    pl, payout = calculate_pl_and_payout(
        RESULT_WIN, 20.0, odds, "Profit Boost",
        boost_pct=30, fee=0.25, round_boosted_odds_up=True)
    assert (pl, payout) == (expected_pl, expected_payout)


def test_real_fanduel_profit_boost_uses_whole_price_then_nearest_cent():
    assert _boosted_american_odds_round_up(-118, 30) == 111
    pl, payout = calculate_pl_and_payout(
        RESULT_WIN, 25.0, -118, "Profit Boost",
        boost_pct=30, round_to_nearest=True,
        round_boosted_odds_up=True)
    assert (pl, payout) == (27.75, 52.75)


def test_rebet_requires_manual_payout_not_nearest():
    # Rebet WIN payouts disagree with truncate/nearest/ceil — see config docstring.
    assert "rebet" in MANUAL_PAYOUT_REQUIRED_BOOKS
    assert "rebet" not in PAYOUT_ROUND_NEAREST_BOOKS


def test_rebet_confirmed_settlements_match_no_rounding_mode():
    # Guard against "just add rebet to PAYOUT_ROUND_NEAREST_BOOKS".
    # Confirmed 2026-07-10: no single mode (truncate / nearest / ceil) matches both.
    france_trunc = 36.28 + _american_odds_profit(36.28, -167, round_to_nearest=False)
    france_nearest = 36.28 + _american_odds_profit(36.28, -167, round_to_nearest=True)
    france_ceil = 36.28 + math.ceil(36.28 * 100 / 167 * 100 - 1e-12) / 100
    assert france_trunc == pytest.approx(58.00)
    assert france_nearest == pytest.approx(58.00)
    assert france_ceil == pytest.approx(58.01)  # ceil happens to match France only

    dbacks_trunc = 28.61 + _american_odds_profit(28.61, -141, round_to_nearest=False)
    dbacks_nearest = 28.61 + _american_odds_profit(28.61, -141, round_to_nearest=True)
    dbacks_ceil = 28.61 + math.ceil(28.61 * 100 / 141 * 100 - 1e-12) / 100
    assert dbacks_trunc == pytest.approx(48.90)
    assert dbacks_nearest == pytest.approx(48.90)
    assert dbacks_ceil == pytest.approx(48.91)  # ceil still misses actual 48.92
    # Actuals that no mode fully covers:
    assert france_trunc != pytest.approx(58.01) or dbacks_nearest != pytest.approx(48.92)
    assert dbacks_ceil != pytest.approx(48.92)

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
