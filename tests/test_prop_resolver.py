"""MLB prop settlement from a REAL boxscore fixture (game 823368, 2026-06-14).

Verified stat lines in the fixture:
  Paul Skenes (P):     10 K, 18 outs, 4 hits allowed, 2 ER, 1 BB, 23 BF
  Heriberto Hernández: 1 H, 4 TB, 1 HR, 1 RBI, 1 R, 2 K, 4 PA
  Spencer Horwitz:     0 H, 1 BB, 5 PA (a genuine 0-for — real appearance)
  Esteury Ruiz:        1 H, 1 2B → 0 singles, 2 PA
  Endy Rodríguez:      accent name, 1 PA
"""

import json
import os

import pytest

from config import RESULT_WIN, RESULT_LOSS, RESULT_PUSH, RESULT_VOID
from prop_resolver import (
    parse_prop_selection, resolve_prop_leg, evaluate_leg, evaluate_legs,
    PropManualReview, RESULT_MANUAL, format_props_marker, parse_props_marker,
)

_FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "mlb_boxscore_823368.json")
with open(_FIXTURE, encoding="utf-8") as f:
    BOX = json.load(f)


def r(sel):
    return resolve_prop_leg(BOX, sel)


# ── parsing ──────────────────────────────────────────────────────────────────
def test_parse_prop_selection():
    assert parse_prop_selection("Paul Skenes Over 7.5 Strikeouts") == ("Paul Skenes", "over", 7.5, "strikeouts")
    assert parse_prop_selection("Ronald Acuna Jr Under 1.5 Total Bases") == ("Ronald Acuna Jr", "under", 1.5, "total bases")
    assert parse_prop_selection("just some text") is None
    assert parse_prop_selection("Team Over 8.5") is None   # no stat label


# ── batting stats ────────────────────────────────────────────────────────────
def test_hits_home_runs_total_bases():
    assert r("Heriberto Hernandez Over 0.5 Hits") == RESULT_WIN       # 1 hit
    assert r("Heriberto Hernandez Over 0.5 Home Runs") == RESULT_WIN  # 1 HR
    assert r("Heriberto Hernandez Over 3.5 Total Bases") == RESULT_WIN  # 4 TB
    assert r("Heriberto Hernandez Under 3.5 Total Bases") == RESULT_LOSS


def test_accent_insensitive_name_match():
    # Selection has NO accent; boxscore has "Hernández" — normalized exact match.
    assert r("Heriberto Hernandez Over 0.5 RBIs") == RESULT_WIN       # 1 RBI
    assert r("Endy Rodriguez Under 0.5 Hits") == RESULT_WIN           # 0 H, accent name


def test_h_r_rbi_combo():
    # Hernández: 1 H + 1 R + 1 RBI = 3
    assert r("Heriberto Hernandez Over 2.5 H+R+RBI") == RESULT_WIN
    assert r("Heriberto Hernandez Under 2.5 H+R+RBI") == RESULT_LOSS


def test_singles_derivation():
    # Esteury Ruiz: 1 H, 1 2B → singles = 1 − 1 − 0 − 0 = 0
    assert r("Esteury Ruiz Under 0.5 Singles") == RESULT_WIN
    assert r("Esteury Ruiz Over 0.5 Singles") == RESULT_LOSS


def test_genuine_zero_settles_not_manual():
    # Horwitz had 5 PA and 0 hits — a real appearance, a genuine 0 → settles.
    assert r("Spencer Horwitz Under 0.5 Hits") == RESULT_WIN
    assert r("Spencer Horwitz Over 0.5 Walks") == RESULT_WIN          # 1 BB


# ── pitching stats ───────────────────────────────────────────────────────────
def test_pitching_stats():
    assert r("Paul Skenes Over 7.5 Strikeouts") == RESULT_WIN         # 10 K
    assert r("Paul Skenes Under 7.5 Strikeouts") == RESULT_LOSS
    assert r("Paul Skenes Over 3.5 Hits Allowed") == RESULT_WIN       # 4 H
    assert r("Paul Skenes Over 2.5 Earned Runs") == RESULT_LOSS       # 2 ER
    assert r("Paul Skenes Under 20.5 Outs") == RESULT_WIN             # 18 outs


