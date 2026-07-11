"""P/L and payout matrix for calculate_pl_and_payout."""

import pytest

from config import BET_TYPE_PARLAY
from resolver import calculate_pl_and_payout


@pytest.mark.parametrize(
    "result,category,stake,odds,fee,fee_before_odds,boost_pct,"
    "fee_pct_on_win_only,fee_pct_on_win_stake,bet_type,expected_pl,expected_payout",
    [
        pytest.param(
            "WIN", "Standard", 100, -110, 0, False, None,
            None, None, "", 90.9, 190.9,
            id="std_win_minus110",
        ),
        pytest.param(
            "WIN", "Standard", 325.76, -255, 0, False, None,
            None, None, "", 127.74, 453.5,
            id="std_win_dk_truncation_325",
        ),
        pytest.param(
            "WIN", "Standard", 100, -453, 0, False, None,
            None, None, "", 22.07, 122.07,
            id="std_win_dk_truncation_100",
        ),
        pytest.param(
            "LOSS", "Standard", 100, -110, 2.5, False, None,
            None, None, "", -102.5, None,
            id="std_loss_with_fee",
        ),
        pytest.param(
            "PUSH", "Standard", 100, -110, 2.5, False, None,
            None, None, "", -2.5, 100,
            id="std_push_with_fee",
        ),
        pytest.param(
            "VOID", "Standard", 100, -110, 0, False, None,
            None, None, "", 0.0, 100,
            id="std_void",
        ),
        pytest.param(
            "LOSS", "Bonus Bet", 50, 200, 0, False, None,
            None, None, "", 0.0, None,
            id="bonus_loss_free",
        ),
        pytest.param(
            "WIN", "Bonus Bet", 50, 200, 0, False, None,
            None, None, "", 100.0, 100.0,
            id="bonus_win_profit_only",
        ),
        pytest.param(
            "VOID", "Bonus Bet", 50, 200, 0, False, None,
            None, None, "", 0.0, None,
            id="bonus_void",
        ),
        pytest.param(
            "LOSS", "Deposit Bonus", 50, -110, 0, False, None,
            None, None, "", 0.0, None,
            id="deposit_bonus_loss",
        ),
        pytest.param(
            "WIN", "Profit Boost", 100, -110, 0, False, 100,
            None, None, "", 181.8, 281.8,
            id="profit_boost_100pct_doubles_profit",
        ),
        pytest.param(
            "VOID", "Standard", 51.24, -200, 1.08, True, None,
            None, None, "", -1.08, 50.16,
            id="poly_void_real_bet",
        ),
        pytest.param(
            "LOSS", "Standard", 51.24, -200, 1.08, True, None,
            None, None, "", -51.24, None,
            id="poly_loss_no_double_fee",
        ),
        pytest.param(
            "WIN", "Standard", 51.24, -200, 1.08, True, None,
            None, None, "", 25.08, 75.24,
            id="poly_win_effective_stake",
        ),
        pytest.param(
            "PUSH", "Standard", 51.24, -200, 1.08, True, None,
            None, None, "", -1.08, 50.16,
            id="poly_push_fee_not_refunded",
        ),
        pytest.param(
            "WIN", "Standard", 100, -110, 0, False, None,
            2.0, None, "Moneyline", 89.08, 190.9,
            id="prophetx_win_2pct_of_profit",
        ),
        pytest.param(
            "LOSS", "Standard", 100, -110, 0, False, None,
            2.0, None, "", -100.0, None,
            id="prophetx_loss_no_fee",
        ),
        pytest.param(
            "WIN", "Standard", 100, -110, 0, False, None,
            2.0, None, BET_TYPE_PARLAY, 90.9, 190.9,
            id="prophetx_parlay_win_no_fee",
        ),
        pytest.param(
            "WIN", "Standard", 100, -110, 0, False, None,
            1.0, None, "Moneyline", 89.99, 190.9,
            id="betopenly_win_1pct_of_profit",
        ),
        pytest.param(
            "LOSS", "Standard", 100, -110, 0, False, None,
            1.0, None, "", -100.0, None,
            id="betopenly_loss_no_fee",
        ),
        pytest.param(
            "LOSS", "Qualifying Bet", 25, -110, 0, False, None,
            None, None, "", -25.0, None,
            id="qualifying_loss_real_money",
        ),
    ],
)
def test_calculate_pl_and_payout(
    result, category, stake, odds, fee, fee_before_odds, boost_pct,
    fee_pct_on_win_only, fee_pct_on_win_stake, bet_type,
    expected_pl, expected_payout,
):
    kwargs = {
        "fee": fee,
        "fee_before_odds": fee_before_odds,
        "boost_pct": boost_pct,
    }
    if fee_pct_on_win_only is not None:
        kwargs["fee_pct_on_win_only"] = fee_pct_on_win_only
        kwargs["bet_type"] = bet_type or "Moneyline"
    if fee_pct_on_win_stake is not None:
        kwargs["fee_pct_on_win_stake"] = fee_pct_on_win_stake

    pl, payout = calculate_pl_and_payout(
        result, stake, odds, category, **kwargs
    )
    assert pl == expected_pl
    assert payout == expected_payout


