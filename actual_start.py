"""Authoritative actual-start resolvers used by importer, recovery and audit."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache

import requests

from config import ESPN_BASE
from date_utils import parse_sheet_date
from name_match import normalize_name
from sources import espn_fights, mlb_statsapi


RESOLVER_VERSION = "actual-start-v1"


@dataclass(frozen=True)
class ActualStartResult:
    actual_start: datetime | None
    source: str = ""
    confidence: str = "UNRESOLVED"
    resolver_version: str = RESOLVER_VERSION
    event_id: str = ""
    error: str = ""

    @property
    def confident(self) -> bool:
        return self.actual_start is not None and self.confidence == "CONFIDENT"


ESPN_ROUTES = {
    "basketball_nba": ("basketball", "nba"),
    "basketball_wnba": ("basketball", "wnba"),
    "soccer_usa_mls": ("soccer", "usa.1"),
    "soccer_fifa_world_cup": ("soccer", "fifa.world"),
    "soccer_england_efl_cup": ("soccer", "eng.league_cup"),
}

TENNIS_ROUTES = {
    "tennis_atp_wimbledon": ("atp", "mens-singles"),
    "tennis_wta_wimbledon": ("wta", "womens-singles"),
    "manual_wta_libema": ("wta", "womens-singles"),
}

COMBAT_ROUTES = {
    # ESPN exposes a real per-bout play feed for UFC. Boxing deliberately is
    # not listed: its site scoreboard route is unavailable and a card/scheduled
    # timestamp is not an acceptable substitute for the opening bell.
    "mma_mixed_martial_arts": "mma/ufc",
}

# ESPN and The Odds API occasionally use different official country names or
# word order. Keep this list explicit rather than making every team match fuzzy.
_ESPN_TEAM_ALIASES = {
    "dr congo": "congo dr",
    "czech republic": "czechia",
    "caty mcnally": "catherine mcnally",
}


def _parse_utc(raw) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(raw or "").strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _team_matches(want: str, candidate: str) -> bool:
    a, b = normalize_name(want), normalize_name(candidate)
    a = _ESPN_TEAM_ALIASES.get(a, a)
    b = _ESPN_TEAM_ALIASES.get(b, b)
    return bool(a and b and (a == b or a in b or b in a))


def _espn_event_matches(event: dict, team1: str, team2: str) -> bool:
    competitors = (((event.get("competitions") or [{}])[0]).get("competitors") or [])
    names = []
    for competitor in competitors:
        team = competitor.get("team") or {}
        names.extend([team.get("displayName", ""), team.get("shortDisplayName", ""), team.get("name", "")])
    return any(_team_matches(team1, name) for name in names) and any(_team_matches(team2, name) for name in names)


def first_play_wallclock(summary: dict) -> datetime | None:
    """Earliest event wallclock in an ESPN summary — the true start.

    Basketball/football summaries expose per-play wallclocks under `plays`.
    Soccer summaries carry `plays: []` and put timing under `keyEvents` (the
    kickoff entry holds the real wallclock; `commentary` has none). Scanning
    both arrays and taking the earliest is one rule that covers every sport:
    the first play / kickoff is always the earliest timestamped event.
    """
    candidates = []
    for source in ("plays", "keyEvents"):
        for item in summary.get(source) or []:
            parsed = _parse_utc(item.get("wallclock"))
            if parsed is not None:
                candidates.append(parsed)
    return min(candidates, default=None)


@lru_cache(maxsize=64)
def _load_espn_tennis_events(league: str, date_key: str) -> tuple[list[dict], str]:
    try:
        response = requests.get(
            f"{ESPN_BASE}/tennis/{league}/scoreboard",
            params={"dates": date_key, "limit": 100}, timeout=15,
        )
        response.raise_for_status()
        return response.json().get("events") or [], ""
    except (requests.RequestException, ValueError) as exc:
        return [], f"ESPN tennis scoreboard failed: {exc}"


def _tennis_competition_matches(competition: dict, player1: str, player2: str) -> bool:
    names = []
    for competitor in competition.get("competitors") or []:
        athlete = competitor.get("athlete") or {}
        names.extend([
            athlete.get("displayName", ""), athlete.get("fullName", ""),
            athlete.get("shortName", ""), competitor.get("name", ""),
        ])
    return (
        any(_team_matches(player1, name) for name in names)
        and any(_team_matches(player2, name) for name in names)
    )


def resolve_espn_tennis_actual_start(bet: dict) -> ActualStartResult:
    """Resolve a completed tennis match's recorded competition start.

    ESPN tournament events contain match competitions nested under draw
    groupings. For completed matches, competition.date is the retained rolling
    match start (including court delays), while the event date is only the
    tournament window. We require timeValid + completed and never fall back to
    the tournament or sheet schedule.
    """
    route = TENNIS_ROUTES.get(str(bet.get("sport") or "").strip())
    game_date = parse_sheet_date(bet.get("game_date", ""))
    if route is None or game_date is None:
        return ActualStartResult(None, error="no ESPN tennis route or invalid date")
    league, grouping_slug = route
    events, error = _load_espn_tennis_events(league, game_date.strftime("%Y%m%d"))
    if error:
        return ActualStartResult(None, error=error)

    matches = []
    for event in events:
        for grouping in event.get("groupings") or []:
            slug = str((grouping.get("grouping") or {}).get("slug") or "")
            if slug != grouping_slug:
                continue
            for competition in grouping.get("competitions") or []:
                actual = _parse_utc(competition.get("date"))
                status = (competition.get("status") or {}).get("type") or {}
                if (
                    competition.get("timeValid") is True
                    and status.get("completed") is True
                    and actual is not None
                    and abs((actual.date() - game_date).days) <= 1
                    and _tennis_competition_matches(
                        competition, bet.get("team1", ""), bet.get("team2", ""),
                    )
                ):
                    matches.append((competition, actual))
    if len(matches) != 1:
        return ActualStartResult(None, error=f"ESPN tennis match count {len(matches)}")
    competition, actual = matches[0]
    return ActualStartResult(
        actual, "espn-tennis-competition-start", "CONFIDENT",
        event_id=str(competition.get("id") or ""),
    )


def resolve_espn_actual_start(bet: dict) -> ActualStartResult:
    route = ESPN_ROUTES.get(str(bet.get("sport") or "").strip())
    game_date = parse_sheet_date(bet.get("game_date", ""))
    if route is None or game_date is None:
        return ActualStartResult(None, error="no ESPN route or invalid date")
    sport, league = route
    try:
        scoreboard = requests.get(
            f"{ESPN_BASE}/{sport}/{league}/scoreboard",
            params={"dates": game_date.strftime("%Y%m%d"), "limit": 100}, timeout=15,
        )
        scoreboard.raise_for_status()
        events = scoreboard.json().get("events") or []
    except (requests.RequestException, ValueError) as exc:
        return ActualStartResult(None, error=f"ESPN scoreboard failed: {exc}")
    matches = [event for event in events if _espn_event_matches(
        event, bet.get("team1", ""), bet.get("team2", ""))]
    if len(matches) != 1:
        return ActualStartResult(None, error=f"ESPN event match count {len(matches)}")
    event_id = str(matches[0].get("id") or "")
    try:
        response = requests.get(
            f"{ESPN_BASE}/{sport}/{league}/summary", params={"event": event_id}, timeout=15,
        )
        response.raise_for_status()
        actual = first_play_wallclock(response.json())
    except (requests.RequestException, ValueError) as exc:
        return ActualStartResult(None, event_id=event_id, error=f"ESPN summary failed: {exc}")
    if actual is None:
        return ActualStartResult(None, event_id=event_id, error="ESPN first-play wallclock unavailable")
    return ActualStartResult(actual, "espn-first-play", "CONFIDENT", event_id=event_id)


def _is_first_round_start(play: dict) -> bool:
    play_type = play.get("type") or {}
    period = play.get("period") or {}
    type_text = str(play_type.get("text") or play_type.get("name") or "").strip().lower()
    try:
        period_number = int(period.get("number"))
    except (TypeError, ValueError):
        return False
    return type_text == "round start" and period_number == 1


def resolve_espn_combat_actual_start(bet: dict) -> ActualStartResult:
    """Resolve a UFC bout from ESPN's recorded round-one start wallclock.

    The scoreboard competition time is the card time, so this resolver only
    succeeds when exactly one strict two-fighter match has exactly one period-1
    ``Round Start`` play with a valid wallclock.
    """
    sport = str(bet.get("sport") or "").strip()
    league = COMBAT_ROUTES.get(sport)
    game_date = parse_sheet_date(bet.get("game_date", ""))
    if league is None or game_date is None:
        return ActualStartResult(None, error="no ESPN combat route or invalid date")
    bouts = espn_fights._fetch_scoreboard(league, espn_fights._dates_param(bet.get("game_date")))
    if bouts is None:
        return ActualStartResult(None, error="ESPN combat scoreboard unavailable")
    bout = espn_fights._find_bout(bouts, bet.get("team1", ""), bet.get("team2", ""))
    if bout is None:
        return ActualStartResult(None, error="ESPN combat bout missing or ambiguous")
    status = (bout.get("status") or {}).get("type") or {}
    if status.get("completed") is not True and status.get("name") != "STATUS_FINAL":
        return ActualStartResult(None, error="ESPN combat bout is not final")
    event_id = str(bout.get("_event_id") or "")
    competition_id = str(bout.get("id") or "")
    if not event_id or not competition_id:
        return ActualStartResult(None, event_id=competition_id, error="ESPN combat ids unavailable")
    plays = espn_fights._fetch_bout_plays(league, event_id, competition_id)
    if plays is None:
        return ActualStartResult(None, event_id=competition_id, error="ESPN combat plays unavailable")
    starts = [_parse_utc(play.get("wallclock")) for play in plays if _is_first_round_start(play)]
    starts = [start for start in starts if start is not None]
    if len(starts) != 1:
        return ActualStartResult(
            None, event_id=competition_id,
            error=f"ESPN combat round-one start count {len(starts)}",
        )
    actual = starts[0]
    if abs((actual.date() - game_date).days) > 1:
        return ActualStartResult(None, event_id=competition_id, error="ESPN combat start outside date window")
    return ActualStartResult(
        actual, "espn-ufc-round-one-start", "CONFIDENT", event_id=competition_id,
    )


def resolve_mlb_actual_start(bet: dict) -> ActualStartResult:
    games = mlb_statsapi.get_schedule_games(bet.get("game_date", ""))
    if games is None:
        return ActualStartResult(None, error="MLB schedule unavailable")
    game = mlb_statsapi.find_game(games, bet.get("team1", ""), bet.get("team2", ""))
    if game is None:
        return ActualStartResult(None, error="MLB game missing or ambiguous")
    actual = mlb_statsapi.game_first_pitch(game)
    game_id = str(mlb_statsapi.game_pk(game) or "")
    if actual is None:
        feed = mlb_statsapi.get_game_feed_live(game_id) if game_id else None
        actual = mlb_statsapi.first_pitch(feed or {})
    if actual is None:
        return ActualStartResult(None, event_id=game_id, error="MLB firstPitch unavailable")
    return ActualStartResult(actual, "mlb-statsapi-firstPitch", "CONFIDENT", event_id=game_id)


def resolve_actual_start(bet: dict) -> ActualStartResult:
    existing = _parse_utc(bet.get("actual_start"))
    if existing is not None:
        confidence = str(bet.get("actual_start_confidence") or "CONFIDENT").upper()
        return ActualStartResult(
            existing, str(bet.get("actual_start_source") or "sheet"), confidence,
            event_id=str(bet.get("event_id") or ""),
        )
    sport = str(bet.get("sport") or "").strip()
    if sport == "baseball_mlb":
        return resolve_mlb_actual_start(bet)
    if sport in TENNIS_ROUTES:
        return resolve_espn_tennis_actual_start(bet)
    if sport in COMBAT_ROUTES:
        return resolve_espn_combat_actual_start(bet)
    if sport in ESPN_ROUTES:
        return resolve_espn_actual_start(bet)
    return ActualStartResult(None, error=f"no actual-start resolver for {sport}")