def test_integer_line_pushes_on_exact():
    assert r("Paul Skenes Over 10 Strikeouts") == RESULT_PUSH         # exactly 10


# ── DNP policy ───────────────────────────────────────────────────────────────
def test_absent_player_voids():
    assert r("Babe Ruth Over 0.5 Hits") == RESULT_VOID                # not in boxscore


def test_zero_qualifying_appearance_is_manual():
    # A player present with 0 PA → ambiguous → manual.
    box = {"teams": {"home": {"team": {"name": "X"}, "players": {
        "id1": {"person": {"fullName": "Ghost Runner"},
                "stats": {"batting": {"plateAppearances": 0, "hits": 0}}},
    }}, "away": {"team": {"name": "Y"}, "players": {}}}}
    with pytest.raises(PropManualReview, match="0 batting appearances"):
        resolve_prop_leg(box, "Ghost Runner Over 0.5 Hits")


def test_batter_on_a_pitching_prop_is_manual():
    # Horwitz is a batter — no pitching appearance → manual, never a wrong settle.
    with pytest.raises(PropManualReview, match="0 pitching appearances"):
        r("Spencer Horwitz Over 4.5 Strikeouts")


def test_unparseable_and_unknown_label_are_manual():
    with pytest.raises(PropManualReview):
        r("some random note")
    with pytest.raises(PropManualReview, match="unknown stat label"):
        r("Paul Skenes Over 1.5 Doubles")   # 'doubles' isn't an offered prop label


# ── evaluate wrappers + marker ──────────────────────────────────────────────
def test_evaluate_leg_never_raises():
    assert evaluate_leg(BOX, "Paul Skenes Over 7.5 Strikeouts") == RESULT_WIN
    assert evaluate_leg(BOX, "Paul Skenes Over 1.5 Doubles") == RESULT_MANUAL
    assert evaluate_leg(BOX, "Babe Ruth Over 0.5 Hits") == RESULT_VOID


def test_props_marker_round_trip():
    legs = {"A Over 1.5 Hits": "WIN", "B Under 5.5 Strikeouts": "LOSS"}
    iso = "2026-06-14T22:00:00Z"
    marker = format_props_marker(legs, iso)
    notes = f"some prior note\n{marker}\ntrailing"
    parsed = parse_props_marker(notes)
    assert parsed is not None
    results, parsed_iso = parsed
    assert results == legs
    assert parsed_iso == iso


def test_parse_marker_absent():
    assert parse_props_marker("no marker here") is None
    assert parse_props_marker("") is None


def test_evaluate_legs_maps_every_leg():
    out = evaluate_legs(BOX, ["Paul Skenes Over 7.5 Strikeouts", "Babe Ruth Over 0.5 Hits"])
    assert out == {"Paul Skenes Over 7.5 Strikeouts": RESULT_WIN, "Babe Ruth Over 0.5 Hits": RESULT_VOID}


# ── two-pass state machine ───────────────────────────────────────────────────
from datetime import datetime, timedelta, timezone
from prop_resolver import plan_settlement, combine_pickem, parse_pickem_marker


def _now(**kw):
    return datetime(2026, 6, 14, 22, 0, 0, tzinfo=timezone.utc) + timedelta(**kw)


def test_plan_settlement_first_pass_observes():
    assert plan_settlement(_now(), None, {"a": "WIN"}) == ("observe", None)


def test_plan_settlement_waits_within_window():
    marker = ({"a": "WIN"}, _now().isoformat())
    assert plan_settlement(_now(minutes=30), marker, {"a": "WIN"}) == ("wait", None)


def test_plan_settlement_settles_after_window_when_unchanged():
    marker = ({"a": "WIN"}, _now().isoformat())
    action, payload = plan_settlement(_now(minutes=61), marker, {"a": "WIN"})
    assert action == "settle"
    assert payload == {"a": "WIN"}


def test_plan_settlement_changed_routes_manual():
    marker = ({"a": "WIN"}, _now().isoformat())
    action, payload = plan_settlement(_now(minutes=61), marker, {"a": "LOSS"})
    assert action == "changed"
    assert payload == ({"a": "WIN"}, {"a": "LOSS"})


