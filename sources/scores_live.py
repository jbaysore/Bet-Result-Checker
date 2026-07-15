"""
Fresh, no-cache live-status client for The Odds API /scores — the true-start
signal the CLV accuracy work is built on (see CLV_ACCURACY_PLAN §2.1, Phase 0/1).

Deliberately SEPARATE from sources.odds_api._fetch_scores, which the settlement
poller relies on and which is unusable here: it has a process-lifetime cache
(so it never re-observes a pregame→live flip) and requests daysFrom=3 (so it
also returns days-old completed games we don't want in a start monitor). This
module never caches a scores payload and requests no daysFrom, so /scores
returns only live + upcoming + just-finished games — exactly the start window.

Nothing here writes to the sheet. Every fetch returns an explicit error state
(TRANSIENT vs coverage) so the caller can map failures to UNKNOWN rather than
silently treating a game as pregame.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

import requests

from config import ODDS_API_KEY, ODDS_API_BASE

# Per-event start-safety states derived from a *successful* /scores payload.
PREGAME = "PREGAME"      # completed False, no live signal (scores/last_update null)
LIVE = "LIVE"            # in-progress: last_update set, or scores present
COMPLETED = "COMPLETED"  # completed True

# Coverage classes for a sport key (CLV_ACCURACY_PLAN Phase 0 item 2). A static
# unsupported list goes stale, so this is recomputed each probe; the caller
# holds the daily TTL (see CoverageCache).
COVERAGE_SUPPORTED = "SUPPORTED"                # active + has games in the window
COVERAGE_OFF_SEASON = "OFF_SEASON"             # not in the /sports active list
COVERAGE_TEMPORARILY_EMPTY = "TEMPORARILY_EMPTY"  # active but no games right now
COVERAGE_TRANSIENT_ERROR = "TRANSIENT_ERROR"   # fetch failed → maps to UNKNOWN, re-probe
COVERAGE_UNSUPPORTED = "UNSUPPORTED"           # sport key unknown to the API (404)


@dataclass
class ScoresResult:
    """Structured outcome of one live /scores fetch — no exceptions escape."""
    sport_key: str
    status: str = "OK"                 # "OK" | "ERROR"
    games: list = field(default_factory=list)
    error: str | None = None
    http_status: int | None = None
    credits: dict = field(default_factory=dict)  # remaining / used / last (str or None)
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def ok(self) -> bool:
        return self.status == "OK"


def parse_credit_headers(headers) -> dict:
    """Pull the Odds API usage counters off a response for per-call accounting."""
    get = getattr(headers, "get", lambda *_: None)
    return {
        "remaining": get("x-requests-remaining"),
        "used": get("x-requests-used"),
        "last": get("x-requests-last"),
    }


def fetch_scores_live(sport_key: str, timeout: int = 10) -> ScoresResult:
    """
    One fresh /scores call for a sport. No cache, no daysFrom. Always returns a
    ScoresResult; network/HTTP/parse failures become status='ERROR' with a
    reason and (when available) the HTTP status the caller uses to tell an
    unknown sport (404 → UNSUPPORTED) from a transient failure.
    """
    url = f"{ODDS_API_BASE}/sports/{sport_key}/scores"
    params = {"apiKey": ODDS_API_KEY, "dateFormat": "iso"}
    try:
        resp = requests.get(url, params=params, timeout=timeout)
    except requests.RequestException as exc:
        return ScoresResult(sport_key, status="ERROR", error=f"request failed: {exc}")

    credits = parse_credit_headers(resp.headers)
    if resp.status_code != 200:
        detail = ""
        try:
            body = resp.json()
            detail = body.get("error_code") or body.get("message") or ""
        except ValueError:
            detail = (resp.text or "")[:200]
        return ScoresResult(
            sport_key, status="ERROR",
            error=f"HTTP {resp.status_code}: {detail}".strip(),
            http_status=resp.status_code, credits=credits,
        )

    try:
        games = resp.json()
    except ValueError as exc:
        return ScoresResult(sport_key, status="ERROR",
                            error=f"bad JSON: {exc}", http_status=200, credits=credits)
    if not isinstance(games, list):
        return ScoresResult(sport_key, status="ERROR",
                            error="unexpected /scores shape (not a list)",
                            http_status=200, credits=credits)

    return ScoresResult(sport_key, status="OK", games=games,
                        http_status=200, credits=credits)


def event_live_state(game: dict) -> str:
    """
    PREGAME / LIVE / COMPLETED for one /scores game entry. The Odds API leaves
    `scores` and `last_update` null until a game is in progress, so their
    presence is the live signal (confirmed in CLV_ACCURACY_PLAN §2). `completed`
    takes precedence — a finished game reports scores but is no longer live.
    """
    if game.get("completed") is True:
        return COMPLETED
    if game.get("last_update"):
        return LIVE
    scores = game.get("scores")
    if isinstance(scores, list) and len(scores) > 0:
        return LIVE
    return PREGAME


def classify_coverage(result: ScoresResult, active_keys: set | None) -> str:
    """
    Coverage class for the sport this result came from (Phase 0 item 2).

    active_keys is the in-season set from GET /sports (free); None when that
    lookup itself failed, in which case we can't distinguish off-season from
    temporarily-empty and fall back to the safe error class.
    """
    if not result.ok:
        # An unknown sport key is a durable 404; everything else (401/422/429/
        # network/parse) is transient and must NOT be cached as unsupported.
        if result.http_status == 404:
            return COVERAGE_UNSUPPORTED
        return COVERAGE_TRANSIENT_ERROR

    if active_keys is not None and result.sport_key not in active_keys:
        return COVERAGE_OFF_SEASON
    if not result.games:
        if active_keys is None:
            return COVERAGE_TRANSIENT_ERROR
        return COVERAGE_TEMPORARILY_EMPTY
    return COVERAGE_SUPPORTED


class CoverageCache:
    """
    Daily-TTL cache of coverage classifications so a static unsupported list
    can't go stale (Phase 0 item 2). Transient errors are never cached — they
    are re-probed on the next call — so a blip can't sideline a sport for a day.
    """

    def __init__(self, ttl_seconds: int = 24 * 60 * 60):
        self.ttl_seconds = ttl_seconds
        self._store: dict[str, tuple[float, str]] = {}

    def get(self, sport_key: str, now_monotonic: float) -> str | None:
        entry = self._store.get(sport_key)
        if entry is None:
            return None
        stamped_at, coverage = entry
        if now_monotonic - stamped_at >= self.ttl_seconds:
            return None
        return coverage

    def put(self, sport_key: str, coverage: str, now_monotonic: float) -> None:
        if coverage == COVERAGE_TRANSIENT_ERROR:
            return
        self._store[sport_key] = (now_monotonic, coverage)
