"""
Settlement-grade MLB player-stat source: the official MLB Stats API
(statsapi.mlb.com — free, no key). Schedule-by-date → gamePk + official status;
boxscore → per-player batting/pitching. Do NOT use TOA or ESPN for MLB stats
(this is the book-grade record).

Split into thin fetch wrappers (return None on transient error → retry next run)
and pure parse helpers (unit-tested against a real boxscore fixture). Field names
were VERIFIED against a real payload (game 823368, 2026-06-14): batting.{hits,
totalBases,homeRuns,rbi,runs,doubles,triples,baseOnBalls,stolenBases,strikeOuts,
plateAppearances}; pitching.{strikeOuts,outs,hits,earnedRuns,baseOnBalls,
battersFaced}. Date lookups go through date_utils.parse_sheet_date (M/D/YYYY —
same trap as F1).
"""

import requests

from date_utils import parse_sheet_date
from name_match import normalize_name

BASE = "https://statsapi.mlb.com/api/v1"


# ── Fetch (thin, None on transient error) ───────────────────────────────────
def get_schedule_games(game_date: str) -> list | None:
    """All MLB games on the bet's date. None on error; [] when no games."""
    d = parse_sheet_date(game_date)
    if d is None:
        return None
    try:
        resp = requests.get(
            f"{BASE}/schedule",
            params={"sportId": 1, "date": d.strftime("%Y-%m-%d")},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        print(f"[mlb_statsapi] schedule fetch failed ({game_date}): {e}")
        return None
    games = []
    for day in data.get("dates", []):
        games += day.get("games", [])
    return games


def get_boxscore(game_pk) -> dict | None:
    try:
        resp = requests.get(f"{BASE}/game/{game_pk}/boxscore", timeout=15)
        resp.raise_for_status()
        return resp.json()
    except (requests.RequestException, ValueError) as e:
        print(f"[mlb_statsapi] boxscore fetch failed (gamePk {game_pk}): {e}")
        return None


# ── Pure parse ──────────────────────────────────────────────────────────────
def _team_match(a: str, b: str) -> bool:
    """Team names match if normalized-equal or one normalized name contains the
    other ('Cardinals' vs 'St Louis Cardinals'). Team matching is looser than the
    EXACT player match — a mismatched team just fails to find the game → manual."""
    na, nb = normalize_name(a), normalize_name(b)
    if not na or not nb:
        return False
    return na == nb or na in nb or nb in na


def find_game(games: list, team1: str, team2: str) -> dict | None:
    """
    The single game whose two teams map bijectively to the bet's {team1, team2}.
    Returns None when no game matches OR more than one does (a doubleheader is a
    genuine ambiguity — route to manual, never guess which game).
    """
    matches = []
    for g in games or []:
        try:
            away = g["teams"]["away"]["team"]["name"]
            home = g["teams"]["home"]["team"]["name"]
        except (KeyError, TypeError):
            continue
        pair_ok = (
            (_team_match(team1, away) and _team_match(team2, home))
            or (_team_match(team1, home) and _team_match(team2, away))
        )
        if pair_ok:
            matches.append(g)
    return matches[0] if len(matches) == 1 else None


def game_pk(game: dict) -> int | None:
    return (game or {}).get("gamePk")


def is_clean_final(game: dict) -> bool:
    """
    Officially, unambiguously Final — the ONLY status props settle on. Suspended,
    postponed, or weather-called ("Completed Early") games are NOT a clean final
    → the caller routes them to manual.
    """
    status = (game or {}).get("status", {})
    return (status.get("abstractGameState") == "Final"
            and status.get("detailedState") == "Final")


def find_player(boxscore: dict, player_name: str) -> dict | None:
    """
    The player entry (with .stats.batting / .stats.pitching) whose fullName EXACTLY
    normalized-matches player_name, across BOTH teams. None when the player is
    entirely absent from the boxscore (a clean DNP → the caller VOIDs the leg).
    """
    want = normalize_name(player_name)
    if not want:
        return None
    for side in ("away", "home"):
        players = (((boxscore or {}).get("teams", {}).get(side, {})).get("players", {}) or {})
        for entry in players.values():
            full = (entry.get("person", {}) or {}).get("fullName", "")
            if normalize_name(full) == want:
                return entry
    return None
