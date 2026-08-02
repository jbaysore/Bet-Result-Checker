"""Promotion finalization logic — FIFO tokens, expiration, insurance."""

from datetime import date

import pytest

from config import (
    BET_CATEGORY_BONUS_BET,
    BET_CATEGORY_DEPOSIT_BONUS,
    BET_CATEGORY_INSURANCE_BET,
    BET_CATEGORY_PROFIT_BOOST,
    BET_CATEGORY_QUALIFYING,
    PROMO_STATUS_REALIZED,
    PROMO_STATUS_UNUSED,
    PROMO_TYPE_BONUS_BET,
    PROMO_TYPE_DEPOSIT_BONUS,
    PROMO_TYPE_INSURANCE_BET,
    PROMO_TYPE_PROFIT_BOOST,
    PROMO_TYPE_PROFIT_BOOST_DAILY,
    REWARD_TIMING_END_OF_WINDOW,
    REWARD_TIMING_GRANTED,
)
from promo_resolver import evaluate_promo
from conftest import make_bet, make_promo


# ── Bonus Bet / multi-grant FIFO ─────────────────────────────────────────


def test_no_qualifiers_after_expiration_is_unused():
    promo = make_promo(expiration_date="2026-06-01")
    verdict = evaluate_promo(promo, [], date(2026, 6, 15))
    assert verdict["finalize"] == {"status": PROMO_STATUS_UNUSED, "realized_amount": 0.0}


def test_qualifying_window_still_open_stays_pending():
    promo = make_promo(expiration_date="2026-07-01")
    qualifiers = [make_bet(bet_category=BET_CATEGORY_QUALIFYING, bet_id="10")]
    verdict = evaluate_promo(promo, qualifiers, date(2026, 6, 15))
    assert verdict["finalize"] is None


def test_bonus_bet_finalizes_with_token_pl():
    promo = make_promo(
        expiration_date="2026-06-01",
        expected_reward_count="1",
        reward_timing=REWARD_TIMING_END_OF_WINDOW,
    )
    bets = [
        make_bet(
            bet_id="10",
            bet_category=BET_CATEGORY_QUALIFYING,
            date_placed="2026-05-15",
            stake="10",
            fee="0.50",
        ),
        make_bet(
            bet_id="11",
            bet_category=BET_CATEGORY_BONUS_BET,
            date_placed="2026-05-20",
            result="WIN",
            pl="50",
        ),
    ]
    verdict = evaluate_promo(promo, bets, date(2026, 6, 15))
    assert verdict["finalize"] == {
        "status": PROMO_STATUS_REALIZED,
        "realized_amount": 50.0,
    }
    assert verdict["qualifying_cost_fill"] == 10.5


def test_unsettled_reward_bet_blocks_finalization():
    promo = make_promo(expiration_date="2026-06-01")
    bets = [
        make_bet(
            bet_id="10",
            bet_category=BET_CATEGORY_QUALIFYING,
            date_placed="2026-05-15",
        ),
        make_bet(
            bet_id="11",
            bet_category=BET_CATEGORY_BONUS_BET,
            date_placed="2026-05-20",
            result="",
        ),
    ]
    verdict = evaluate_promo(promo, bets, date(2026, 6, 15))
    assert verdict["finalize"] is None


def test_second_token_forfeited_after_deadline():
    promo = make_promo(
        expiration_date="2026-06-01",
        expected_reward_count="2",
        reward_timing=REWARD_TIMING_END_OF_WINDOW,
        token_usage_window="7",
    )
    bets = [
        make_bet(
            bet_id="10",
            bet_category=BET_CATEGORY_QUALIFYING,
            date_placed="2026-05-01",
        ),
        make_bet(
            bet_id="11",
            bet_category=BET_CATEGORY_QUALIFYING,
            date_placed="2026-05-02",
        ),
        make_bet(
            bet_id="12",
            bet_category=BET_CATEGORY_BONUS_BET,
            date_placed="2026-05-10",
            result="WIN",
            pl="30",
        ),
    ]
    verdict = evaluate_promo(promo, bets, date(2026, 6, 20))
    assert verdict["finalize"] == {
        "status": PROMO_STATUS_REALIZED,
        "realized_amount": 30.0,
    }


# ── Granted (no qualifying bet) ──────────────────────────────────────────


def test_granted_bonus_bet_pending_before_use_by():
    promo = make_promo(
        expiration_date="2026-07-01",
        reward_timing=REWARD_TIMING_GRANTED,
    )
    verdict = evaluate_promo(promo, [], date(2026, 6, 15))
    assert verdict["finalize"] is None
    assert verdict["qualifying_cost_fill"] is None


