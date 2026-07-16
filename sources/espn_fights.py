"""
Auto-resolves MMA / boxing Moneyline (bout-winner) bets from ESPN's free
scoreboard API. Same rationale as sources/espn_tennis.py: The Odds API /scores
feed doesn't reliably cover combat sports, and the ESPN league here is a
deterministic map from the Odds API sport key (mma_mixed_martial_arts → mma/ufc,
boxing_boxing → boxing), NOT the fuzzy team-sport league guess that made ESPN
impractical elsewhere.

Scope (by design): Moneyline only — the bout winner. A Spread/Total/prop on a
fight returns None and routes to manual (the poller gates on bet_type).

Ruling principle (Josh): accuracy over everything — every ambiguity fails toward
manual, never a guess:
  - clean STATUS_FINAL with exactly one flagged winner → graded WIN/LOSS;
  - Draw (final, no single winner, ESPN labels it a draw) → equal scores, which
    resolver._resolve_moneyline settles as PUSH for a 2-way fighter bet (money
    back); a real "Draw" *selection* still wins via _resolve_draw;
  - No Contest / cancelled bout → the GAME_STATUS_CANCELLED short-circuit → VOID;
  - anything else (bout not found, ambiguous rematch/name-collision, final with
    no winner and no clear draw/NC label, not yet final) → None → manual.

VERIFY (PROPOSED): the exact ESPN field wording for a DRAW vs a NO CONTEST could
not be observed on a live board when this shipped (no such bout was scheduled).
The detection scans the status text defensively and, crucially, FAILS TO MANUAL
when the signal isn't explicit — so an unrecognised label never mis-settles, it
just routes to review. Confirm the wording against one real draw/NC payload and
tighten `_classify_no_winner` if needed.
"""

from datetime import timedelta

import requests

from config import GAME_STATUS_CANCELLED
from date_utils import parse_sheet_date
from name_match import normalize_name as _normalize_name

ESPN_FIGHT_BASE = "https://site.api.espn.com/apis/site/v2/sports"
ESPN_CORE_BASE = "https://sports.core.api.espn.com/v2/sports"

# Odds API sport key → ESPN scoreboard league path.
_LEAGUE_BY_SPORT = {
    "mma_mixed_martial_arts": "mma/ufc",
    "boxing_boxing": "boxing",
}

# One scoreboard payload per (league, dates_key) per process.
_scoreboard_cache: dict[tuple[str, str], list | None] = {}
_plays_cache: dict[tuple[str, str, str], list | None] = {}

def league_for_sport(sport_key: str) -> str | None:
    return _LEAGUE_BY_SPORT.get((sport_key or "").strip().lower())


def is_fight(sport_key: str) -> bool:
    """True for any sport key this module can route to ESPN (MMA/boxing)."""
    return league_for_sport(sport_key) is not None


# Explicit provider-name equivalences. Keep these narrow: exact normalized
# matching remains the rule, with only individually verified aliases admitted.
_FIGHTER_ALIASES = {
    "zachary reese": "zach reese",
}


def normalize_fighter_name(value: str) -> str:
    normalized = _normalize_name(value)
    return _FIGHTER_ALIASES.get(normalized, normalized)


def _dates_param(game_date: str | None) -> str:
    """
    Sheet date ("M/D/YYYY" OR "YYYY-MM-DD" — via the shared parser, F1) → an ESPN
    `dates=YYYYMMDD-YYYYMMDD` ±1-day window (cards straddle midnight UTC, so a
    single day can miss the bout). '' when no/garbage date → fetch the default
    (current) board.
    """
    d = parse_sheet_date(game_date)
    if d is None:
        return ""
    lo = (d - timedelta(days=1)).strftime("%Y%m%d")
    hi = (d + timedelta(days=1)).strftime("%Y%m%d")
    return f"{lo}-{hi}"


def _fetch_scoreboard(league: str, dates_key: str) -> list | None:
    """Flattened list of bout competitions for a league (+ optional date
    window). None on transient error so a later run retries."""
    cache_key = (league, dates_key)
    if cache_key in _scoreboard_cache:
        return _scoreboard_cache[cache_key]

    url = f"{ESPN_FIGHT_BASE}/{league}/scoreboard"
    params = {"dates": dates_key} if dates_key else None
    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        print(f"[espn_fights] {league} scoreboard fetch failed: {e}")
        return None

    bouts = _flatten_bouts(data)
    _scoreboard_cache[cache_key] = bouts
    return bouts


def _flatten_bouts(data: dict) -> list:
    """Flatten bouts while retaining the parent card's event id.

    ESPN's per-bout play feed requires both the card event id and competition
    id. The competition date is only the card start and must never be treated
    as the opening bell for an individual bout.
    """
    bouts = []
    for event in (data or {}).get("events", []):
        event_id = str(event.get("id") or "")
        for competition in event.get("competitions", []):
            bout = dict(competition)
            bout["_event_id"] = event_id
            bouts.append(bout)
    return bouts


