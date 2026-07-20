"""Unit tests for closing_odds.py — no API or Sheets credentials required."""

from datetime import datetime, timezone
from unittest.mock import Mock, patch

import closing_odds as closing_odds_module

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
    fetch_parlay_closing_odds,
    _fetch_closing_price,
    _fetch_live_snapshot,
    _apply_live_market_family,
    _price_from_snapshot,
    is_exchange_book,
    needs_manual_closing_odds,
)
from actual_start import ActualStartResult
from config import (
    CLOSING_ODDS_MANUAL_REQUIRED,
    CLOSING_ODDS_SPORT_NOT_ON_API,
    CLOSING_ODDS_SELECTION_NOT_FOUND,
    CLOSING_ODDS_GAME_NOT_FOUND,
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


def test_find_event_doubleheader_picks_closest_commence():
    # Same matchup twice in one snapshot (an MLB doubleheader). Without an
    # expected start the old matcher returned the FIRST — here the later,
    # not-yet-started nightcap — and recorded its non-closing price.
    from datetime import datetime, timezone
    events = [
        {"home_team": "Chicago Cubs", "away_team": "New York Mets",
         "commence_time": "2026-07-11T00:05:00Z", "id": "nightcap"},
        {"home_team": "Chicago Cubs", "away_team": "New York Mets",
         "commence_time": "2026-07-10T17:20:00Z", "id": "opener"},
    ]
    opener_start = datetime(2026, 7, 10, 17, 20, tzinfo=timezone.utc)
    assert find_event(events, "Cubs", "Mets", opener_start)["id"] == "opener"
    nightcap_start = datetime(2026, 7, 11, 0, 5, tzinfo=timezone.utc)
    assert find_event(events, "Cubs", "Mets", nightcap_start)["id"] == "nightcap"


def test_find_event_ambiguous_without_expected_start_defers():
    # Two same-name games and no start to disambiguate → defer, don't guess.
    events = [
        {"home_team": "Chicago Cubs", "away_team": "New York Mets", "id": "a"},
        {"home_team": "Chicago Cubs", "away_team": "New York Mets", "id": "b"},
    ]
    assert find_event(events, "Cubs", "Mets") is None


def test_find_event_lone_match_returns_without_commence():
    # A single name match is unambiguous even with no commence_time on the event.
    from datetime import datetime, timezone
    events = [{"home_team": "Kansas City Chiefs", "away_team": "Las Vegas Raiders"}]
    start = datetime(2026, 7, 10, 17, 20, tzinfo=timezone.utc)
    assert find_event(events, "Chiefs", "Raiders", start) is not None


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


def test_needs_manual_closing_odds_excludes_api_supported_exchanges():
    assert needs_manual_closing_odds("kalshi")
    assert needs_manual_closing_odds("polymarket")
    assert needs_manual_closing_odds("betopenly")
    assert not needs_manual_closing_odds("novig")
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
def test_fetch_closing_odds_queries_novig(mock_snapshot, _mock_feed):
    mock_snapshot.return_value = [{
        "home_team": "Chicago Cubs",
        "away_team": "St. Louis Cardinals",
        "bookmakers": [{
            "key": "novig",
            "markets": [{
                "key": "h2h",
                "outcomes": [
                    {"name": "Chicago Cubs", "price": -105},
                    {"name": "St. Louis Cardinals", "price": -115},
                ],
            }],
        }],
    }]
    result = fetch_closing_odds({
        "bet_id": "202",
        "sport": "baseball_mlb",
        "book": "novig",
        "team1": "Chicago Cubs",
        "team2": "St. Louis Cardinals",
        "game_date": "7/10/2026",
        "game_start": "7:05 PM",
        "bet_type": "Moneyline",
        "selection": "Chicago Cubs",
        "odds_taken": "+100",
    })
    assert mock_snapshot.called
    assert mock_snapshot.call_args.args[2] == "novig"
    assert result["error"] is None
    assert result["closing_odds"] == "-105"


@patch("closing_odds.sport_has_odds_feed", return_value=True)
@patch("closing_odds._fetch_historical_event_snapshot")
@patch("closing_odds._fetch_historical_snapshot")
def test_fetch_closing_odds_cascades_to_alternate_spread(mock_snapshot, mock_event_snapshot, _mock_feed):
    # Mainline miss on the sport-level endpoint …
    mock_snapshot.return_value = [{
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
    # … cascades to alternate_spreads via the event-scoped endpoint.
    mock_event_snapshot.return_value = [{
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
    assert mock_snapshot.call_count == 1
    assert mock_event_snapshot.call_count == 1
    assert mock_event_snapshot.call_args[0][3] == "alternate_spreads"
    assert result["error"] is None
    assert result["closing_odds"] == "+150"


@patch("closing_odds.sport_has_odds_feed", return_value=True)
@patch("closing_odds._fetch_historical_event_snapshot")
@patch("closing_odds._fetch_historical_snapshot")
def test_fetch_closing_odds_uses_explicit_market_key(mock_snapshot, mock_event_snapshot, _mock_feed):
    mock_event_snapshot.return_value = [{
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
    mock_event_snapshot.assert_called_once()
    assert mock_event_snapshot.call_args[0][3] == "alternate_spreads"
    assert not mock_snapshot.called  # alternates would 422 on the sport-level endpoint
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


@patch("closing_odds.sport_has_odds_feed", return_value=False)
@patch("closing_odds._fetch_historical_snapshot", return_value=[])
def test_confident_historical_recovery_bypasses_current_active_sport_gate(
    mock_snapshot, _mock_feed,
):
    result = _fetch_closing_price({
        "sport": "tennis_atp_wimbledon",
        "book": "fanatics",
        "team1": "Stefanos Tsitsipas",
        "team2": "Hugo Gaston",
        "game_date": "6/29/2026",
        "game_start": "10:00 AM",
        "bet_type": "Moneyline",
        "selection": "Stefanos Tsitsipas",
        "actual_start": "2026-06-29T14:30:00Z",
        "actual_start_confidence": "CONFIDENT",
    }, "BetID 106")
    mock_snapshot.assert_called_once()
    assert result["error"] == CLOSING_ODDS_GAME_NOT_FOUND


@patch("closing_odds._fetch_historical_snapshot")
def test_manual_sport_alias_never_calls_historical_endpoint(mock_snapshot):
    result = _fetch_closing_price({
        "sport": "manual_wta_libema",
        "book": "draftkings",
        "team1": "Anastasia Potapova",
        "team2": "Suzan Lamens",
        "game_date": "6/10/2026",
        "game_start": "4:10 AM",
        "bet_type": "Moneyline",
        "selection": "Anastasia Potapova",
        "actual_start": "2026-06-10T09:05:00Z",
        "actual_start_confidence": "CONFIDENT",
    }, "BetID 8")
    mock_snapshot.assert_not_called()
    assert result["error"] == CLOSING_ODDS_SPORT_NOT_ON_API


def test_request_error_redacts_api_key():
    # closing_odds re-exports the shared helper; full coverage lives in test_redact.py
    rendered = closing_odds_module._redact_request_error(RuntimeError(
        "404 for https://api.example.test/odds?apiKey=top-secret&markets=h2h",
    ))
    assert "top-secret" not in rendered
    assert "apiKey=[REDACTED]" in rendered


@patch("closing_odds.sport_has_odds_feed", return_value=True)
@patch("closing_odds._fetch_historical_event_snapshot")
@patch("closing_odds._fetch_historical_snapshot")
def test_fetch_closing_price_all_markets_miss_selection(mock_snapshot, mock_event_snapshot, _mock_feed):
    snapshot = [{
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
    mock_snapshot.return_value = snapshot
    mock_event_snapshot.return_value = snapshot
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
    assert result["closing_odds"] == "+145"
    assert result["decimal_closing"] == 2.45
    assert result["error"] is None
    assert result["fetched_at"].endswith("Z")


@patch("closing_odds._fetch_live_snapshot")
def test_fetch_live_closing_odds_queries_novig(mock_snapshot):
    mock_snapshot.return_value = [{
        "home_team": "Chicago Cubs",
        "away_team": "St. Louis Cardinals",
        "bookmakers": [{
            "key": "novig",
            "markets": [{
                "key": "h2h",
                "outcomes": [{"name": "Chicago Cubs", "price": -102}],
            }],
        }],
    }]
    result = fetch_live_closing_odds({
        "bet_id": "303", "sport": "baseball_mlb", "book": "novig",
        "team1": "Chicago Cubs", "team2": "St. Louis Cardinals",
        "bet_type": "Moneyline", "selection": "Chicago Cubs",
        "market_key": "h2h",
    })
    assert mock_snapshot.call_args.args[1] == "novig"
    assert result["closing_odds"] == "-102"
    assert result["decimal_closing"] == 1.98039216
    assert result["error"] is None


def test_live_market_key_cascades_to_related_market_family():
    total = parse_selection("Total", "Under 173")
    assert _apply_live_market_family(total, "totals")["markets_to_try"] == [
        "totals", "alternate_totals",
    ]
    assert _apply_live_market_family(total, "alternate_totals")["markets_to_try"] == [
        "alternate_totals", "totals",
    ]
    spread = parse_selection("Spread", "Liberty +5.5")
    assert _apply_live_market_family(spread, "spreads")["markets_to_try"] == [
        "spreads", "alternate_spreads",
    ]


@patch("closing_odds._fetch_live_snapshot")
def test_live_closing_falls_through_mainline_to_exact_alternate(mock_snapshot):
    mock_snapshot.side_effect = [
        [{
            "home_team": "Minnesota Lynx", "away_team": "New York Liberty",
            "bookmakers": [{"key": "draftkings", "markets": [{
                "key": "totals", "outcomes": [
                    {"name": "Under", "point": 175.5, "price": -110},
                ],
            }]}],
        }],
        [{
            "home_team": "Minnesota Lynx", "away_team": "New York Liberty",
            "bookmakers": [{"key": "draftkings", "markets": [{
                "key": "alternate_totals", "outcomes": [
                    {"name": "Under", "point": 173, "price": 105},
                ],
            }]}],
        }],
    ]
    result = fetch_live_closing_odds({
        "bet_id": "294", "sport": "basketball_wnba", "book": "draftkings",
        "team1": "New York Liberty", "team2": "Minnesota Lynx",
        "bet_type": "Total", "selection": "Under 173", "market_key": "totals",
    })
    assert [call.args[2] for call in mock_snapshot.call_args_list] == [
        "totals", "alternate_totals",
    ]
    assert result["closing_odds"] == "+105"
    assert result["decimal_closing"] == 2.05
    assert result["error"] is None


@patch("closing_odds.requests.get")
def test_additional_live_market_uses_event_specific_endpoint(mock_get):
    closing_odds_module._live_events_cache.clear()
    closing_odds_module._live_snapshot_cache.clear()
    events_response = Mock(status_code=200)
    events_response.json.return_value = [{
        "id": "event-123", "home_team": "Los Angeles Dodgers",
        "away_team": "Arizona Diamondbacks",
    }]
    events_response.raise_for_status.return_value = None
    odds_response = Mock(status_code=200, headers={})
    odds_response.json.return_value = {
        "id": "event-123", "home_team": "Los Angeles Dodgers",
        "away_team": "Arizona Diamondbacks", "bookmakers": [],
    }
    odds_response.raise_for_status.return_value = None
    mock_get.side_effect = [events_response, odds_response]

    result = _fetch_live_snapshot(
        "baseball_mlb", "fanduel", "team_totals",
        "Arizona Diamondbacks", "Los Angeles Dodgers",
    )

    assert len(result) == 1
    assert mock_get.call_args_list[0].args[0].endswith("/sports/baseball_mlb/events")
    assert mock_get.call_args_list[1].args[0].endswith(
        "/sports/baseball_mlb/events/event-123/odds"
    )


def test_live_missing_exact_point_reports_available_points():
    sel = _apply_live_market_family(parse_selection("Total", "Under 173"), "totals")
    result = _price_from_snapshot(
        [{
            "home_team": "Minnesota Lynx", "away_team": "New York Liberty",
            "bookmakers": [{"key": "draftkings", "markets": [{
                "key": "totals", "outcomes": [
                    {"name": "Under", "point": 175.5, "price": -110},
                    {"name": "Over", "point": 175.5, "price": -110},
                ],
            }]}],
        }],
        "basketball_wnba", "draftkings", "New York Liberty", "Minnesota Lynx",
        "totals", sel, "BetID 294 live", diagnose_missing=True,
    )
    assert result["error"] == (
        "EXACT SELECTION NOT FOUND: totals point 173.0; available points: 175.5"
    )


def test_parlay_summary_uses_worst_leg_quality_and_per_leg_start_status(monkeypatch):
    monkeypatch.setattr(
        closing_odds_module,
        "resolve_actual_start",
        lambda leg: ActualStartResult(
            datetime(2026, 7, 11, 0, 5, tzinfo=timezone.utc),
            "fixture", "CONFIDENT", event_id=leg.get("event_id", ""),
        ),
    )
    qualities = iter(["VERIFIED_CLOSE", "STALE"])
    monkeypatch.setattr(
        closing_odds_module,
        "_fetch_closing_price",
        lambda leg, label: {
            "price": -110, "error": None, "closing_quality": next(qualities),
            "snapshot_at": "2026-07-11T00:03:30Z",
            "book_last_update": "2026-07-11T00:03:00Z",
        },
    )
    result = fetch_parlay_closing_odds({
        "bet_id": "p1", "odds_taken": "+264", "_resolve_actual_start": True,
        "legs": [{"event_id": "e1"}, {"event_id": "e2"}],
    })
    assert result["closing_quality"] == "STALE"
    assert result["start_status"] == "VERIFIED"
    assert [leg["start_status"] for leg in result["per_leg_audit"]] == ["VERIFIED", "VERIFIED"]


# ── Event-scoped historical markets (alternate_*/team_totals) ────────────────
# The sport-level historical endpoint 422s on additional markets; they must
# route through /historical/sports/{sport}/events/{id}/odds.

TEAM_TOTAL_BET = {
    "bet_id": "371",
    "sport": "basketball_wnba",
    "book": "betrivers",
    "team1": "Los Angeles Sparks",
    "team2": "Minnesota Lynx",
    "game_date": "7/9/2026",
    "game_start": "7:00 PM",
    "bet_type": "Total",
    "selection": "Minnesota Lynx Team Total Over 42.5",
    "odds_taken": "+102",
}


def _clear_historical_caches():
    closing_odds_module._snapshot_cache.clear()
    closing_odds_module._historical_events_cache.clear()


@patch("closing_odds.sport_has_odds_feed", return_value=True)
@patch("closing_odds._fetch_historical_event_snapshot")
@patch("closing_odds._fetch_historical_snapshot")
def test_additional_market_routes_to_event_snapshot(mock_sport_level, mock_event_level, _mock_feed):
    mock_event_level.return_value = [{
        "id": "wnba-evt-1",
        "home_team": "Minnesota Lynx",
        "away_team": "Los Angeles Sparks",
        "bookmakers": [{
            "key": "betrivers",
            "markets": [{
                "key": "team_totals",
                "outcomes": [
                    {"name": "Over", "description": "Minnesota Lynx", "point": 42.5, "price": -105},
                    {"name": "Under", "description": "Minnesota Lynx", "point": 42.5, "price": -115},
                ],
            }],
        }],
    }]
    result = fetch_closing_odds(TEAM_TOTAL_BET)
    assert result["error"] is None
    assert result["closing_odds"] == "-105"
    assert mock_event_level.called
    # team_totals must never hit the sport-level endpoint (it would 422).
    assert not mock_sport_level.called


@patch("closing_odds.requests.get")
def test_event_snapshot_fetches_roster_then_event_odds(mock_get):
    _clear_historical_caches()
    roster_resp = Mock(status_code=200, headers={})
    roster_resp.json.return_value = {"data": [{
        "id": "wnba-evt-1",
        "home_team": "Minnesota Lynx",
        "away_team": "Los Angeles Sparks",
        "commence_time": "2026-07-10T00:00:00Z",
    }]}
    odds_resp = Mock(status_code=200, headers={})
    odds_resp.json.return_value = {
        "timestamp": "2026-07-09T23:56:12Z",
        "data": {
            "id": "wnba-evt-1",
            "home_team": "Minnesota Lynx",
            "away_team": "Los Angeles Sparks",
            "bookmakers": [{"key": "betrivers", "markets": [{"key": "team_totals", "outcomes": []}]}],
        },
    }
    mock_get.side_effect = [roster_resp, odds_resp]

    events = closing_odds_module._fetch_historical_event_snapshot(
        "basketball_wnba", "2026-07-09T23:58:00Z", "betrivers", "team_totals",
        "Los Angeles Sparks", "Minnesota Lynx",
        datetime(2026, 7, 10, 0, 0, tzinfo=timezone.utc),
    )
    assert len(events) == 1
    assert events[0]["_snapshot_at"] == "2026-07-09T23:56:12Z"
    roster_url = mock_get.call_args_list[0][0][0]
    odds_url = mock_get.call_args_list[1][0][0]
    assert roster_url.endswith("/historical/sports/basketball_wnba/events")
    assert odds_url.endswith("/historical/sports/basketball_wnba/events/wnba-evt-1/odds")
    _clear_historical_caches()


@patch("closing_odds.requests.get")
def test_event_snapshot_roster_failure_is_transient(mock_get):
    _clear_historical_caches()
    mock_get.return_value = Mock(status_code=429, headers={})
    events = closing_odds_module._fetch_historical_event_snapshot(
        "basketball_wnba", "2026-07-09T23:58:00Z", "betrivers", "team_totals",
        "Los Angeles Sparks", "Minnesota Lynx", None,
    )
    assert events is None  # transient — caller retries later
    _clear_historical_caches()


@patch("closing_odds.requests.get")
def test_event_snapshot_missing_game_is_permanent(mock_get):
    _clear_historical_caches()
    roster_resp = Mock(status_code=200, headers={})
    roster_resp.json.return_value = {"data": []}
    mock_get.return_value = roster_resp
    events = closing_odds_module._fetch_historical_event_snapshot(
        "basketball_wnba", "2026-07-09T23:58:00Z", "betrivers", "team_totals",
        "Los Angeles Sparks", "Minnesota Lynx", None,
    )
    assert events == []  # -> GAME NOT FOUND (permanent), not a retry loop
    _clear_historical_caches()