def test_profit_boost_win_without_boost_pct_raises():
    with pytest.raises(ValueError, match="boost"):
        calculate_pl_and_payout(
            "WIN", 100, -110, "Profit Boost", boost_pct=None
        )


def test_unrecognised_result_raises():
    with pytest.raises(ValueError, match="unrecognised result"):
        calculate_pl_and_payout("MAYBE", 100, -110, "Standard")


# ── Phase 2: HALF WIN / HALF LOSS P/L (half the stake scored, half pushed) ───
# THE cross-repo parity fixture: -0.75, 1-goal win, $100 @ -110. Half ($50) wins
# 50*(100/110)=45.4545→45.45 (DK truncation), half pushes (returns $50). So
# P/L = 45.45, payout = 95.45 + 50 = 145.45. betReviewPl.js must produce the same.
def test_half_win_standard_minus110():
    pl, payout = calculate_pl_and_payout("HALF WIN", 100, -110, "Standard")
    assert (pl, payout) == (45.45, 145.45)


def test_half_loss_standard_minus110():
    # Half ($50) loses, half pushes (returns $50). P/L = -50, payout = 50.
    pl, payout = calculate_pl_and_payout("HALF LOSS", 100, -110, "Standard")
    assert (pl, payout) == (-50.0, 50.0)


def test_half_win_plus150():
    # Half ($50) wins 50*1.5=75 profit; payout = (50+75) + 50 push = 175. P/L 75.
    pl, payout = calculate_pl_and_payout("HALF WIN", 100, 150, "Standard")
    assert (pl, payout) == (75.0, 175.0)


def test_half_loss_with_fee_charged_once():
    # Fee applies per the LOSS rules, ONCE (not double-charged across halves).
    pl, payout = calculate_pl_and_payout("HALF LOSS", 100, -110, "Standard", fee=2.5)
    assert (pl, payout) == (-52.5, 50.0)


def test_half_win_fee_charged_once():
    # Fee per WIN rules, once. P/L = 45.45 - 2.5 = 42.95; payout unchanged.
    pl, payout = calculate_pl_and_payout("HALF WIN", 100, -110, "Standard", fee=2.5)
    assert (pl, payout) == (42.95, 145.45)


def test_half_win_bonus_bet_pays_profit_plus_returned_half():
    # Bonus Bet: winning half returns profit only (token consumed); pushing half
    # returns its stake. profit(50@-110)=45.45; payout = 45.45 + 50 = 95.45.
    pl, payout = calculate_pl_and_payout("HALF WIN", 100, -110, "Bonus Bet")
    assert (pl, payout) == (45.45, 95.45)
