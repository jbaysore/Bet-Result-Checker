"""Unit tests for sources.odds_api — no live API key required."""

from unittest.mock import MagicMock, patch

import sources.odds_api as odds_api


def _mock_scores_response(games):
    resp = MagicMock()
    resp.status_code = 200
    resp.headers = {"x-requests-remaining": "99", "x-requests-used": "1"}
    resp.json.return_value = games
    resp.raise_for_status = MagicMock()
    return resp


@patch("sources.odds_api.requests.get")
def test_scores_fetched_once_per_sport_per_run(mock_get):
    odds_api._scores_cache.clear()
    odds_api._no_scores_feed.clear()
    odds_api._active_sports_cache = {"americanfootball_nfl"}

    games = [{
        "completed": True,
        "home_team": "Kansas City Chiefs",
        "away_team": "Las Vegas Raiders",
        "scores": [
            {"name": "Kansas City Chiefs", "score": "27"},
            {"name": "Las Vegas Raiders", "score": "14"},
        ],
    }]
    mock_get.return_value = _mock_scores_response(games)

    r1 = odds_api.get_game_result("americanfootball_nfl", "Chiefs", "Raiders")
    r2 = odds_api.get_game_result("americanfootball_nfl", "Chiefs", "Raiders")

    assert r1 is not None
    assert r1["home_score"] == 27
    assert r2 is not None
    assert mock_get.call_count == 1


@patch("sources.odds_api.requests.get")
def test_scores_skips_inactive_sport_without_paid_call(mock_get):
    odds_api._scores_cache.clear()
    odds_api._no_scores_feed.clear()
    odds_api._active_sports_cache = {"americanfootball_nfl"}

    result = odds_api.get_game_result("soccer_fifa_world_cup", "Argentina", "Jordan")

    assert result is None
    mock_get.assert_not_called()