def test_granted_bonus_bet_finalizes_with_only_reward_bet():
    promo = make_promo(
        expiration_date="2026-07-01",
        reward_timing=REWARD_TIMING_GRANTED,
        expected_reward_count="1",
    )
    bets = [
        make_bet(
            bet_id="11",
            bet_category=BET_CATEGORY_BONUS_BET,
            date_placed="2026-06-15",
            result="WIN",
            pl="50",
        ),
    ]
    verdict = evaluate_promo(promo, bets, date(2026, 6, 20))
    assert verdict["finalize"] == {
        "status": PROMO_STATUS_REALIZED,
        "realized_amount": 50.0,
    }
    assert verdict["qualifying_cost_fill"] is None


def test_granted_bonus_bet_expired_no_bets_realized_zero():
    promo = make_promo(
        expiration_date="2026-06-01",
        reward_timing=REWARD_TIMING_GRANTED,
        expected_reward_count="1",
    )
    verdict = evaluate_promo(promo, [], date(2026, 6, 15))
    assert verdict["finalize"] == {
        "status": PROMO_STATUS_REALIZED,
        "realized_amount": 0.0,
    }
    assert verdict["qualifying_cost_fill"] is None


# ── Profit Boost token value ─────────────────────────────────────────────


def test_profit_boost_token_value_is_boost_delta():
    promo = make_promo(
        promo_type=PROMO_TYPE_PROFIT_BOOST,
        boost_pct="100",
        expiration_date="2026-06-01",
    )
    bets = [
        make_bet(
            bet_id="10",
            bet_category=BET_CATEGORY_QUALIFYING,
            date_placed="2026-05-15",
        ),
        make_bet(
            bet_id="11",
            bet_category=BET_CATEGORY_PROFIT_BOOST,
            date_placed="2026-05-20",
            result="WIN",
            stake="100",
            odds_taken="-110",
            fee="0",
            pl="181.8",
        ),
    ]
    verdict = evaluate_promo(
        promo, bets, date(2026, 6, 15), fee_before_odds_lookup={"draftkings": False}
    )
    assert verdict["finalize"]["status"] == PROMO_STATUS_REALIZED
    assert verdict["finalize"]["realized_amount"] == pytest.approx(90.9, abs=0.01)


def test_profit_boost_daily_token_value_is_boost_delta():
    promo = make_promo(
        promo_type=PROMO_TYPE_PROFIT_BOOST_DAILY,
        start_date="2026-06-01",
        boost_pct="100",
    )
    bets = [
        make_bet(
            bet_id="11",
            bet_category=BET_CATEGORY_PROFIT_BOOST,
            date_placed="2026-06-01",
            result="WIN",
            stake="100",
            odds_taken="-110",
            fee="0",
            pl="181.8",
        ),
    ]
    verdict = evaluate_promo(
        promo, bets, date(2026, 6, 15), fee_before_odds_lookup={"draftkings": False}
    )
    assert verdict["finalize"]["status"] == PROMO_STATUS_REALIZED
    assert verdict["finalize"]["realized_amount"] == pytest.approx(90.9, abs=0.01)


def test_profit_boost_daily_fee_before_odds_affects_token_value():
    promo = make_promo(
        promo_type=PROMO_TYPE_PROFIT_BOOST_DAILY,
        start_date="2026-06-01",
        boost_pct="100",
    )
    bets = [
        make_bet(
            bet_id="11",
            book="polymarket",
            bet_category=BET_CATEGORY_PROFIT_BOOST,
            date_placed="2026-06-01",
            result="WIN",
            stake="51.24",
            odds_taken="-200",
            fee="1.08",
            pl="50.16",
        ),
    ]
    verdict_wrong = evaluate_promo(promo, bets, date(2026, 6, 15), fee_before_odds_lookup={})
    verdict_right = evaluate_promo(
        promo, bets, date(2026, 6, 15), fee_before_odds_lookup={"polymarket": True}
    )
    assert verdict_wrong["finalize"]["realized_amount"] == pytest.approx(25.62, abs=0.01)
    assert verdict_right["finalize"]["realized_amount"] == pytest.approx(25.08, abs=0.01)
    assert verdict_wrong["finalize"]["realized_amount"] != verdict_right["finalize"]["realized_amount"]


# ── Deposit Bonus ────────────────────────────────────────────────────────


