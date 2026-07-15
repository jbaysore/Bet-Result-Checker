import json
from pathlib import Path

from pinnacle_closing import implied_probability, pinnacle_quote_for_bet, power_devig


def test_power_devig_matches_shared_js_vectors():
    vectors = json.loads((Path(__file__).parent / "fixtures" / "power_devig_vectors.json").read_text())
    for vector in vectors:
        odds = vector["american"]
        fair = power_devig([implied_probability(value) for value in odds])
        assert abs(sum(fair) - 1) < 1e-9
        assert all(abs(a - b) < 1e-12 for a, b in zip(fair, vector["fair"]))


def test_pinnacle_requires_exact_point_and_devigs_full_market():
    events = [{
        "id": "event-1", "home_team": "Home", "away_team": "Away",
        "bookmakers": [{"key": "pinnacle", "last_update": "2026-07-15T19:58:00Z", "markets": [{
            "key": "totals", "last_update": "2026-07-15T19:59:00Z", "outcomes": [
                {"name": "Over", "point": 8.5, "price": -115},
                {"name": "Under", "point": 8.5, "price": -105},
                {"name": "Over", "point": 9.5, "price": 120},
                {"name": "Under", "point": 9.5, "price": -140},
            ],
        }]}],
    }]
    quote = pinnacle_quote_for_bet(events, {
        "event_id": "event-1", "bet_type": "Total", "selection": "Over 8.5",
        "team1": "Away", "team2": "Home",
    })
    assert quote is not None
    assert len(quote["outcomes"]) == 2
    assert quote["book_last_update"] == "2026-07-15T19:59:00Z"
    assert pinnacle_quote_for_bet(events, {
        "event_id": "wrong-event", "bet_type": "Total", "selection": "Over 8.5",
        "team1": "Away", "team2": "Home",
    }) is None
    assert pinnacle_quote_for_bet(events, {
        "event_id": "event-1", "bet_type": "Total", "selection": "Over 10.5",
        "team1": "Away", "team2": "Home",
    }) is None
