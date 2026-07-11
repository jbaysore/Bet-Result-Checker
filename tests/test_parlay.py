"""Parlay parsing, odds math, result combination, and full-leg resolution."""

import json

import pytest

from config import (
    BET_TYPE_MONEYLINE, BET_TYPE_SPREAD, BET_TYPE_TOTAL,
    RESULT_WIN, RESULT_LOSS, RESULT_PUSH, RESULT_VOID,
)
import parlay
from resolver import resolve_parlay


# ── Odds conversion ────────────────────────────────────────────────────────────

def test_american_to_decimal_round_trip():
    assert parlay.american_to_decimal("+150") == 2.5
    assert parlay.decimal_to_american(2.5) == 150
    assert abs(parlay.american_to_decimal(-110) - 1.9090909) < 1e-6
    assert parlay.decimal_to_american(parlay.american_to_decimal(-110)) == -110


def test_fmt_american_sign():
    assert parlay.fmt_american(150) == "+150"
    assert parlay.fmt_american(-110) == "-110"
    assert parlay.fmt_american(None) is None


def test_combined_decimal_is_product():
    d = parlay.combined_decimal([parlay.american_to_decimal(-110), parlay.american_to_decimal(150)])
    assert abs(d - (1.9090909 * 2.5)) < 1e-5
    # -110 & +150 combine to roughly +377
    assert parlay.decimal_to_american(d) == 377


def test_half_point_combined_truncates_down():
    # -6000, -10000, +105 -> decimal 2.105008 = +110.5 American. Truncates to
    # +110, matching the sportsbook, rather than rounding up to +111.
    d = parlay.combined_decimal([
        parlay.american_to_decimal(-6000),
        parlay.american_to_decimal(-10000),
        parlay.american_to_decimal(105),
    ])
    assert abs(d - 2.1050083) < 1e-6
    assert parlay.decimal_to_american(d) == 110


def test_decimal_to_american_no_float_truncation():
    # An exact integer price (-110) must not be floored to -109 by float noise.
    assert parlay.decimal_to_american(parlay.american_to_decimal(-110)) == -110
    assert parlay.decimal_to_american(parlay.american_to_decimal(150)) == 150


def test_parlay_win_settles_from_exact_decimal():
    # The whole point of the decimal path: a $100 win on combined decimal
    # 2.105008 pays 210.50, not 211.00 (American +111 round-trip) or 210.00
    # (American +110 label).
    from resolver import calculate_pl_and_payout
    from config import BET_CATEGORY_STANDARD
    pl, payout = calculate_pl_and_payout(
        RESULT_WIN, 100.0, 0, BET_CATEGORY_STANDARD, decimal_odds=2.1050083333)
    assert payout == 210.50
    assert pl == 110.50


# ── Leg JSON parsing ───────────────────────────────────────────────────────────

def test_parse_legs_normalizes_camel_case():
    raw = json.dumps([{
        "sport": "baseball_mlb", "book": "fanduel",
        "team1": "Yankees", "team2": "Red Sox",
        "gameDate": "2026-06-30", "gameStart": "19:05",
        "betType": "Moneyline", "selection": "Yankees", "oddsTaken": "-150",
    }])
    legs = parlay.parse_legs(raw)
    assert legs[0]["game_date"] == "2026-06-30"
    assert legs[0]["game_start"] == "19:05"
    assert legs[0]["bet_type"] == "Moneyline"
    assert legs[0]["odds_taken"] == "-150"


def test_parse_legs_preserves_market_key():
    raw = json.dumps([{
        "sport": "baseball_mlb", "book": "draftkings",
        "team1": "Cubs", "team2": "Cardinals",
        "gameDate": "2026-07-08", "gameStart": "19:05",
        "betType": "Spread", "selection": "Cubs -7.5", "oddsTaken": "+140",
        "marketKey": "alternate_spreads",
    }])
    legs = parlay.parse_legs(raw)
    assert legs[0]["market_key"] == "alternate_spreads"


def test_parse_legs_bad_json_returns_empty():
    assert parlay.parse_legs("not json") == []
    assert parlay.parse_legs("") == []
    assert parlay.parse_legs(None) == []


def test_all_legs_automatable():
    good = parlay.parse_legs(json.dumps([{
        "sport": "x", "team1": "A", "selection": "A",
        "gameDate": "2026-01-01", "gameStart": "12:00", "betType": "Moneyline",
    }]))
    assert parlay.all_legs_automatable(good) is True

    prop = parlay.parse_legs(json.dumps([{
        "sport": "x", "team1": "A", "selection": "s",
        "gameDate": "2026-01-01", "gameStart": "12:00", "betType": "Prop",
    }]))
    assert parlay.all_legs_automatable(prop) is False

    missing_game = parlay.parse_legs(json.dumps([{
        "sport": "x", "team1": "A", "selection": "A", "betType": "Moneyline",
    }]))
    assert parlay.all_legs_automatable(missing_game) is False


