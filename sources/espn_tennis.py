"""
Auto-resolves tennis Moneyline (match-winner) bets from ESPN's free
scoreboard API, which -- unlike The Odds API /scores feed -- actually covers
tennis.

The Odds API sport key encodes the tour (tennis_atp_* / tennis_wta_*), so the
ESPN path is a deterministic prefix map, NOT the fuzzy league guess that made
ESPN impractical for team sports in poller.py. One scoreboard fetch per tour
per process is cached; ESPN returns the whole current tournament's matches in
a single response (the ?dates= param does not filter), so a date isn't needed.

Scope (by design, 2026-06-30): Moneyline only. Set/game spreads & totals are
derivable from each competitor's linescores but are deferred. Only
STATUS_FINAL matches with a clearly flagged winner are graded; STATUS_RETIRED,
walkovers, and in-progress/scheduled matches return None, so the bet falls
through to NEEDS_REVIEW for manual settlement (books grade retirements
inconsistently).
"""

import requests

ESPN_TENNIS_BASE = "https://site.api.espn.com/apis/site/v2/sports/tennis"

# One scoreboard payload (list of match competitions) per tour per process.
_scoreboard_cache: dict[str, list | None] = {}


def tour_for_sport(sport_key: str) -> str | None:
    """tennis_atp_wimbledon -> 'atp', tennis_wta_* -> 'wta', else None."""
    k = (sport_key or "").strip().lower()
    if k.startswith("tennis_atp"):
        return "atp"
    if k.startswith("tennis_wta"):
        return "wta"
    return None


def is_tennis(sport_key: str) -> bool:
    """True for any tennis sport key this module can route to ESPN."""
    return tour_for_sport(sport_key) is not None


def _fetch_scoreboard(tour: str) -> list | None:
    """
    Fetches and caches the flattened list of match competitions for a tour's
    current tournament. Returns None (not cached) on a transient API error so
    a later run can retry.
    """
    if tour in _scoreboard_cache:
        return _scoreboard_cache[tour]

    url = f"{ESPN_TENNIS_BASE}/{tour}/scoreboard"
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        print(f"[espn_tennis] {tour} scoreboard fetch failed: {e}")
        return None

    matches = []
    for event in data.get("events", []):
        for grouping in event.get("groupings", []):
            matches += grouping.get("competitions", [])
    _scoreboard_cache[tour] = matches
    return matches


def _competitor_names(competition: dict) -> list[tuple[str, dict]]:
    """[(displayName, competitor_dict), ...] for a match's two players."""
    out = []
    for c in competition.get("competitors", []):
        ath = c.get("athlete") or {}
        out.append((ath.get("displayName", ""), c))
    return out


def _find_match(matches: list, p1: str, p2: str) -> dict | None:
    """
    Finds the competition whose two players substring-match p1 and p2 (in
    either order), the same case-insensitive matching used for team names
    elsewhere. Requires the two bet players to map to DISTINCT competitors so
    a single name appearing in both isn't a false positive.
    """
    a, b = p1.lower().strip(), p2.lower().strip()
    for comp in matches:
        names = _competitor_names(comp)
        if len(names) != 2:
            continue
        n0, n1 = names[0][0].lower(), names[1][0].lower()
        if (a in n0 and b in n1) or (a in n1 and b in n0):
            return comp
    return None


def get_match_result(sport_key: str, player1: str, player2: str,
                     game_date: str = None) -> dict | None:
    """
    Returns a game dict compatible with resolver.resolve() for a tennis
    Moneyline bet, or None if the match can't be cleanly graded (sport isn't
    tennis, scoreboard unavailable, match not found, not a clean final,
    retired/walkover, or no winner flagged).

    The dict uses the BET's own player strings as home/away with a 1/0 score
    for the winner, so resolver._resolve_moneyline grades it with no tennis-
    specific logic and no dependency on ESPN's exact name spelling beyond the
    initial substring match.

    game_date is accepted for signature symmetry with odds_api.get_game_result
    but unused -- ESPN returns the whole current tournament in one response.

    TODO (RESOLVER_EXPANSION_PLAN F1): unlike espn_fights, tennis intentionally
    gets NO date window because its scoreboard `?dates=` param does not filter
    (whole tournament comes back regardless). If ESPN ever changes that, route
    game_date through date_utils.parse_sheet_date here the way _dates_param does.
    """
    tour = tour_for_sport(sport_key)
    if tour is None:
        return None

    matches = _fetch_scoreboard(tour)
    if not matches:
        return None

    comp = _find_match(matches, player1, player2)
    if comp is None:
        print(f"[espn_tennis] match not found on {tour} board: {player1} vs {player2}")
        return None

    status = comp.get("status", {}).get("type", {}).get("name", "")
    if status != "STATUS_FINAL":
        # STATUS_RETIRED, STATUS_SCHEDULED, in-progress, etc. -> don't grade;
        # the caller leaves the bet pending, and it routes to NEEDS_REVIEW
        # past the give-up threshold.
        print(f"[espn_tennis] {player1} vs {player2}: status "
              f"'{status or 'unknown'}' -- not a clean final, leaving for review.")
        return None

    winner_name = next((n for n, c in _competitor_names(comp) if c.get("winner") is True), None)
    if winner_name is None:
        print(f"[espn_tennis] {player1} vs {player2}: final but no winner flagged.")
        return None

    # Map ESPN's winner back to the bet's player1/player2 by substring, then
    # express it as a 1/0 score on the bet's own names.
    w = winner_name.lower()
    if player1.lower().strip() in w:
        home_score, away_score = 1, 0
    elif player2.lower().strip() in w:
        home_score, away_score = 0, 1
    else:
        print(f"[espn_tennis] winner '{winner_name}' didn't map to either "
              f"bet player ({player1} / {player2}).")
        return None

    return {
        "home_team": player1,
        "away_team": player2,
        "home_score": home_score,
        "away_score": away_score,
    }