def test_deposit_bonus_loss_realizes_zero():
    promo = make_promo(promo_type=PROMO_TYPE_DEPOSIT_BONUS)
    bets = [
        make_bet(
            bet_id="20",
            bet_category=BET_CATEGORY_DEPOSIT_BONUS,
            result="LOSS",
            pl="0",
        ),
    ]
    verdict = evaluate_promo(promo, bets, date(2026, 6, 15))
    assert verdict["finalize"] == {
        "status": PROMO_STATUS_REALIZED,
        "realized_amount": 0.0,
    }


def test_deposit_bonus_win_uses_bet_pl():
    promo = make_promo(promo_type=PROMO_TYPE_DEPOSIT_BONUS)
    bets = [
        make_bet(
            bet_id="20",
            bet_category=BET_CATEGORY_DEPOSIT_BONUS,
            result="WIN",
            pl="75.50",
        ),
    ]
    verdict = evaluate_promo(promo, bets, date(2026, 6, 15))
    assert verdict["finalize"]["realized_amount"] == 75.5


def test_deposit_bonus_waits_for_linked_bet():
    promo = make_promo(promo_type=PROMO_TYPE_DEPOSIT_BONUS)
    verdict = evaluate_promo(promo, [], date(2026, 6, 15))
    assert verdict["finalize"] is None


# ── Insurance Bet ────────────────────────────────────────────────────────


def test_insurance_leg1_win_realizes_zero():
    promo = make_promo(
        promo_type=PROMO_TYPE_INSURANCE_BET,
        bonus_amount="100",
    )
    bets = [
        make_bet(
            bet_id="30",
            bet_category=BET_CATEGORY_INSURANCE_BET,
            result="WIN",
        ),
    ]
    verdict = evaluate_promo(promo, bets, date(2026, 6, 15))
    assert verdict["finalize"] == {
        "status": PROMO_STATUS_REALIZED,
        "realized_amount": 0.0,
    }


def test_insurance_leg1_loss_leg2_win():
    promo = make_promo(
        promo_type=PROMO_TYPE_INSURANCE_BET,
        bonus_amount="100",
    )
    bets = [
        make_bet(
            bet_id="30",
            bet_category=BET_CATEGORY_INSURANCE_BET,
            result="LOSS",
            stake="50",
        ),
        make_bet(
            bet_id="31",
            bet_category=BET_CATEGORY_BONUS_BET,
            date_placed="2026-06-02",
            result="WIN",
            stake="50",
            pl="40",
        ),
    ]
    verdict = evaluate_promo(promo, bets, date(2026, 6, 15))
    assert verdict["finalize"] == {
        "status": PROMO_STATUS_REALIZED,
        "realized_amount": 40.0,
    }


def test_insurance_leg1_loss_no_leg2_stays_pending():
    promo = make_promo(
        promo_type=PROMO_TYPE_INSURANCE_BET,
        bonus_amount="100",
    )
    bets = [
        make_bet(
            bet_id="30",
            bet_category=BET_CATEGORY_INSURANCE_BET,
            result="LOSS",
            stake="50",
        ),
    ]
    verdict = evaluate_promo(promo, bets, date(2026, 6, 15))
    assert verdict["finalize"] is None


def test_insurance_split_refund_waits_until_full_face_amount_is_linked():
    promo = make_promo(promo_type=PROMO_TYPE_INSURANCE_BET, bonus_amount="100")
    bets = [
        make_bet(
            bet_id="30",
            bet_category=BET_CATEGORY_INSURANCE_BET,
            result="LOSS",
            stake="100",
        ),
        make_bet(
            bet_id="31",
            bet_category=BET_CATEGORY_BONUS_BET,
            date_placed="2026-06-02",
            result="WIN",
            stake="50",
            pl="35",
        ),
    ]

    verdict = evaluate_promo(promo, bets, date(2026, 6, 15))

    assert verdict["finalize"] is None
    assert any("$50.00 of $100.00" in line for line in verdict["log"])


def test_insurance_split_refund_waits_for_every_covering_bet_to_settle():
    promo = make_promo(promo_type=PROMO_TYPE_INSURANCE_BET, bonus_amount="100")
    bets = [
        make_bet(
            bet_id="30",
            bet_category=BET_CATEGORY_INSURANCE_BET,
            result="LOSS",
            stake="100",
        ),
        make_bet(
            bet_id="31",
            bet_category=BET_CATEGORY_BONUS_BET,
            date_placed="2026-06-02",
            result="WIN",
            stake="50",
            pl="35",
        ),
        make_bet(
            bet_id="32",
            bet_category=BET_CATEGORY_BONUS_BET,
            date_placed="2026-06-03",
            result="",
            stake="50",
            pl="",
        ),
    ]

    verdict = evaluate_promo(promo, bets, date(2026, 6, 15))

    assert verdict["finalize"] is None
    assert any("BetID 32 is not yet settled" in line for line in verdict["log"])


