"""ESPN MMA/boxing Moneyline auto-resolution (offline -- scoreboard stubbed).

Fixture bouts mirror the real ESPN shape observed live 2026-07-10:
events[].competitions[] with competitors[].athlete.displayName + .winner and
status.type.name == 'STATUS_FINAL' (e.g. UFC Freedom 250: Diego Lopes def.
Steve Garcia). Draw/NC label wording is the PROPOSED assumption (see the module
docstring) — the tests pin both the auto-settle path and the fail-to-manual path.
"""

import pytest

from sources import espn_fights as ef
from resolver import resolve
from config import (
    BET_TYPE_MONEYLINE, RESULT_WIN, RESULT_LOSS, RESULT_PUSH, RESULT_VOID,
)


def _bout(f1, f2, winner=None, status="STATUS_FINAL", description="Final"):
    """Minimal ESPN competition dict for a two-fighter bout."""
    return {
        "status": {"type": {"name": status, "description": description}},
        "competitors": [
            {"athlete": {"displayName": f1}, "winner": (winner == f1)},
            {"athlete": {"displayName": f2}, "winner": (winner == f2)},
        ],
    }


@pytest.fixture(autouse=True)
def _clear_cache():
    ef._scoreboard_cache.clear()
    yield
    ef._scoreboard_cache.clear()


def _stub(monkeypatch, league, bouts):
    monkeypatch.setattr(ef, "_fetch_scoreboard",
                        lambda lg, dk: bouts if lg == league else None)


def _bet(selection, f1, f2, sport="mma_mixed_martial_arts"):
    return {
        "bet_type": BET_TYPE_MONEYLINE, "selection": selection,
        "team1": f1, "team2": f2, "sport": sport,
    }


def _grade(sport, f1, f2, bouts, monkeypatch, selection=None):
    league = ef.league_for_sport(sport)
    _stub(monkeypatch, league, bouts)
    game = ef.get_fight_result(sport, f1, f2)
    if game is None:
        return None
    return resolve(_bet(selection or f1, f1, f2, sport), game)


# ── routing + normalization ─────────────────────────────────────────────────

def test_sport_routing():
    assert ef.league_for_sport("mma_mixed_martial_arts") == "mma/ufc"
    assert ef.league_for_sport("boxing_boxing") == "boxing"
    assert ef.league_for_sport("baseball_mlb") is None
    assert ef.is_fight("mma_mixed_martial_arts") is True
    assert ef.is_fight("basketball_nba") is False


def test_name_normalization_accents_and_suffixes():
    assert ef.normalize_fighter_name("José Aldo Jr.") == "jose aldo"
    assert ef.normalize_fighter_name("Israel  Adesanya") == "israel adesanya"
    assert ef.normalize_fighter_name("Khabib Nurmagomedov") == "khabib nurmagomedov"
    # Different accents/suffixes on the same name normalize equal.
    assert ef.normalize_fighter_name("Acuña") == ef.normalize_fighter_name("Acuna")
    assert ef.normalize_fighter_name("Zachary Reese") == ef.normalize_fighter_name("Zach Reese")


# ── grading ──────────────────────────────────────────────────────────────────

def test_final_bout_grades_winner_and_loser(monkeypatch):
    bouts = [_bout("Steve Garcia", "Diego Lopes", winner="Diego Lopes")]
    # Bet on the winner → WIN.
    assert _grade("mma_mixed_martial_arts", "Diego Lopes", "Steve Garcia", bouts, monkeypatch) == RESULT_WIN
    # Bet on the loser → LOSS.
    assert _grade("mma_mixed_martial_arts", "Steve Garcia", "Diego Lopes", bouts, monkeypatch) == RESULT_LOSS


def test_accented_name_still_matches(monkeypatch):
    bouts = [_bout("Jose Aldo", "Cory Sandhagen", winner="Jose Aldo")]
    # Bet stored with the accent → normalized exact match still grades.
    assert _grade("mma_mixed_martial_arts", "José Aldo", "Cory Sandhagen", bouts, monkeypatch) == RESULT_WIN