# ── Result combination ─────────────────────────────────────────────────────────

def _d(a):
    return parlay.american_to_decimal(a)


def test_combine_all_win():
    result, eff = parlay.combine_parlay_results([(RESULT_WIN, _d(-110)), (RESULT_WIN, _d(150))])
    assert result == RESULT_WIN
    assert abs(eff - (_d(-110) * _d(150))) < 1e-9


def test_combine_any_loss_loses():
    result, eff = parlay.combine_parlay_results([(RESULT_WIN, _d(-110)), (RESULT_LOSS, _d(150))])
    assert result == RESULT_LOSS
    assert eff is None


def test_combine_push_leg_drops_out():
    # A pushed leg neither wins nor loses -- parlay reduces to the surviving leg
    # and re-prices at only that leg's odds.
    result, eff = parlay.combine_parlay_results([(RESULT_WIN, _d(-110)), (RESULT_PUSH, _d(150))])
    assert result == RESULT_WIN
    assert abs(eff - _d(-110)) < 1e-9


def test_combine_all_push_is_push():
    result, eff = parlay.combine_parlay_results([(RESULT_PUSH, _d(-110)), (RESULT_PUSH, _d(150))])
    assert result == RESULT_PUSH
    assert eff == 1.0


def test_combine_all_void_is_void():
    result, eff = parlay.combine_parlay_results([(RESULT_VOID, _d(-110)), (RESULT_VOID, _d(150))])
    assert result == RESULT_VOID
    assert eff == 1.0


# ── Full leg resolution ────────────────────────────────────────────────────────

def _leg(**kw):
    base = {
        "sport": "baseball_mlb", "team1": "Yankees", "team2": "Red Sox",
        "bet_type": BET_TYPE_MONEYLINE, "selection": "Yankees", "odds_taken": "-150",
    }
    base.update(kw)
    return base


def test_resolve_parlay_all_win():
    legs = [
        _leg(),
        _leg(sport="basketball_nba", team1="Lakers", team2="Celtics",
             bet_type=BET_TYPE_SPREAD, selection="Lakers -3.5", odds_taken="-110"),
    ]
    games = [
        {"home_team": "Red Sox", "away_team": "Yankees", "home_score": 2, "away_score": 5},
        {"home_team": "Celtics", "away_team": "Lakers", "home_score": 100, "away_score": 110},
    ]
    result, eff = resolve_parlay(legs, games)
    assert result == RESULT_WIN
    assert abs(eff - (_d("-150") * _d("-110"))) < 1e-9


def test_resolve_parlay_one_leg_loses():
    legs = [
        _leg(),
        _leg(sport="basketball_nba", team1="Lakers", team2="Celtics",
             bet_type=BET_TYPE_SPREAD, selection="Lakers -3.5", odds_taken="-110"),
    ]
    games = [
        {"home_team": "Red Sox", "away_team": "Yankees", "home_score": 2, "away_score": 5},
        {"home_team": "Celtics", "away_team": "Lakers", "home_score": 110, "away_score": 100},
    ]
    result, eff = resolve_parlay(legs, games)
    assert result == RESULT_LOSS
    assert eff is None


def test_parlay_with_half_result_leg_routes_to_manual():
    # A quarter-line leg inside a parlay → HALF WIN/LOSS → combine raises → manual
    # (book conventions for parlay half-results vary; accuracy first, trap #1).
    from parlay import combine_parlay_results
    with pytest.raises(ValueError, match="HALF WIN/HALF LOSS"):
        combine_parlay_results([("WIN", 1.91), ("HALF WIN", 1.91)])
    with pytest.raises(ValueError, match="HALF WIN/HALF LOSS"):
        combine_parlay_results([("HALF LOSS", 1.91), ("WIN", 2.5)])


def test_resolve_parlay_with_team_total_leg():
    # A parlay containing a team-total leg resolves per-leg via resolve() (Phase 1).
    legs = [
        _leg(),  # Yankees ML win
        _leg(sport="baseball_mlb", team1="Atlanta Braves", team2="New York Mets",
             bet_type=BET_TYPE_TOTAL, selection="Braves Team Total Over 4.5",
             odds_taken="-110"),
    ]
    games = [
        {"home_team": "Red Sox", "away_team": "Yankees", "home_score": 2, "away_score": 5},
        {"home_team": "New York Mets", "away_team": "Atlanta Braves", "home_score": 3, "away_score": 5},
    ]
    result, eff = resolve_parlay(legs, games)
    assert result == RESULT_WIN               # Braves scored 5 → team Over 4.5 hits
    assert abs(eff - (_d("-150") * _d("-110"))) < 1e-9
