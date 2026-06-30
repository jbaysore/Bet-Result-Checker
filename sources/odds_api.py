import requests
from config import ODDS_API_KEY, ODDS_API_BASE

# One /scores response per sport per process — poll_bet() calls get_game_result()
# once per pending bet; on a busy NFL Sunday that's many identical API calls
# without this cache.
_scores_cache: dict[str, list] = {}


def get_game_result(sport_key: str, team1: str, team2: str) -> dict | None:
    """
    Fetches the final score for a game from The Odds API.
    Only called when ESPN returns no result — conserves credits.

    Args:
        sport_key:  Odds API sport key, e.g. "americanfootball_nfl"
        team1:      First team name as stored in your sheet
        team2:      Second team name as stored in your sheet

    Returns a dict if the game is found and final:
        {
            "final":              True,
            "home_team":          "Kansas City Chiefs",
            "away_team":          "Las Vegas Raiders",
            "home_score":         27,
            "away_score":         14,
            "status_description": "Final",
        }

    Returns None if:
        - Game not found
        - Game is not yet final
        - API returns an error

    NOTE on cancelled/postponed games: The Odds API's /scores endpoint only
    exposes a boolean "completed" field — it has no separate status for
    cancellation, so a cancelled game and a not-yet-played game are
    indistinguishable here. This function cannot detect cancellations.
    Cancellation detection is handled by espn.py (which has real status
    granularity via ESPN's status.type.name) before this fallback is ever
    called. If ESPN fails to find a cancelled game for some other reason,
    this fallback will incorrectly report it as "not final yet" rather
    than cancelled — a known limitation, not a bug to fix here.
    """
    games = _fetch_scores(sport_key)
    if games is None:
        return None

    t1 = team1.lower().strip()
    t2 = team2.lower().strip()

    for game in games:
        completed = game.get("completed", False)
        home_team = game.get("home_team", "")
        away_team = game.get("away_team", "")

        home_lower = home_team.lower()
        away_lower = away_team.lower()

        # Match both teams against sheet values
        home_match = _matches_any(t1, home_lower) or _matches_any(t2, home_lower)
        away_match = _matches_any(t1, away_lower) or _matches_any(t2, away_lower)

        if not (home_match and away_match):
            continue  # not our game

        if not completed:
            print(f"[odds_api] Game found ({home_team} vs {away_team}) but not final yet.")
            return None

        # Extract scores
        scores = game.get("scores") or []
        score_map = {s.get("name", "").lower(): _safe_int(s.get("score", 0)) for s in scores}

        home_score = score_map.get(home_lower, 0)
        away_score = score_map.get(away_lower, 0)

        return {
            "final":              True,
            "home_team":          home_team,
            "away_team":          away_team,
            "home_score":         home_score,
            "away_score":         away_score,
            "status_description": "Final",
        }

    print(f"[odds_api] No matching game found for '{team1}' vs '{team2}' in {sport_key}.")
    return None


def _fetch_scores(sport_key: str) -> list | None:
    """Fetches and caches the full /scores payload for a sport (one credit per sport per run)."""
    if sport_key in _scores_cache:
        return _scores_cache[sport_key]

    url = f"{ODDS_API_BASE}/sports/{sport_key}/scores"

    params = {
        "apiKey":        ODDS_API_KEY,
        "daysFrom":      7,   # look back up to 7 days for completed games
        "dateFormat":    "iso",
    }

    try:
        response = requests.get(url, params=params, timeout=10)

        # Surface credit/auth errors clearly — don't cache so a later bet can retry
        if response.status_code == 401:
            print("[odds_api] Invalid API key. Check ODDS_API_KEY in your .env.")
            return None
        if response.status_code == 422:
            print(f"[odds_api] Sport key '{sport_key}' not recognised by Odds API.")
            return None
        if response.status_code == 429:
            print("[odds_api] Odds API quota exceeded.")
            return None

        response.raise_for_status()

    except requests.RequestException as e:
        print(f"[odds_api] Request failed for {sport_key}: {e}")
        return None

    try:
        games = response.json()
    except ValueError as e:
        print(f"[odds_api] Failed to parse JSON for {sport_key}: {e}")
        return None

    if not isinstance(games, list):
        print(f"[odds_api] Unexpected response format for {sport_key}.")
        return None

    # Log remaining credits after every call so you can monitor usage
    remaining = response.headers.get("x-requests-remaining", "unknown")
    used = response.headers.get("x-requests-used", "unknown")
    print(f"[odds_api] Credits used: {used} | Remaining: {remaining}")

    _scores_cache[sport_key] = games
    return games


def _matches_any(sheet_name: str, api_name: str) -> bool:
    """
    Returns True if the sheet team name matches the API team name.
    Checks exact match first, then substring in either direction.
    """
    if not api_name:
        return False
    if sheet_name == api_name:
        return True
    if sheet_name in api_name or api_name in sheet_name:
        return True
    return False


def _safe_int(value) -> int:
    try:
        return int(float(str(value)))
    except (ValueError, TypeError):
        return 0