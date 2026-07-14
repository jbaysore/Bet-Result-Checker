import poller
from config import RESULT_NEEDS_REVIEW


def _period_bet(market_key):
    return {
        "row_idx": 2,
        "bet_id": "42",
        "sport": "baseball_mlb",
        "book": "fanduel",
        "team1": "Chicago Cubs",
        "team2": "New York Yankees",
        "game_date": "2020-07-14",
        "game_start": "19:05",
        "selection": "Over 4.5",
        "bet_type": "Total",
        "market_key": market_key,
        "is_prop_entry": False,
        "is_parlay": False,
        "kalshi_ticker": "",
    }


def test_first_five_total_is_not_settled_from_full_game_score(monkeypatch):
    monkeypatch.setattr(poller, "_fetch_result", lambda bet, sport: {
        "home_team": "New York Yankees",
        "away_team": "Chicago Cubs",
        "home_score": 6,
        "away_score": 4,
    })
    writes = []
    monkeypatch.setattr(
        poller, "write_result",
        lambda row_idx, result, bet_id, **kwargs: writes.append((row_idx, result, bet_id)),
    )

    status = poller.poll_bet(_period_bet("totals_1st_5_innings"))

    assert status == "needs_review"
    assert writes == [(2, RESULT_NEEDS_REVIEW, "42")]


def test_period_market_registry_covers_both_new_markets():
    assert poller.PERIOD_MARKETS_REQUIRING_MANUAL_SCORE == {
        "spreads_h1",
        "totals_1st_5_innings",
    }
