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
    RESULT_HALF_WIN,
    RESULT_HALF_LOSS,
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


# ── Phase 2: quarter-line HALF WIN / HALF LOSS settlement ────────────────────
# A quarter line is two half-stakes at L±0.25 (Phase 0's guard is gone; these are
# the SAME lines, now asserting the combined result). Game: Argentina (home) vs
# Switzerland (away).

@pytest.mark.parametrize("selection,home,away,expected", [
    ("Argentina -0.75", 2, 1, RESULT_HALF_WIN),   # fav wins by exactly 1: -0.5 WIN, -1.0 PUSH
    ("Argentina -0.75", 3, 1, RESULT_WIN),         # wins by 2: both halves win
    ("Argentina -0.75", 1, 1, RESULT_LOSS),        # draw: both halves lose
    ("Switzerland +0.75", 2, 1, RESULT_HALF_LOSS), # dog loses by exactly 1: +0.5 LOSS, +1.0 PUSH
    ("Switzerland +0.75", 1, 1, RESULT_WIN),       # draw: dog +0.75 wins both halves
    ("Switzerland +0.75", 2, 0, RESULT_LOSS),      # loses by 2: both halves lose
    ("Argentina +0.25", 1, 1, RESULT_HALF_WIN),    # +0.25 on a draw: 0.0 PUSH, +0.5 WIN
])
def test_quarter_spread_half_results(selection, home, away, expected):
    assert resolve(
        _bet(bet_type=BET_TYPE_SPREAD, selection=selection,
             team1="Argentina", team2="Switzerland"),
        _game(home_team="Argentina", away_team="Switzerland",
              home_score=home, away_score=away),
    ) == expected


@pytest.mark.parametrize("selection,home,away,expected", [
    ("Over 2.75", 2, 1, RESULT_HALF_WIN),   # total 3: over 2.5 WIN, over 3.0 PUSH
    ("Over 2.75", 3, 1, RESULT_WIN),         # total 4
    ("Over 2.75", 1, 1, RESULT_LOSS),        # total 2
    ("Under 2.25", 1, 1, RESULT_HALF_WIN),   # total 2: under 2.0 PUSH, under 2.5 WIN
    ("Under 2.75", 2, 1, RESULT_HALF_LOSS),  # total 3: under 2.5 LOSS, under 3.0 PUSH
])
def test_quarter_total_half_results(selection, home, away, expected):
    assert resolve(
        _bet(bet_type=BET_TYPE_TOTAL, selection=selection),
        _game(home_score=home, away_score=away),
    ) == expected


def test_quarter_team_total_half_result():
    # Braves (away) scored 5; Team Total Over 4.75 → over 4.5 WIN, over 5.0 PUSH → HALF WIN.
    assert resolve(
        _mlb_bet(selection="Braves Team Total Over 4.75"), _mlb_game(),
    ) == RESULT_HALF_WIN


@pytest.mark.parametrize("selection,scores,expected", [
    ("Kansas City Chiefs -0.5", (24, 17), RESULT_WIN),   # half line settles binary
    ("Buffalo Bills +7", (24, 17), RESULT_PUSH),          # integer line unaffected
])
def test_half_and_integer_spreads_still_settle_binary(selection, scores, expected):
    assert resolve(
        _bet(bet_type=BET_TYPE_SPREAD, selection=selection),
        _game(home_score=scores[0], away_score=scores[1]),
    ) == expected


@pytest.mark.parametrize("selection,expected", [
    ("Over 40.5", RESULT_WIN),   # half line
    ("Over 41", RESULT_PUSH),    # integer line pushes, does NOT trip the guard
    ("Under 42", RESULT_WIN),    # integer line
])
def test_half_and_integer_totals_still_settle_binary(selection, expected):
    assert resolve(
        _bet(bet_type=BET_TYPE_TOTAL, selection=selection),
        _game(home_score=24, away_score=17),
    ) == expected


# ── Phase 1: team totals (settle against ONE team's own score) ───────────────

def _mlb_game(**kwargs):
    base = {
        "home_team": "New York Mets", "away_team": "Atlanta Braves",
        "home_score": 3, "away_score": 5,
    }
    base.update(kwargs)
    return base


def _mlb_bet(**kwargs):
    base = {
        "bet_type": BET_TYPE_TOTAL, "sport": "baseball_mlb",
        "team1": "Atlanta Braves", "team2": "New York Mets",
        "selection": "Braves Team Total Over 4.5",
    }
    base.update(kwargs)
    return base


def test_team_total_over_wins_on_own_score():
    # Braves scored 5 (away) → Over 4.5 wins.
    assert resolve(_mlb_bet(selection="Braves Team Total Over 4.5"), _mlb_game()) == RESULT_WIN


def test_team_total_under_wins_on_own_score():
    # Mets scored 3 (home) → Under 4.5 wins.
    assert resolve(_mlb_bet(selection="Mets Team Total Under 4.5"), _mlb_game()) == RESULT_WIN


def test_both_teams_same_line_settle_independently():
    # THE trap: both teams quoted at 4.5. Each settles vs its OWN score, not the
    # game sum (8). Braves 5 → Over wins; Mets 3 → Over loses.
    g = _mlb_game()
    assert resolve(_mlb_bet(selection="Braves Team Total Over 4.5"), g) == RESULT_WIN
    assert resolve(_mlb_bet(selection="Mets Team Total Over 4.5"), g) == RESULT_LOSS
    # And the Unders mirror them.
    assert resolve(_mlb_bet(selection="Braves Team Total Under 4.5"), g) == RESULT_LOSS
    assert resolve(_mlb_bet(selection="Mets Team Total Under 4.5"), g) == RESULT_WIN


def test_team_total_short_name_matches_full_api_name():
    # Selection "Braves" must map to API "Atlanta Braves" via _resolve_team.
    assert resolve(
        _mlb_bet(selection="Braves Team Total Over 4.5"),
        _mlb_game(home_team="New York Mets", away_team="Atlanta Braves"),
    ) == RESULT_WIN


def test_team_total_integer_line_pushes_on_exact():
    # Braves 5 vs an integer 5 line → PUSH (does not trip the quarter guard).
    assert resolve(_mlb_bet(selection="Braves Team Total Over 5"), _mlb_game()) == RESULT_PUSH


# (quarter team-total settlement is covered by test_quarter_team_total_half_result)


def test_team_total_unmatched_team_routes_to_manual():
    with pytest.raises(ValueError, match="Could not match selection team"):
        resolve(_mlb_bet(selection="Yankees Team Total Over 4.5"), _mlb_game())


def test_plain_game_total_still_resolves_when_not_a_team_total():
    # "Over 8.5" is NOT a team total → falls through to the game-sum path
    # (Braves 5 + Mets 3 = 8 < 8.5 → LOSS). The team-total branch must not hijack it.
    assert resolve(_mlb_bet(selection="Over 8.5"), _mlb_game()) == RESULT_LOSS
    assert resolve(_mlb_bet(selection="Under 8.5"), _mlb_game()) == RESULT_WIN
