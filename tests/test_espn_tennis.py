"""ESPN tennis Moneyline auto-resolution (offline -- scoreboard is stubbed)."""

import pytest

from sources import espn_tennis as et
from resolver import resolve


def _match(p1, p2, winner, status="STATUS_FINAL"):
    """Build a minimal ESPN competition dict for two players."""
    return {
        "status": {"type": {"name": status}},
        "competitors": [
            {"athlete": {"displayName": p1}, "winner": (winner == p1)},
            {"athlete": {"displayName": p2}, "winner": (winner == p2)},
        ],
    }


@pytest.fixture(autouse=True)
def _clear_cache():
    et._scoreboard_cache.clear()
    yield
    et._scoreboard_cache.clear()


def _stub(monkeypatch, tour, matches):
    monkeypatch.setattr(et, "_fetch_scoreboard", lambda t: matches if t == tour else None)


def test_tour_mapping():
    assert et.tour_for_sport("tennis_atp_wimbledon") == "atp"
    assert et.tour_for_sport("tennis_wta_us_open") == "wta"
    assert et.tour_for_sport("baseball_mlb") is None
    assert et.is_tennis("tennis_atp_wimbledon") is True
    assert et.is_tennis("basketball_nba") is False


def test_final_match_grades_winner(monkeypatch):
    _stub(monkeypatch, "wta", [_match("Marta Kostyuk", "Nadia Podoroska", winner="Marta Kostyuk")])
    g = et.get_match_result("tennis_wta_wimbledon", "Nadia Podoroska", "Marta Kostyuk")
    assert g == {"home_team": "Nadia Podoroska", "away_team": "Marta Kostyuk",
                 "home_score": 0, "away_score": 1}
    # Plugs straight into resolve()
    bet = {"bet_type": "Moneyline", "selection": "Marta Kostyuk",
           "team1": "Nadia Podoroska", "team2": "Marta Kostyuk", "sport": "tennis_wta_wimbledon"}
    assert resolve(bet, g) == "WIN"
    bet["selection"] = "Nadia Podoroska"
    assert resolve(bet, g) == "LOSS"


def test_retirement_is_not_graded(monkeypatch):
    _stub(monkeypatch, "atp", [_match("Alex Molcan", "Daniel Altmaier",
                                       winner="Alex Molcan", status="STATUS_RETIRED")])
    assert et.get_match_result("tennis_atp_wimbledon", "Alex Molcan", "Daniel Altmaier") is None


def test_scheduled_match_not_graded(monkeypatch):
    _stub(monkeypatch, "atp", [_match("A Player", "B Player", winner="", status="STATUS_SCHEDULED")])
    assert et.get_match_result("tennis_atp_wimbledon", "A Player", "B Player") is None


def test_match_not_found_returns_none(monkeypatch):
    _stub(monkeypatch, "atp", [_match("Someone Else", "Another Guy", winner="Someone Else")])
    assert et.get_match_result("tennis_atp_wimbledon", "Alex Molcan", "Daniel Altmaier") is None


def test_non_tennis_sport_returns_none():
    assert et.get_match_result("baseball_mlb", "Yankees", "Red Sox") is None