def _fetch_bout_plays(league: str, event_id: str, competition_id: str) -> list | None:
    """Return ESPN's per-bout play records, or None on a transient failure."""
    cache_key = (league, str(event_id), str(competition_id))
    if cache_key in _plays_cache:
        return _plays_cache[cache_key]
    try:
        sport, league_name = league.split("/", 1)
    except ValueError:
        return None
    url = (
        f"{ESPN_CORE_BASE}/{sport}/leagues/{league_name}/events/{event_id}"
        f"/competitions/{competition_id}/plays"
    )
    try:
        response = requests.get(url, params={"limit": 1000}, timeout=15)
        response.raise_for_status()
        plays = response.json().get("items") or []
    except (requests.RequestException, ValueError) as exc:
        print(f"[espn_fights] bout plays fetch failed: {exc}")
        return None
    _plays_cache[cache_key] = plays
    return plays


def _competitors(competition: dict) -> list[tuple[str, bool]]:
    """[(displayName, winner_bool), ...] for a bout's two fighters."""
    out = []
    for c in competition.get("competitors", []):
        ath = c.get("athlete") or {}
        out.append((ath.get("displayName", ""), c.get("winner") is True))
    return out


def _find_bout(bouts: list, f1: str, f2: str) -> dict | None:
    """
    The bout whose two fighters EXACTLY normalized-match {f1, f2}. Matching by
    both fighters (not date) is the disambiguator; if MORE than one bout matches
    (a rematch on the same board, or a name collision), return None → manual,
    never guess which card.
    """
    want = {normalize_fighter_name(f1), normalize_fighter_name(f2)}
    if "" in want or len(want) != 2:
        return None
    matches = []
    for comp in bouts:
        names = _competitors(comp)
        if len(names) != 2:
            continue
        have = {normalize_fighter_name(names[0][0]), normalize_fighter_name(names[1][0])}
        if have == want:
            matches.append(comp)
    return matches[0] if len(matches) == 1 else None


def _classify_no_winner(competition: dict) -> str | None:
    """
    A final bout with no single winner is a DRAW or a NO CONTEST — settled
    differently (PUSH vs VOID). Scan the status text; return 'draw', 'nc', or
    None (ambiguous → manual). Deliberately conservative: only an explicit label
    auto-settles.
    """
    st = competition.get("status", {}).get("type", {})
    blob = " ".join(
        str(st.get(k, "")) for k in ("name", "description", "detail", "shortDetail")
    ).lower()
    if "no contest" in blob or "no decision" in blob:
        return "nc"
    if "draw" in blob:
        return "draw"
    return None


def get_fight_result(sport_key: str, fighter1: str, fighter2: str,
                     game_date: str = None) -> dict | None:
    """
    Returns a game dict compatible with resolver.resolve() for an MMA/boxing
    Moneyline bet, or None when it can't be cleanly graded (route to manual).

    The dict uses the BET's own fighter strings as home/away with a 1/0 score
    for the winner, so resolver._resolve_moneyline grades it with no fight-
    specific logic. A draw is expressed as an equal (0-0) score → PUSH; a No
    Contest / cancelled bout is expressed as the cancelled status → VOID.
    """
    league = league_for_sport(sport_key)
    if league is None:
        return None

    bouts = _fetch_scoreboard(league, _dates_param(game_date))
    if not bouts:
        return None

    comp = _find_bout(bouts, fighter1, fighter2)
    if comp is None:
        print(f"[espn_fights] bout not found / ambiguous on {league}: "
              f"{fighter1} vs {fighter2}")
        return None

    status_name = comp.get("status", {}).get("type", {}).get("name", "")
    if status_name != "STATUS_FINAL":
        print(f"[espn_fights] {fighter1} vs {fighter2}: status "
              f"'{status_name or 'unknown'}' -- not final, leaving for review.")
        return None

    winners = [nm for nm, is_w in _competitors(comp) if is_w]
    if len(winners) == 1:
        w = normalize_fighter_name(winners[0])
        if w == normalize_fighter_name(fighter1):
            return {"home_team": fighter1, "away_team": fighter2,
                    "home_score": 1, "away_score": 0}
        if w == normalize_fighter_name(fighter2):
            return {"home_team": fighter1, "away_team": fighter2,
                    "home_score": 0, "away_score": 1}
        print(f"[espn_fights] winner '{winners[0]}' didn't map to either bettor "
              f"fighter ({fighter1} / {fighter2}).")
        return None

    # No single winner → draw or no contest.
    kind = _classify_no_winner(comp)
    if kind == "nc":
        return {"home_team": fighter1, "away_team": fighter2,
                "home_score": 0, "away_score": 0,
                "status": GAME_STATUS_CANCELLED}   # → VOID
    if kind == "draw":
        return {"home_team": fighter1, "away_team": fighter2,
                "home_score": 0, "away_score": 0}  # equal → PUSH (2-way refund)

    print(f"[espn_fights] {fighter1} vs {fighter2}: final with no clear winner "
          f"and no draw/NC label -- routing to manual.")
    return None
