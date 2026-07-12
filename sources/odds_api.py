import requests
from datetime import datetime, timezone
from config import ODDS_API_KEY, ODDS_API_BASE

# One /scores response per sport per process — poll_bet() calls get_game_result()
# once per pending bet; on a busy NFL Sunday that's many identical API calls
# without this cache.
_scores_cache: dict[str, list] = {}
_active_sports_cache: set[str] | None = None
_no_scores_feed: set[str] = set()
GAME_TIME_MATCH_TOLERANCE_SECONDS = 12 * 60 * 60


def _parse_commence_time(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def _select_matching_game(games: list, team1: str, team2: str,
                          expected_start: datetime | None = None) -> dict | None:
    t1 = team1.lower().strip()
    t2 = team2.lower().strip()
    candidates = []
    for game in games:
        home_lower = game.get("home_team", "").lower()
        away_lower = game.get("away_team", "").lower()
        home_match = _matches_any(t1, home_lower) or _matches_any(t2, home_lower)
        away_match = _matches_any(t1, away_lower) or _matches_any(t2, away_lower)
        if home_match and away_match:
            candidates.append(game)

    if not candidates:
        return None

    if expected_start is not None:
        expected_utc = expected_start.astimezone(timezone.utc)
        timed = []
        for game in candidates:
            commence = _parse_commence_time(game.get("commence_time"))
            if commence is None:
                continue
            delta = abs((commence - expected_utc).total_seconds())
            if delta <= GAME_TIME_MATCH_TOLERANCE_SECONDS:
                timed.append((delta, game))
        timed.sort(key=lambda item: item[0])
        return timed[0][1] if timed else None

    # A repeated matchup without a scheduled start is ambiguous. Returning
    # None safely defers settlement instead of using another game in a series.
    return candidates[0] if len(candidates) == 1 else None


def get_game_result(sport_key: str, team1: str, team2: str,
                    expected_start: datetime | None = None) -> dict | None:
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

    game = _select_matching_game(games, team1, team2, expected_start)
    if game is not None:
        completed = game.get("completed", False)
        home_team = game.get("home_team", "")
        away_team = game.get("away_team", "")
        home_lower = home_team.lower()
        away_lower = away_team.lower()

        if not completed:
            print(f"[odds_api] Game found ({home_team} vs {away_team}) but not final yet.")
            return None

        scores = game.get("scores") or []
        score_map = {s.get("name", "").lower(): _safe_int(s.get("score", 0)) for s in scores}

        return {
            "final":              True,
            "home_team":          home_team,
            "away_team":          away_team,
            "home_score":         score_map.get(home_lower, 0),
            "away_score":         score_map.get(away_lower, 0),
            "status_description": "Final",
        }

    print(f"[odds_api] No matching game found for '{team1}' vs '{team2}' in {sport_key}.")
    return None


def fetch_active_sport_keys() -> set[str] | None:
    """
    Returns the set of in-season sport keys from GET /sports (free — no quota).
    Returns None if the lookup fails so callers can fall back to a direct request.
    """
    global _active_sports_cache
    if _active_sports_cache is not None:
        return _active_sports_cache

    url = f"{ODDS_API_BASE}/sports"
    try:
        response = requests.get(url, params={"apiKey": ODDS_API_KEY}, timeout=10)
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError) as e:
        print(f"[odds_api] Could not fetch active sports list: {e}")
        return None

    if not isinstance(data, list):
        print("[odds_api] Unexpected /sports response format.")
        return None

    _active_sports_cache = {
        s["key"] for s in data
        if isinstance(s, dict) and s.get("key") and s.get("active")
    }
    return _active_sports_cache


def sport_has_odds_feed(sport_key: str) -> bool:
    """True when sport_key is currently in-season on The Odds API."""
    active = fetch_active_sport_keys()
    if active is None:
        return True  # unknown — let the paid endpoint decide
    return sport_key in active


def _fetch_scores(sport_key: str) -> list | None:
    """Fetches and caches the full /scores payload for a sport (one credit per sport per run)."""
    if sport_key in _scores_cache:
        return _scores_cache[sport_key]
    if sport_key in _no_scores_feed:
        return None

    if not sport_has_odds_feed(sport_key):
        print(f"[odds_api] Sport '{sport_key}' is not currently active on The Odds API "
              f"(no /scores feed). Use the notification bell to check ESPN manually.")
        _no_scores_feed.add(sport_key)
        return None

    url = f"{ODDS_API_BASE}/sports/{sport_key}/scores"

    # The Odds API only accepts daysFrom in 1..3 for /scores. Using a larger
    # value returns HTTP 422 INVALID_SCORES_DAYS_FROM — previously mislabeled
    # as "sport key not recognised", which made valid keys like baseball_mlb
    # look broken.
    params = {
        "apiKey":        ODDS_API_KEY,
        "daysFrom":      3,
        "dateFormat":    "iso",
    }

    try:
        response = requests.get(url, params=params, timeout=10)

        # Surface credit/auth errors clearly — don't cache so a later bet can retry
        if response.status_code == 401:
            print("[odds_api] Invalid API key. Check ODDS_API_KEY in your .env.")
            return None
        if response.status_code == 404:
            print(f"[odds_api] Sport key '{sport_key}' unknown to Odds API (HTTP 404).")
            return None
        if response.status_code == 422:
            detail = ""
            try:
                err = response.json()
                detail = f" — {err.get('error_code') or ''}: {err.get('message') or response.text[:200]}"
            except ValueError:
                detail = f" — {response.text[:200]}"
            print(f"[odds_api] Scores request rejected for '{sport_key}' (HTTP 422){detail}")
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
