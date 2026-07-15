"""Pure schedule/status matching for mlb_statsapi (offline)."""

from datetime import datetime, timezone

from sources.mlb_statsapi import (
    find_game, is_clean_final, game_pk, first_pitch, scheduled_start,
)


def _game(away, home, pk=1, abstract="Final", detailed="Final"):
    return {
        "gamePk": pk,
        "status": {"abstractGameState": abstract, "detailedState": detailed},
        "teams": {"away": {"team": {"name": away}}, "home": {"team": {"name": home}}},
    }


def test_find_game_bijective_match_either_order():
    games = [_game("Miami Marlins", "Pittsburgh Pirates", pk=823368)]
    g = find_game(games, "Pittsburgh Pirates", "Miami Marlins")   # reversed order ok
    assert game_pk(g) == 823368
    # Short name tolerance (team match is looser than the exact player match).
    assert game_pk(find_game(games, "Marlins", "Pirates")) == 823368


def test_find_game_doubleheader_is_ambiguous_none():
    games = [
        _game("Miami Marlins", "Pittsburgh Pirates", pk=1),
        _game("Miami Marlins", "Pittsburgh Pirates", pk=2),
    ]
    assert find_game(games, "Miami Marlins", "Pittsburgh Pirates") is None


def test_find_game_no_match_none():
    games = [_game("Miami Marlins", "Pittsburgh Pirates")]
    assert find_game(games, "New York Yankees", "Boston Red Sox") is None


def test_is_clean_final_only_true_for_official_final():
    assert is_clean_final(_game("A", "B")) is True
    assert is_clean_final(_game("A", "B", abstract="Live", detailed="In Progress")) is False
    # Weather-shortened / suspended games are NOT a clean final → manual.
    assert is_clean_final(_game("A", "B", abstract="Final", detailed="Completed Early")) is False
    assert is_clean_final(_game("A", "B", abstract="Preview", detailed="Suspended")) is False


def _feed(first=None, scheduled=None):
    game_info = {}
    if first is not None:
        game_info["firstPitch"] = first
    return {"gameData": {"gameInfo": game_info, "datetime": {"dateTime": scheduled}}}


def test_first_pitch_parsed_to_utc():
    fp = first_pitch(_feed(first="2026-06-14T23:15:00Z"))
    assert fp == datetime(2026, 6, 14, 23, 15, tzinfo=timezone.utc)


def test_first_pitch_none_until_game_starts():
    # firstPitch is absent/blank pregame — the caller must not treat this as a start.
    assert first_pitch(_feed(first=None)) is None
    assert first_pitch(_feed(first="")) is None
    assert first_pitch({}) is None


def test_scheduled_start_parsed():
    assert scheduled_start(_feed(scheduled="2026-06-14T23:05:00Z")) == datetime(
        2026, 6, 14, 23, 5, tzinfo=timezone.utc)
    assert scheduled_start(_feed(scheduled=None)) is None
