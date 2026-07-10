"""Unit tests for closing_odds.py — no API or Sheets credentials required."""

from unittest.mock import patch

from closing_odds import (
    parse_selection,
    find_event,
    extract_odds,
    fmt_odds,
    to_decimal_odds,
    calc_clv,
    clv_for_sheet,
    region_for_book_key,
    fetch_closing_odds,
    fetch_live_closing_odds,
    _fetch_closing_price,
    is_exchange_book,
    needs_manual_closing_odds,
)
from config import (
    CLOSING_ODDS_MANUAL_REQUIRED,
    CLOSING_ODDS_SPORT_NOT_ON_API,
    CLOSING_ODDS_SELECTION_NOT_FOUND,
)


def test_region_for_book_key():
    assert region_for_book_key("fanatics") == "us"
    assert region_for_book_key("kalshi") == "us_ex"
    assert region_for_book_key("espnbet") == "us2"
    assert region_for_book_key("prophetx") == "us_ex"


def test_parse_selection_moneyline():
    sel = parse_selection("Moneyline", "Kansas City Chiefs")
    assert sel["markets_to_try"] == ["h2h"]
    assert sel["extract_mode"] == "h2h"
    assert sel["selection_team"] == "Kansas City Chiefs"


def test_parse_selection_spread():
    sel = parse_selection("Spread", "Chiefs -3.5")
    assert sel["markets_to_try"] == ["spreads", "alternate_spreads"]
    assert sel["extract_mode"] == "spread"
    assert sel["selection_team"] == "Chiefs"
    assert sel["selection_point"] == -3.5


def test_parse_selection_total():
    sel = parse_selection("Total", "Over 47.5")
    assert sel["markets_to_try"] == ["totals", "alternate_totals"]
    assert sel["extract_mode"] == "total"
    assert sel["selection_side"] == "over"
    assert sel["selection_point"] == 47.5


def test_parse_selection_team_total():
    sel = parse_selection("Total", "Braves Team Total Over 4.5")
    assert sel["markets_to_try"] == ["team_totals"]
    assert sel["extract_mode"] == "team_total"
    assert sel["selection_team"] == "Braves"
    assert sel["selection_side"] == "over"
    assert sel["selection_point"] == 4.5


def test_parse_selection_unsupported():
    assert parse_selection("Parlay", "Parlay") is None


def test_find_event_substring_match():
    events = [{"home_team": "Kansas City Chiefs", "away_team": "Las Vegas Raiders"}]
    ev = find_event(events, "Chiefs", "Raiders")
    assert ev is not None


def test_extract_odds_h2h():
    outcomes = [
        {"name": "Kansas City Chiefs", "price": -150},
        {"name": "Las Vegas Raiders", "price": 130},
    ]
    assert extract_odds("h2h", outcomes, "Chiefs", None, None) == -150


def test_extract_odds_spread_integer_point():
    outcomes = [
        {"name": "Kansas City Chiefs", "price": -110, "point": -3},
    ]
    assert extract_odds("spread", outcomes, "Chiefs", None, -3.0) == -110


def test_extract_odds_team_total():
    outcomes = [
        {
            "name": "Over",
            "description": "Atlanta Braves",
            "point": 4.5,
            "price": -115,
        },
    ]
    assert extract_odds("team_total", outcomes, "Braves", "over", 4.5) == -115


def test_fmt_odds():
    assert fmt_odds(150) == "+150"
    assert fmt_odds(-110) == "-110"


def test_clv_math():
    dec_taken = to_decimal_odds(150)
    dec_close = to_decimal_odds(-110)
    clv_pct = calc_clv(dec_taken, dec_close)
    assert clv_pct is not None
    assert clv_for_sheet(clv_pct) == round(clv_pct / 100, 6)


def test_is_exchange_book():
    assert is_exchange_book("kalshi")
    assert is_exchange_book("prophetx")
    assert not is_exchange_book("fanatics")


def test_needs_manual_closing_odds_excludes_prophetx():
    assert needs_manual_closing_odds("kalshi")
    assert needs_manual_closing_odds("polymarket")
    assert needs_manual_closing_odds("novig")
    assert needs_manual_closing_odds("betopenly")
    assert not needs_manual_closing_odds("prophetx")
    assert not needs_manual_closing_odds("fanatics")


@patch("closing_odds.sport_has_odds_feed", return_value=True)
@patch("closing_odds._fetch_historical_snapshot")
def test_fetch_closing_odds_queries_prophetx(mock_snapshot, _mock_feed):
    mock_snapshot.return_value = [{
        "home_team": "Argentina",
        "away_team": "Jordan",
        "bookmakers": [{
            "key": "prophetx",
            "markets": [{
                "key": "h2h",
                "outcomes": [
                    {"name": "Argentina", "price": -140},
                    {"name": "Jordan", "price": 120},
                ],
            }],
        }],
    }]
    result = fetch_closing_odds({
        "bet_id": "201",
        "sport": "soccer_fifa_world_cup",
        "book": "prophetx",
        "team1": "Argentina",
        "team2": "Jordan",
        "game_date": "6/15/2026",
        "game_start": "2:00 PM",
        "bet_type": "Moneyline",
        "selection": "Argentina",
        "odds_taken": "-150",
    })
    assert mock_snapshot.called
    assert result["error"] is None
    assert result["closing_odds"] == "-140"


