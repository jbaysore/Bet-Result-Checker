"""WIN/LOSS/PUSH/VOID resolution from final scores."""

import pytest

from config import (
    BET_TYPE_DRAW,
    BET_TYPE_MONEYLINE,
    BET_TYPE_SPREAD,
    BET_TYPE_TOTAL,
    GAME_STATUS_CANCELLED,
    RESULT_LOSS,
    RESULT_PUSH,
    RESULT_VOID,
    RESULT_WIN,
)
from resolver import resolve


def _bet(**kwargs):
    base = {
        "bet_type": BET_TYPE_MONEYLINE,
        "selection": "Kansas City Chiefs",
        "team1": "Kansas City Chiefs",
        "team2": "Buffalo Bills",
        "sport": "americanfootball_nfl",
    }
    base.update(kwargs)
    return base


def _game(**kwargs):
    base = {
        "home_team": "Kansas City Chiefs",
        "away_team": "Buffalo Bills",
        "home_score": 24,
        "away_score": 17,
    }
    base.update(kwargs)
    return base


def test_cancelled_game_is_void_before_scores():
    assert resolve(_bet(), {"status": GAME_STATUS_CANCELLED}) == RESULT_VOID


def test_moneyline_home_win():
    assert resolve(_bet(selection="Kansas City Chiefs"), _game()) == RESULT_WIN


def test_moneyline_away_loss():
    assert resolve(
        _bet(selection="Buffalo Bills"),
        _game(),
    ) == RESULT_LOSS


def test_moneyline_nfl_tie_is_push():
    assert resolve(
        _bet(selection="Kansas City Chiefs"),
        _game(home_score=17, away_score=17),
    ) == RESULT_PUSH


def test_soccer_team_bet_on_draw_is_loss_not_push():
    assert resolve(
        _bet(
            selection="Kansas City Chiefs",
            sport="soccer_epl",
            team1="Arsenal",
            team2="Chelsea",
        ),
        _game(
            home_team="Arsenal",
            away_team="Chelsea",
            home_score=1,
            away_score=1,
        ),
    ) == RESULT_LOSS


def test_soccer_draw_selection_wins_on_tie():
    assert resolve(
        _bet(
            bet_type=BET_TYPE_DRAW,
            selection="Draw",
            sport="soccer_epl",
            team1="Arsenal",
            team2="Chelsea",
        ),
        _game(
            home_team="Arsenal",
            away_team="Chelsea",
            home_score=2,
            away_score=2,
        ),
    ) == RESULT_WIN


def test_spread_cover():
    assert resolve(
        _bet(
            bet_type=BET_TYPE_SPREAD,
            selection="Kansas City Chiefs -3.5",
        ),
        _game(home_score=24, away_score=17),
    ) == RESULT_WIN


def test_spread_no_cover():
    assert resolve(
        _bet(
            bet_type=BET_TYPE_SPREAD,
            selection="Buffalo Bills +3",
        ),
        _game(home_score=24, away_score=17),
    ) == RESULT_LOSS


def test_spread_underdog_covers_on_push_line():
    assert resolve(
        _bet(
            bet_type=BET_TYPE_SPREAD,
            selection="Buffalo Bills +7",
        ),
        _game(home_score=24, away_score=17),
    ) == RESULT_PUSH


def test_spread_push():
    assert resolve(
        _bet(
            bet_type=BET_TYPE_SPREAD,
            selection="Kansas City Chiefs -7",
        ),
        _game(home_score=24, away_score=17),
    ) == RESULT_PUSH


def test_total_over_win():
    assert resolve(
        _bet(
            bet_type=BET_TYPE_TOTAL,
            selection="Over 40.5",
        ),
        _game(home_score=24, away_score=17),
    ) == RESULT_WIN


def test_total_under_loss():
    assert resolve(
        _bet(
            bet_type=BET_TYPE_TOTAL,
            selection="Under 40.5",
        ),
        _game(home_score=24, away_score=17),
    ) == RESULT_LOSS


def test_total_push():
    assert resolve(
        _bet(
            bet_type=BET_TYPE_TOTAL,
            selection="Over 41",
        ),
        _game(home_score=24, away_score=17),
    ) == RESULT_PUSH


def test_fuzzy_team_name_match():
    assert resolve(
        _bet(selection="Chiefs"),
        _game(home_team="Kansas City Chiefs", away_team="Buffalo Bills"),
    ) == RESULT_WIN


def test_unrecognised_bet_type_raises():
    with pytest.raises(ValueError, match="Unrecognised bet type"):
        resolve(_bet(bet_type="Prop"), _game())
