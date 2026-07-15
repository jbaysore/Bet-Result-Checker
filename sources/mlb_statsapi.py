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

from datetime import datetime, timezone

import requests

from date_utils import parse_sheet_date
from name_match import normalize_name

BASE = "https://statsapi.mlb.com/api/v1"
# feed/live lives on the v1.1 API (not v1). It carries the authoritative
# gameData.gameInfo.firstPitch wallclock — the true-start signal the CLV
# accuracy work resolves actual starts from (see CLV_ACCURACY_PLAN Phase 0/2).
FEED_LIVE_BASE = "https://statsapi.mlb.com/api/v1.1"


# ── Fetch (thin, None on transient error) ───────────────────────────────────
def get_schedule_games(game_date: str) -> list | None:
    """All MLB games on the bet's date. None on error; [] when no games."""
    d = parse_sheet_date(game_date)
    if d is None:
        return None
    try:
        resp = requests.get(
            f"{BASE}/schedule",
            params={"sportId": 1, "date": d.strftime("%Y-%m-%d"), "hydrate": "gameInfo"},
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


def get_game_feed_live(game_pk) -> dict | None:
    """
    The full live feed for a game (v1.1). Carries gameData.gameInfo.firstPitch —
    the authoritative first-pitch wallclock — plus gameData.datetime.dateTime
    (the scheduled start) and gameData.status. None on transient error.
    """
    try:
        resp = requests.get(f"{FEED_LIVE_BASE}/game/{game_pk}/feed/live", timeout=15)
        resp.raise_for_status()
        return resp.json()
    except (requests.RequestException, ValueError) as e:
        print(f"[mlb_statsapi] feed/live fetch failed (gamePk {game_pk}): {e}")
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


def game_first_pitch(game: dict) -> datetime | None:
    """Actual firstPitch hydrated directly on a schedule game."""
    return _parse_iso_utc(((game or {}).get("gameInfo") or {}).get("firstPitch"))


def _parse_iso_utc(raw) -> datetime | None:
    """Parse an ISO8601 timestamp (statsapi returns e.g. '2026-06-14T23:15:00Z')
    to an aware UTC datetime. None for blank/unparseable/placeholder values."""
    if not raw or not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.strip().replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def first_pitch(feed: dict) -> datetime | None:
    """
    Actual first-pitch wallclock from feed/live → gameData.gameInfo.firstPitch,
    as an aware UTC datetime. None until the game has started (the field is
    absent/blank pregame) or when the feed is missing it — the caller then has
    no confident actual-start and must fall back per the plan (SAFE_BUT_EARLY).
    """
    game_info = (((feed or {}).get("gameData") or {}).get("gameInfo") or {})
    return _parse_iso_utc(game_info.get("firstPitch"))


def scheduled_start(feed: dict) -> datetime | None:
    """
    Scheduled start from feed/live → gameData.datetime.dateTime, as aware UTC.
    Lets the shadow monitor measure scheduled-vs-actual drift per game. None when
    absent or a placeholder (statsapi uses a blank/epoch for TBD starts)."""
    dt = (((feed or {}).get("gameData") or {}).get("datetime") or {})
    return _parse_iso_utc(dt.get("dateTime"))


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
