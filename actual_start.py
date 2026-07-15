"""Authoritative actual-start resolvers used by importer, recovery and audit."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import requests

from config import ESPN_BASE
from date_utils import parse_sheet_date
from name_match import normalize_name
from sources import mlb_statsapi


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
    "basketball_wnba": ("basketball", "wnba"),
    "soccer_usa_mls": ("soccer", "usa.1"),
    "soccer_fifa_world_cup": ("soccer", "fifa.world"),
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
    return bool(a and b and (a == b or a in b or b in a))


def _espn_event_matches(event: dict, team1: str, team2: str) -> bool:
    competitors = (((event.get("competitions") or [{}])[0]).get("competitors") or [])
    names = []
    for competitor in competitors:
        team = competitor.get("team") or {}
        names.extend([team.get("displayName", ""), team.get("shortDisplayName", ""), team.get("name", "")])
    return any(_team_matches(team1, name) for name in names) and any(_team_matches(team2, name) for name in names)


def first_play_wallclock(summary: dict) -> datetime | None:
    plays = summary.get("plays") or []
    parsed = [_parse_utc(play.get("wallclock")) for play in plays if play.get("wallclock")]
    parsed = [value for value in parsed if value is not None]
    return min(parsed, default=None)


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
    if sport in ESPN_ROUTES:
        return resolve_espn_actual_start(bet)
    return ActualStartResult(None, error=f"no actual-start resolver for {sport}")