def test_plan_settlement_corrupt_marker_routes_manual():
    # A marker with an unparseable timestamp must NOT settle — hand to a human.
    assert plan_settlement(_now(minutes=61), ({"a": "WIN"}, "not-a-date"), {"a": "WIN"}) == ("manual", None)


def test_corrupt_json_marker_parses_to_none():
    # A present-but-broken props-observed line parses to None; the poller turns
    # that (with the prefix still in Notes) into manual, never a fresh settle.
    assert parse_props_marker("props-observed: {not valid json @ 2026-06-14T22:00:00Z") is None


# ── pick'em combination ──────────────────────────────────────────────────────
def test_parse_pickem_marker():
    assert parse_pickem_marker("blah\nPickem power 4-pick\nfoo") == ("power", 4)
    assert parse_pickem_marker("Pickem FLEX 6-pick") == ("flex", 6)
    assert parse_pickem_marker("no marker") is None


def test_pickem_power_all_win_settles():
    out = combine_pickem("power", {"l1": "WIN", "l2": "WIN", "l3": "WIN"})
    assert out["route"] == "settle_win"
    assert out["hits"] == 3


def test_pickem_power_loss_no_void_is_loss():
    out = combine_pickem("power", {"l1": "WIN", "l2": "LOSS", "l3": "WIN"})
    assert out["route"] == "settle_loss"


def test_pickem_void_or_push_routes_manual_payout():
    assert combine_pickem("power", {"l1": "WIN", "l2": "VOID", "l3": "WIN"})["route"] == "manual_payout"
    assert combine_pickem("power", {"l1": "WIN", "l2": "PUSH"})["route"] == "manual_payout"


def test_pickem_flex_always_manual_payout():
    out = combine_pickem("flex", {"l1": "WIN", "l2": "WIN", "l3": "WIN", "l4": "WIN"})
    assert out["route"] == "manual_payout"
    assert out["hits"] == 4


def test_pickem_manual_leg_routes_manual_review():
    assert combine_pickem("power", {"l1": "WIN", "l2": "MANUAL"})["route"] == "manual"


# ── entry orchestrator (injected providers) ─────────────────────────────────
from prop_resolver import resolve_prop_entry

_SCHED = [{
    "gamePk": 823368,
    "status": {"abstractGameState": "Final", "detailedState": "Final"},
    "teams": {"away": {"team": {"name": "Miami Marlins"}}, "home": {"team": {"name": "Pittsburgh Pirates"}}},
}]


def _providers(sched=_SCHED, box=BOX):
    return (lambda d: sched), (lambda pk: box)


def _leg(sel):
    return {"selection": sel, "team1": "Pittsburgh Pirates", "team2": "Miami Marlins", "game_date": "6/14/2026"}


def test_entry_first_pass_observes_then_settles():
    sp, bp = _providers()
    legs = [_leg("Paul Skenes Over 7.5 Strikeouts")]
    d1 = resolve_prop_entry(legs, _now(), None, sp, bp)
    assert d1["action"] == "observe"
    assert d1["current"] == {"Paul Skenes Over 7.5 Strikeouts": RESULT_WIN}
    marker = (d1["current"], _now().isoformat())
    d2 = resolve_prop_entry(legs, _now(minutes=61), marker, sp, bp)
    assert d2["action"] == "settle"


def test_entry_waits_when_game_not_final():
    live = [dict(_SCHED[0], status={"abstractGameState": "Live", "detailedState": "In Progress"})]
    sp, bp = _providers(sched=live)
    d = resolve_prop_entry([_leg("Paul Skenes Over 7.5 Strikeouts")], _now(), None, sp, bp)
    assert d["action"] == "wait_final"


def test_entry_manual_when_game_ambiguous():
    dh = _SCHED + [dict(_SCHED[0], gamePk=999)]   # doubleheader
    sp, bp = _providers(sched=dh)
    d = resolve_prop_entry([_leg("Paul Skenes Over 7.5 Strikeouts")], _now(), None, sp, bp)
    assert d["action"] == "manual"


def test_entry_retry_on_fetch_failure():
    d = resolve_prop_entry([_leg("Paul Skenes Over 7.5 Strikeouts")], _now(), None,
                           lambda d: None, lambda pk: BOX)
    assert d["action"] == "retry"
