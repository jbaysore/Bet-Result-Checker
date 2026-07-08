"""Manual Payout P/L derivation for Kalshi-style books."""

from resolver import calculate_pl_and_payout
from config import BET_CATEGORY_BONUS_BET, BET_CATEGORY_STANDARD, RESULT_LOSS


def test_kalshi_bonus_bet_loss_pl_is_zero_not_negative_stake():
    # BetID 54 pattern: Bonus Bet LOSS on Kalshi with payout $0.
    # Old manual path used payout - stake = -20; correct promo-funded loss is 0.
    pl, payout = calculate_pl_and_payout(
        RESULT_LOSS, 20.0, -150, BET_CATEGORY_BONUS_BET, fee=0.0, fee_before_odds=True)
    assert pl == 0.0
    assert payout is None


def test_kalshi_standard_loss_still_costs_full_stake():
    pl, payout = calculate_pl_and_payout(
        RESULT_LOSS, 99.99, -200, BET_CATEGORY_STANDARD, fee=0.0, fee_before_odds=True)
    assert pl == -99.99
    assert payout is None