def test_draw_is_push_for_fighter_bet(monkeypatch):
    bouts = [_bout("Fighter A", "Fighter B", winner=None, description="Draw")]
    assert _grade("mma_mixed_martial_arts", "Fighter A", "Fighter B", bouts, monkeypatch) == RESULT_PUSH


def test_no_contest_is_void(monkeypatch):
    bouts = [_bout("Fighter A", "Fighter B", winner=None, description="No Contest")]
    assert _grade("mma_mixed_martial_arts", "Fighter A", "Fighter B", bouts, monkeypatch) == RESULT_VOID


def test_final_no_winner_no_label_routes_to_manual(monkeypatch):
    # Final, nobody flagged, no draw/NC wording → do NOT guess → None (manual).
    bouts = [_bout("Fighter A", "Fighter B", winner=None, description="Final")]
    _stub(monkeypatch, "mma/ufc", bouts)
    assert ef.get_fight_result("mma_mixed_martial_arts", "Fighter A", "Fighter B") is None


def test_scheduled_bout_not_graded(monkeypatch):
    bouts = [_bout("Fighter A", "Fighter B", winner=None, status="STATUS_SCHEDULED")]
    _stub(monkeypatch, "mma/ufc", bouts)
    assert ef.get_fight_result("mma_mixed_martial_arts", "Fighter A", "Fighter B") is None


def test_bout_not_found_routes_to_manual(monkeypatch):
    bouts = [_bout("Someone Else", "Another Guy", winner="Someone Else")]
    _stub(monkeypatch, "mma/ufc", bouts)
    assert ef.get_fight_result("mma_mixed_martial_arts", "Fighter A", "Fighter B") is None


def test_ambiguous_rematch_same_board_routes_to_manual(monkeypatch):
    # Two bouts with the same fighter pair (rematch/collision) → cannot pick → manual.
    bouts = [
        _bout("Fighter A", "Fighter B", winner="Fighter A"),
        _bout("Fighter A", "Fighter B", winner="Fighter B"),
    ]
    _stub(monkeypatch, "mma/ufc", bouts)
    assert ef.get_fight_result("mma_mixed_martial_arts", "Fighter A", "Fighter B") is None


def test_boxing_routes_to_boxing_league(monkeypatch):
    bouts = [_bout("Canelo Alvarez", "Jermell Charlo", winner="Canelo Alvarez")]
    assert _grade("boxing_boxing", "Canelo Alvarez", "Jermell Charlo", bouts, monkeypatch) == RESULT_WIN


def test_winner_unmapped_to_bettor_routes_to_manual(monkeypatch):
    # A third name flagged winner (data glitch) → don't map → manual.
    bout = _bout("Fighter A", "Fighter B", winner=None)
    bout["competitors"][0]["winner"] = False
    bout["competitors"][1]["winner"] = False
    bout["competitors"].append({"athlete": {"displayName": "Fighter C"}, "winner": True})
    _stub(monkeypatch, "mma/ufc", [bout])
    # 3 competitors → not a clean 2-fighter bout → not found → manual.
    assert ef.get_fight_result("mma_mixed_martial_arts", "Fighter A", "Fighter B") is None


def test_dates_param_builds_window():
    # F1: BOTH sheet date formats yield the SAME window (M/D/YYYY was the bug —
    # it used to fall through to '' and miss the card a day later).
    assert ef._dates_param("2026-06-14") == "20260613-20260615"
    assert ef._dates_param("6/14/2026") == "20260613-20260615"
    assert ef._dates_param("7/10/2026") == ef._dates_param("2026-07-10")
    assert ef._dates_param("") == ""
    assert ef._dates_param(None) == ""
    assert ef._dates_param("garbage") == ""


def test_flatten_bouts_preserves_parent_event_id():
    bout = _bout("Fighter A", "Fighter B", winner="Fighter A")
    bout["id"] = "competition-1"
    flattened = ef._flatten_bouts({"events": [{"id": "event-1", "competitions": [bout]}]})
    assert flattened[0]["id"] == "competition-1"
    assert flattened[0]["_event_id"] == "event-1"
    assert "_event_id" not in bout