def test_insurance_split_refund_sums_every_covering_bets_pl():
    promo = make_promo(promo_type=PROMO_TYPE_INSURANCE_BET, bonus_amount="$100.00")
    bets = [
        make_bet(
            bet_id="30",
            bet_category=BET_CATEGORY_INSURANCE_BET,
            result="LOSS",
            stake="150",
        ),
        make_bet(
            bet_id="31",
            bet_category=BET_CATEGORY_BONUS_BET,
            date_placed="2026-06-02",
            result="WIN",
            stake="50",
            pl="35.25",
        ),
        make_bet(
            bet_id="32",
            bet_category=BET_CATEGORY_BONUS_BET,
            date_placed="2026-06-03",
            result="LOSS",
            stake="50",
            pl="0",
        ),
    ]

    verdict = evaluate_promo(promo, bets, date(2026, 6, 15))

    assert verdict["finalize"] == {
        "status": PROMO_STATUS_REALIZED,
        "realized_amount": 35.25,
    }


def test_insurance_refund_overfill_requires_manual_review():
    promo = make_promo(promo_type=PROMO_TYPE_INSURANCE_BET, bonus_amount="100")
    bets = [
        make_bet(
            bet_id="30",
            bet_category=BET_CATEGORY_INSURANCE_BET,
            result="LOSS",
            stake="100",
        ),
        make_bet(
            bet_id="31",
            bet_category=BET_CATEGORY_BONUS_BET,
            date_placed="2026-06-02",
            result="WIN",
            stake="60",
            pl="40",
        ),
        make_bet(
            bet_id="32",
            bet_category=BET_CATEGORY_BONUS_BET,
            date_placed="2026-06-03",
            result="WIN",
            stake="50",
            pl="30",
        ),
    ]

    verdict = evaluate_promo(promo, bets, date(2026, 6, 15))

    assert verdict["finalize"] is None
    assert any("overfill" in line for line in verdict["log"])


def test_multi_day_insurance_fifo_matches_split_refunds_by_amount():
    promo = make_promo(
        promo_type=PROMO_TYPE_INSURANCE_BET,
        bonus_amount="100",
        expected_reward_count="2",
        start_date="2026-06-01",
    )
    bets = [
        make_bet(
            bet_id="40",
            bet_category=BET_CATEGORY_INSURANCE_BET,
            date_placed="2026-06-01",
            result="LOSS",
            stake="100",
        ),
        make_bet(
            bet_id="41",
            bet_category=BET_CATEGORY_INSURANCE_BET,
            date_placed="2026-06-02",
            result="LOSS",
            stake="60",
        ),
        make_bet(
            bet_id="42",
            bet_category=BET_CATEGORY_BONUS_BET,
            date_placed="2026-06-03",
            result="WIN",
            stake="50",
            pl="30",
        ),
        make_bet(
            bet_id="43",
            bet_category=BET_CATEGORY_BONUS_BET,
            date_placed="2026-06-04",
            result="LOSS",
            stake="50",
            pl="0",
        ),
        make_bet(
            bet_id="44",
            bet_category=BET_CATEGORY_BONUS_BET,
            date_placed="2026-06-05",
            result="WIN",
            stake="25",
            pl="15",
        ),
        make_bet(
            bet_id="45",
            bet_category=BET_CATEGORY_BONUS_BET,
            date_placed="2026-06-06",
            result="WIN",
            stake="35",
            pl="21",
        ),
    ]

    verdict = evaluate_promo(promo, bets, date(2026, 6, 15))

    assert verdict["finalize"] == {
        "status": PROMO_STATUS_REALIZED,
        "realized_amount": 66.0,
    }
    assert any("BetID 42, 43" in line for line in verdict["log"])
    assert any("BetID 44, 45" in line for line in verdict["log"])


# ── Dispatch / edge cases ────────────────────────────────────────────────


def test_unknown_promo_type_not_implemented():
    promo = make_promo(promo_type="Mystery Promo")
    verdict = evaluate_promo(promo, [], date(2026, 6, 15))
    assert verdict.get("not_implemented") is True
    assert verdict["finalize"] is None


def test_profit_boost_daily_without_start_date_stays_pending():
    promo = make_promo(
        promo_type=PROMO_TYPE_PROFIT_BOOST_DAILY,
        start_date="",
    )
    verdict = evaluate_promo(promo, [], date(2026, 6, 15))
    assert verdict["finalize"] is None
    assert any("Start Date" in line for line in verdict["log"])
