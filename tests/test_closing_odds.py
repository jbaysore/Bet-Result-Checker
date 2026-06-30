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
    is_exchange_book,
)
from config import CLOSING_ODDS_MANUAL_REQUIRED, CLOSING_ODDS_SPORT_NOT_ON_API


def test_region_for_book_key():
    assert region_for_book_key("fanatics") == "us"
    assert region_for_book_key("kalshi") == "us_ex"
    assert region_for_book_key("espnbet") == "us2"


def test_parse_selection_moneyline():
    sel = parse_selection("Moneyline", "Kansas City Chiefs")
    assert sel["market"] == "h2h"
    assert sel["selection_team"] == "Kansas City Chiefs"


def test_parse_selection_spread():
    sel = parse_selection("Spread", "Chiefs -3.5")
    assert sel["market"] == "spreads"
    assert sel["selection_team"] == "Chiefs"
    assert sel["selection_point"] == -3.5


def test_parse_selection_total():
    sel = parse_selection("Total", "Over 47.5")
    assert sel["market"] == "totals"
    assert sel["selection_side"] == "over"
    assert sel["selection_point"] == 47.5


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


def test_fmt_odds():
    assert fmt_odds(150) == "+150"
    assert fmt_odds(-110) == "-110"


def test_clv_math():
    dec_taken = to_decimal_odds(150)   # 2.5
    dec_close = to_decimal_odds(-110)  # ~1.909
    clv_pct = calc_clv(dec_taken, dec_close)
    assert clv_pct is not None
    assert clv_for_sheet(clv_pct) == round(clv_pct / 100, 6)


def test_is_exchange_book():
    assert is_exchange_book("kalshi")
    assert not is_exchange_book("fanatics")


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
