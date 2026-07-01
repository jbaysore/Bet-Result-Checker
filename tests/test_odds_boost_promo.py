"""Odds Boost promo: realized value = P/L delta vs the original (pre-boost) odds."""

from datetime import date

import pytest

from promo_resolver import evaluate_odds_boost_promo, evaluate_promo
from config import (
    PROMO_TYPE_ODDS_BOOST, BET_CATEGORY_ODDS_BOOST,
    PROMO_STATUS_REALIZED, PROMO_STATUS_UNUSED,
)


def _promo(**kw):
    p = {"promo_id": "9", "promo_type": PROMO_TYPE_ODDS_BOOST,
         "original_odds": "+100", "expiration_date": "2026-07-31"}
    p.update(kw)
    return p


def _bet(**kw):
    b = {"bet_id": "50", "date_placed": "2026-07-01", "book": "fanduel", "sport": "x",
         "stake": "25", "fee": "0", "bet_category": BET_CATEGORY_ODDS_BOOST,
         "promo_id": "9", "result": "WIN", "payout": "62.50", "pl": "37.50",
         "odds_taken": "+150"}
    b.update(kw)
    return b


def test_win_realizes_the_boost_delta():
    # Boosted +150 (P/L 37.50 on $25) vs original +100 (25.00) -> boost worth 12.50
    v = evaluate_odds_boost_promo(_promo(), [_bet()], date(2026, 7, 2), {"fanduel": False})
    assert v["finalize"] == {"status": PROMO_STATUS_REALIZED, "realized_amount": 12.5}


def test_loss_realizes_zero():
    v = evaluate_odds_boost_promo(
        _promo(), [_bet(result="LOSS", pl="-25.00", payout="")],
        date(2026, 7, 2), {"fanduel": False})
    assert v["finalize"] == {"status": PROMO_STATUS_REALIZED, "realized_amount": 0.0}


def test_push_realizes_zero():
    v = evaluate_odds_boost_promo(
        _promo(), [_bet(result="PUSH", pl="0", payout="25")],
        date(2026, 7, 2), {"fanduel": False})
    assert v["finalize"]["realized_amount"] == 0.0


def test_no_bet_before_expiry_waits():
    v = evaluate_odds_boost_promo(_promo(), [], date(2026, 7, 2), {})
    assert v["finalize"] is None


def test_no_bet_after_expiry_is_unused():
    v = evaluate_odds_boost_promo(
        _promo(expiration_date="2026-06-30"), [], date(2026, 7, 2), {})
    assert v["finalize"] == {"status": PROMO_STATUS_UNUSED, "realized_amount": 0.0}


def test_unsettled_bet_waits():
    v = evaluate_odds_boost_promo(
        _promo(), [_bet(result="", pl="", payout="")], date(2026, 7, 2), {"fanduel": False})
    assert v["finalize"] is None


def test_missing_original_odds_stays_pending():
    v = evaluate_odds_boost_promo(
        _promo(original_odds=""), [_bet()], date(2026, 7, 2), {"fanduel": False})
    assert v["finalize"] is None
    assert "Original Odds" in v["log"][-1]


def test_negative_original_odds_baseline():
    # Boosted to +120 from a -110 favorite, $100 stake, win, on a truncate book.
    # Boosted P/L = 120.00; baseline at -110 = 90.90 -> boost = 29.10
    bet = _bet(odds_taken="+120", stake="100", pl="120.00", payout="220", book="draftkings")
    v = evaluate_odds_boost_promo(
        _promo(original_odds="-110"), [bet], date(2026, 7, 2), {"draftkings": False})
    assert v["finalize"]["realized_amount"] == pytest.approx(29.10, abs=0.01)


def test_fanduel_odds_boost_baseline_rounds_nearest():
    # Same bet on FanDuel: baseline at -110 rounds to 90.91, so boost = 29.09.
    bet = _bet(odds_taken="+120", stake="100", pl="120.00", payout="220", book="fanduel")
    v = evaluate_odds_boost_promo(
        _promo(original_odds="-110"), [bet], date(2026, 7, 2), {"fanduel": False})
    assert v["finalize"]["realized_amount"] == pytest.approx(29.09, abs=0.01)


def test_dispatch_routes_odds_boost():
    v = evaluate_promo(_promo(), [_bet()], date(2026, 7, 2), {"fanduel": False})
    assert v["finalize"] == {"status": PROMO_STATUS_REALIZED, "realized_amount": 12.5}