@patch("closing_odds.sport_has_odds_feed", return_value=True)
@patch("closing_odds._fetch_historical_snapshot")
def test_fetch_closing_odds_cascades_to_alternate_spread(mock_snapshot, _mock_feed):
    def side_effect(sport, date_iso, book, market):
        if market == "spreads":
            return [{
                "home_team": "Chicago Cubs",
                "away_team": "St. Louis Cardinals",
                "bookmakers": [{
                    "key": "draftkings",
                    "markets": [{
                        "key": "spreads",
                        "outcomes": [
                            {"name": "Chicago Cubs", "point": -1.5, "price": -110},
                        ],
                    }],
                }],
            }]
        if market == "alternate_spreads":
            return [{
                "home_team": "Chicago Cubs",
                "away_team": "St. Louis Cardinals",
                "bookmakers": [{
                    "key": "draftkings",
                    "markets": [{
                        "key": "alternate_spreads",
                        "outcomes": [
                            {"name": "Chicago Cubs", "point": -7.5, "price": +150},
                        ],
                    }],
                }],
            }]
        return []

    mock_snapshot.side_effect = side_effect
    result = fetch_closing_odds({
        "bet_id": "301",
        "sport": "baseball_mlb",
        "book": "draftkings",
        "team1": "Cubs",
        "team2": "Cardinals",
        "game_date": "7/8/2026",
        "game_start": "7:05 PM",
        "bet_type": "Spread",
        "selection": "Cubs -7.5",
        "odds_taken": "+140",
    })
    assert mock_snapshot.call_count == 2
    assert result["error"] is None
    assert result["closing_odds"] == "+150"


@patch("closing_odds.sport_has_odds_feed", return_value=True)
@patch("closing_odds._fetch_historical_snapshot")
def test_fetch_closing_odds_uses_explicit_market_key(mock_snapshot, _mock_feed):
    mock_snapshot.return_value = [{
        "home_team": "Chicago Cubs",
        "away_team": "St. Louis Cardinals",
        "bookmakers": [{
            "key": "draftkings",
            "markets": [{
                "key": "alternate_spreads",
                "outcomes": [
                    {"name": "Chicago Cubs", "point": -7.5, "price": +145},
                ],
            }],
        }],
    }]
    result = fetch_closing_odds({
        "bet_id": "302",
        "sport": "baseball_mlb",
        "book": "draftkings",
        "team1": "Cubs",
        "team2": "Cardinals",
        "game_date": "7/8/2026",
        "game_start": "7:05 PM",
        "bet_type": "Spread",
        "selection": "Cubs -7.5",
        "odds_taken": "+140",
        "market_key": "alternate_spreads",
    })
    mock_snapshot.assert_called_once()
    assert mock_snapshot.call_args[0][3] == "alternate_spreads"
    assert result["closing_odds"] == "+145"


@patch("closing_odds.sport_has_odds_feed", return_value=True)
def test_fetch_closing_odds_skips_kalshi_without_api(_mock_feed):
    result = fetch_closing_odds({
        "bet_id": "103",
        "sport": "soccer_fifa_world_cup",
        "book": "kalshi",
        "team1": "Argentina",
        "team2": "Jordan",
        "game_date": "6/15/2026",
        "game_start": "2:00 PM",
        "bet_type": "Moneyline",
        "selection": "Argentina",
        "odds_taken": "-150",
    })
    assert result["error"] == CLOSING_ODDS_MANUAL_REQUIRED
    assert result["closing_odds"] is None


@patch("closing_odds.sport_has_odds_feed", return_value=False)
def test_fetch_closing_odds_skips_inactive_sport(_mock_feed):
    result = fetch_closing_odds({
        "bet_id": "99",
        "sport": "soccer_fifa_world_cup",
        "book": "fanatics",
        "team1": "Argentina",
        "team2": "Jordan",
        "game_date": "6/15/2026",
        "game_start": "2:00 PM",
        "bet_type": "Moneyline",
        "selection": "Argentina",
        "odds_taken": "-150",
    })
    assert result["error"] == CLOSING_ODDS_SPORT_NOT_ON_API


@patch("closing_odds.sport_has_odds_feed", return_value=True)
@patch("closing_odds._fetch_historical_snapshot")
def test_fetch_closing_price_all_markets_miss_selection(mock_snapshot, _mock_feed):
    mock_snapshot.return_value = [{
        "home_team": "Chicago Cubs",
        "away_team": "St. Louis Cardinals",
        "bookmakers": [{
            "key": "draftkings",
            "markets": [
                {"key": "spreads", "outcomes": []},
                {"key": "alternate_spreads", "outcomes": []},
            ],
        }],
    }]
    res = _fetch_closing_price({
        "sport": "baseball_mlb",
        "book": "draftkings",
        "team1": "Cubs",
        "team2": "Cardinals",
        "game_date": "7/8/2026",
        "game_start": "7:05 PM",
        "bet_type": "Spread",
        "selection": "Cubs -7.5",
    }, "BetID 9")
    assert res["error"] == CLOSING_ODDS_SELECTION_NOT_FOUND


@patch("closing_odds._fetch_live_snapshot")
def test_fetch_live_closing_odds_reuses_selection_extraction(mock_snapshot):
    mock_snapshot.return_value = [{
        "home_team": "Chicago Cubs",
        "away_team": "St. Louis Cardinals",
        "bookmakers": [{
            "key": "draftkings",
            "markets": [{
                "key": "alternate_spreads",
                "outcomes": [{"name": "Chicago Cubs", "point": -7.5, "price": 145}],
            }],
        }],
    }]
    result = fetch_live_closing_odds({
        "bet_id": "302", "sport": "baseball_mlb", "book": "draftkings",
        "team1": "Cubs", "team2": "Cardinals", "bet_type": "Spread",
        "selection": "Cubs -7.5", "market_key": "alternate_spreads",
    })
    assert result == {"closing_odds": "+145", "decimal_closing": 2.45, "error": None}
