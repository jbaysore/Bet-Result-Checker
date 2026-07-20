"""
Phase 0 shadow start-monitor for the CLV accuracy work (CLV_ACCURACY_PLAN.md).

A persistent, NO-WRITE observer. It polls The Odds API /scores on a cadence,
tracks each upcoming event's pregame→live transition, and logs everything the
plan's Open Items need to be filled in with real numbers:

  1. Scores flip semantics + per-sport detection margin (vs MLB firstPitch).
  2. Coverage classification per sport key (daily TTL, transient ≠ unsupported).
  3. Event-id stability across /scores and /events (opt-in probe).
  4. MLB actual-start (firstPitch) detection lag.
  5. Historical snapshot granularity (opt-in probe).
  6. Pinnacle region + per-market coverage (opt-in probe).
  7. Bookmaker last_update freshness in the final window (opt-in probe).

It writes NOTHING to the Google Sheet. Observations go to a JSONL file (durable,
analyzable) and concise stdout. "Would-have-finalized" decisions are simulated
from poll timing so we can size the sample-slot count and safety margin before
Phase 1 hardcodes anything.

Run:  python shadow_start_monitor.py
The paid probes (3/5/6/7) default OFF; the baseline loop is scores-only (one
credit per sport per poll). Enable probes via the SHADOW_* env flags below.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import requests

from config import ODDS_API_KEY, ODDS_API_BASE
from redact import redact_request_error
from sources import mlb_statsapi, odds_api
from sources.odds_api import fetch_active_sport_keys
from sources.scores_live import (
    COMPLETED,
    LIVE,
    PREGAME,
    CoverageCache,
    classify_coverage,
    event_live_state,
    fetch_scores_live,
    parse_credit_headers,
)

# ── Config ───────────────────────────────────────────────────────────────────
POLL_SECONDS = max(15, int(os.getenv("SHADOW_POLL_SECONDS", "60")))
LOG_PATH = os.getenv("SHADOW_LOG_PATH", "shadow_logs/start_monitor.jsonl")
# Only track an event once it is within PRE minutes of its scheduled start, and
# keep tracking up to POST minutes after (the "cap") if it never goes live.
PRE_MINUTES = int(os.getenv("SHADOW_TRACK_PRE_MINUTES", "30"))
POST_MINUTES = int(os.getenv("SHADOW_TRACK_POST_MINUTES", "45"))
# Candidate safety margin + max-age used to SIMULATE finalize quality (§2.2).
MARGIN_SECONDS = int(os.getenv("SHADOW_MARGIN_SECONDS", "75"))
MAX_AGE_SECONDS = int(os.getenv("SHADOW_MAX_AGE_SECONDS", "300"))
# Optional sport allow-list (comma-separated Odds API keys). Empty → the API's
# in-season active set.
SPORTS_OVERRIDE = [s.strip() for s in os.getenv("SHADOW_SPORTS", "").split(",") if s.strip()]

# Opt-in paid probes.
BOOK_FRESHNESS_ON = os.getenv("SHADOW_BOOK_FRESHNESS", "0") == "1"
EVENTS_STABILITY_ON = os.getenv("SHADOW_EVENTS_STABILITY", "0") == "1"
HISTORICAL_PROBE_ON = os.getenv("SHADOW_HISTORICAL_PROBE", "0") == "1"
PINNACLE_PROBE_ON = os.getenv("SHADOW_PINNACLE_PROBE", "0") == "1"
FRESHNESS_BOOK = os.getenv("SHADOW_FRESHNESS_BOOK", "pinnacle").lower()
FRESHNESS_REGION = os.getenv("SHADOW_FRESHNESS_REGION", "us")
FRESHNESS_WINDOW_MINUTES = int(os.getenv("SHADOW_FRESHNESS_WINDOW_MINUTES", "20"))
DAILY_PROBE_SECONDS = 24 * 60 * 60

# Would-have-finalized quality classes (mirror CLV_ACCURACY_PLAN §2.2).
WOULD_VERIFIED_CLOSE = "VERIFIED_CLOSE"  # safe pregame sample inside margin AND fresh
WOULD_STALE = "STALE"                    # safe pregame sample exists but too old
WOULD_MISSED = "MISSED"                  # no pregame sample older than the margin
WOULD_SAFE_BUT_EARLY = "SAFE_BUT_EARLY"  # cap reached still pregame (never went live)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_commence(raw) -> datetime | None:
    if not raw or not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


# ── Pure helpers (unit-tested) ────────────────────────────────────────────────
def within_tracking_window(now: datetime, commence: datetime,
                           pre_minutes: int = PRE_MINUTES,
                           post_minutes: int = POST_MINUTES) -> bool:
    """True while now is inside [commence − pre, commence + post]."""
    return (commence - timedelta(minutes=pre_minutes)
            <= now <= commence + timedelta(minutes=post_minutes))


def classify_transition(prev: str | None, new: str) -> str | None:
    """
    The start-relevant transition, if any. We care about the first departure
    from PREGAME. PREGAME→COMPLETED means the whole live window fell between two
    polls (a detection miss worth logging distinctly).
    """
    if prev == PREGAME and new == LIVE:
        return "PREGAME_TO_LIVE"
    if prev == PREGAME and new == COMPLETED:
        return "PREGAME_TO_COMPLETED"
    if prev is None and new in (LIVE, COMPLETED):
        return "FIRST_SEEN_NOT_PREGAME"  # started before we ever polled it pregame
    return None


def detection_lag_seconds(detected_at: datetime, first_pitch: datetime | None) -> float | None:
    """Seconds between the true first pitch and when we first saw the game live.
    Positive = we detected after first pitch (the normal case)."""
    if first_pitch is None:
        return None
    return (detected_at - first_pitch).total_seconds()


def simulate_finalize(pregame_poll_times: list[datetime], detected_at: datetime,
                      margin_seconds: int = MARGIN_SECONDS,
                      max_age_seconds: int = MAX_AGE_SECONDS) -> dict:
    """
    What quality Phase 1's finalizer would have stamped, from poll timing alone.

    Chosen sample = the latest pregame poll at or before detected_at − margin.
      • none that old            → MISSED  (detection beat the margin; cadence or
                                            margin needs the samples-tab escape hatch)
      • chosen within max-age    → VERIFIED_CLOSE
      • chosen older than max-age → STALE
    """
    cutoff = detected_at - timedelta(seconds=margin_seconds)
    safe = [t for t in pregame_poll_times if t <= cutoff]
    if not safe:
        newest = max(pregame_poll_times) if pregame_poll_times else None
        gap = (detected_at - newest).total_seconds() if newest else None
        return {"quality": WOULD_MISSED, "chosen_at": None,
                "sample_age_s": None, "newest_pregame_gap_s": gap}
    chosen = max(safe)
    age = (detected_at - chosen).total_seconds()
    quality = WOULD_VERIFIED_CLOSE if age <= max_age_seconds else WOULD_STALE
    return {"quality": quality, "chosen_at": iso(chosen),
            "sample_age_s": age, "newest_pregame_gap_s": age}


def mlb_candidate_dates(commence: datetime) -> list[str]:
    """
    M/D/YYYY date strings to try for MLB schedule lookup. A game's official date
    (US/Eastern) equals either the UTC calendar date or the day before it for
    late starts, so trying both — with a bijective team match per date — resolves
    the gamePk without a timezone dependency.
    """
    day = commence.date()
    prev = (commence - timedelta(days=1)).date()
    return [f"{d.month}/{d.day}/{d.year}" for d in (day, prev)]


# ── Per-event tracking state ──────────────────────────────────────────────────
@dataclass
class EventTracker:
    sport: str
    event_id: str
    home: str
    away: str
    commence: datetime
    state: str | None = None
    pregame_poll_times: list[datetime] = field(default_factory=list)
    detected_at: datetime | None = None
    transition_logged: bool = False
    mlb_resolved: bool = False
    freshness_logged: bool = False
    stability_logged: bool = False
    capped: bool = False

    @property
    def matchup(self) -> str:
        return f"{self.away} @ {self.home}"


# ── JSONL logging (durable, no sheet writes) ──────────────────────────────────
class ShadowLog:
    def __init__(self, path: str):
        self.path = path
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)

    def emit(self, event_type: str, payload: dict) -> None:
        record = {"ts": iso(utc_now()), "type": event_type, **payload}
        line = json.dumps(record, default=str)
        try:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError as exc:
            print(f"[shadow] log write failed: {exc}")
        print(f"[shadow] {event_type}: {line}")


# ── Optional paid probes ──────────────────────────────────────────────────────
def _log_credits(log: ShadowLog, context: str, headers) -> None:
    credits = parse_credit_headers(headers)
    log.emit("credits", {"context": context, **credits})


def probe_book_freshness(log: ShadowLog, tracker: EventTracker) -> None:
    """Item 7: how fresh is the book's own quote (bookmakers[].last_update) vs
    when we fetched, for an event still pregame inside the final window."""
    url = f"{ODDS_API_BASE}/sports/{tracker.sport}/events/{tracker.event_id}/odds"
    params = {"apiKey": ODDS_API_KEY, "regions": FRESHNESS_REGION, "markets": "h2h",
              "oddsFormat": "american", "dateFormat": "iso", "bookmakers": FRESHNESS_BOOK}
    fetched_at = utc_now()
    try:
        resp = requests.get(url, params=params, timeout=15)
        _log_credits(log, "book_freshness", resp.headers)
        resp.raise_for_status()
        payload = resp.json()
    except (requests.RequestException, ValueError) as exc:
        log.emit("book_freshness_error", {"event_id": tracker.event_id, "error": redact_request_error(exc)})
        return
    books = (payload or {}).get("bookmakers", []) if isinstance(payload, dict) else []
    if not books:
        log.emit("book_freshness", {"event_id": tracker.event_id, "matchup": tracker.matchup,
                                    "book": FRESHNESS_BOOK, "present": False})
        tracker.freshness_logged = True
        return
    book = books[0]
    book_last = parse_commence(book.get("last_update"))
    market_last = None
    markets = book.get("markets") or []
    if markets:
        market_last = parse_commence(markets[0].get("last_update"))
    staleness = (fetched_at - book_last).total_seconds() if book_last else None
    log.emit("book_freshness", {
        "event_id": tracker.event_id, "matchup": tracker.matchup, "book": FRESHNESS_BOOK,
        "present": True, "fetched_at": iso(fetched_at),
        "book_last_update": iso(book_last) if book_last else None,
        "market_last_update": iso(market_last) if market_last else None,
        "book_staleness_s": staleness,
        "minutes_to_commence": (tracker.commence - fetched_at).total_seconds() / 60.0,
    })
    tracker.freshness_logged = True


def probe_event_stability(log: ShadowLog, sport: str, trackers: dict[str, EventTracker]) -> None:
    """Item 3: does the /events id match the /scores id for the same matchup?"""
    url = f"{ODDS_API_BASE}/sports/{sport}/events"
    try:
        resp = requests.get(url, params={"apiKey": ODDS_API_KEY, "dateFormat": "iso"}, timeout=15)
        _log_credits(log, "events_stability", resp.headers)
        resp.raise_for_status()
        events = resp.json()
    except (requests.RequestException, ValueError) as exc:
        log.emit("events_stability_error", {"sport": sport, "error": redact_request_error(exc)})
        return
    by_pair = {}
    for ev in events if isinstance(events, list) else []:
        key = (str(ev.get("home_team", "")).lower(), str(ev.get("away_team", "")).lower())
        by_pair[key] = ev.get("id")
    for tr in trackers.values():
        if tr.sport != sport or tr.stability_logged:
            continue
        events_id = by_pair.get((tr.home.lower(), tr.away.lower()))
        log.emit("event_id_stability", {
            "sport": sport, "matchup": tr.matchup, "scores_id": tr.event_id,
            "events_id": events_id, "match": events_id == tr.event_id,
        })
        tr.stability_logged = True


def probe_pinnacle_coverage(log: ShadowLog, sport: str) -> None:
    """Item 6: which featured markets does Pinnacle price for this sport, and in
    which region does it appear (batch /odds, one call per sport)."""
    for region in ("us", "eu", "uk", "au"):
        url = f"{ODDS_API_BASE}/sports/{sport}/odds"
        params = {"apiKey": ODDS_API_KEY, "regions": region,
                  "markets": "h2h,spreads,totals", "oddsFormat": "american",
                  "dateFormat": "iso", "bookmakers": "pinnacle"}
        try:
            resp = requests.get(url, params=params, timeout=15)
            _log_credits(log, f"pinnacle_coverage:{region}", resp.headers)
            resp.raise_for_status()
            events = resp.json()
        except (requests.RequestException, ValueError) as exc:
            log.emit("pinnacle_coverage_error", {"sport": sport, "region": region, "error": redact_request_error(exc)})
            continue
        events = events if isinstance(events, list) else []
        markets_seen: set[str] = set()
        n_with_pinnacle = 0
        for ev in events:
            for bk in ev.get("bookmakers", []):
                if bk.get("key") == "pinnacle":
                    n_with_pinnacle += 1
                    markets_seen.update(m.get("key") for m in bk.get("markets", []))
        log.emit("pinnacle_coverage", {
            "sport": sport, "region": region, "events": len(events),
            "events_with_pinnacle": n_with_pinnacle,
            "markets": sorted(m for m in markets_seen if m),
        })
        if n_with_pinnacle:
            return  # found the region that carries Pinnacle; no need to probe the rest


def probe_historical_granularity(log: ShadowLog, sport: str) -> None:
    """Item 5: how far apart are consecutive historical snapshots on our plan
    (5 vs 10 min) — probe two nearby historical timestamps and log the served
    snapshot timestamps + spacing."""
    base_ts = utc_now() - timedelta(days=1)
    served = []
    for offset in (0, 5):
        stamp = (base_ts + timedelta(minutes=offset)).isoformat().replace("+00:00", "Z")
        url = f"{ODDS_API_BASE}/historical/sports/{sport}/odds"
        params = {"apiKey": ODDS_API_KEY, "regions": "us", "markets": "h2h",
                  "oddsFormat": "american", "dateFormat": "iso", "date": stamp}
        try:
            resp = requests.get(url, params=params, timeout=20)
            _log_credits(log, f"historical_granularity:{offset}", resp.headers)
            resp.raise_for_status()
            payload = resp.json()
        except (requests.RequestException, ValueError) as exc:
            log.emit("historical_granularity_error", {"sport": sport, "requested": stamp, "error": redact_request_error(exc)})
            continue
        served.append({"requested": stamp,
                       "served_timestamp": payload.get("timestamp") if isinstance(payload, dict) else None,
                       "previous_timestamp": payload.get("previous_timestamp") if isinstance(payload, dict) else None,
                       "next_timestamp": payload.get("next_timestamp") if isinstance(payload, dict) else None})
    log.emit("historical_granularity", {"sport": sport, "snapshots": served})


# ── MLB actual-start resolution (read-only) ───────────────────────────────────
def resolve_mlb_first_pitch(tracker: EventTracker) -> dict | None:
    """Best-effort firstPitch for a tracked MLB game. Read-only; None on any miss."""
    for date_str in mlb_candidate_dates(tracker.commence):
        games = mlb_statsapi.get_schedule_games(date_str)
        if not games:
            continue
        game = mlb_statsapi.find_game(games, tracker.home, tracker.away)
        pk = mlb_statsapi.game_pk(game) if game else None
        if not pk:
            continue
        feed = mlb_statsapi.get_game_feed_live(pk)
        if feed is None:
            return None
        return {
            "game_pk": pk,
            "first_pitch": mlb_statsapi.first_pitch(feed),
            "scheduled": mlb_statsapi.scheduled_start(feed),
        }
    return None


def maybe_log_mlb_first_pitch(log: ShadowLog, tracker: EventTracker) -> None:
    if tracker.mlb_resolved or not tracker.sport.startswith("baseball_mlb"):
        return
    resolved = resolve_mlb_first_pitch(tracker)
    if resolved is None or resolved["first_pitch"] is None:
        return  # not started per statsapi yet, or unresolved — retry next poll
    fp = resolved["first_pitch"]
    log.emit("mlb_first_pitch", {
        "matchup": tracker.matchup, "event_id": tracker.event_id,
        "game_pk": resolved["game_pk"],
        "scheduled_commence": iso(tracker.commence),
        "statsapi_scheduled": iso(resolved["scheduled"]) if resolved["scheduled"] else None,
        "first_pitch": iso(fp),
        "detected_live_at": iso(tracker.detected_at) if tracker.detected_at else None,
        "detection_lag_s": detection_lag_seconds(tracker.detected_at, fp) if tracker.detected_at else None,
        "scheduled_vs_firstpitch_s": (fp - tracker.commence).total_seconds(),
    })
    tracker.mlb_resolved = True


# ── Main loop ─────────────────────────────────────────────────────────────────
def sports_to_watch() -> list[str]:
    if SPORTS_OVERRIDE:
        return SPORTS_OVERRIDE
    active = fetch_active_sport_keys()
    return sorted(active) if active else []


def poll_once(log: ShadowLog, trackers: dict[str, EventTracker],
              coverage: CoverageCache, active_keys: set | None) -> None:
    now = utc_now()
    for sport in sports_to_watch():
        result = fetch_scores_live(sport)
        cover = classify_coverage(result, active_keys)
        coverage.put(sport, cover, time.monotonic())
        log.emit("scores_poll", {
            "sport": sport, "coverage": cover, "ok": result.ok,
            "games": len(result.games), "error": result.error, **result.credits,
        })
        if not result.ok:
            continue
        for game in result.games:
            _process_game(log, trackers, sport, game, now)

    if BOOK_FRESHNESS_ON:
        for tr in list(trackers.values()):
            if (tr.state == PREGAME and not tr.freshness_logged
                    and now >= tr.commence - timedelta(minutes=FRESHNESS_WINDOW_MINUTES)):
                probe_book_freshness(log, tr)

    if EVENTS_STABILITY_ON:
        for sport in {tr.sport for tr in trackers.values() if not tr.stability_logged}:
            probe_event_stability(log, sport, trackers)

    _prune(log, trackers, now)


def _process_game(log: ShadowLog, trackers: dict[str, EventTracker],
                  sport: str, game: dict, now: datetime) -> None:
    event_id = str(game.get("id") or "")
    commence = parse_commence(game.get("commence_time"))
    if not event_id or commence is None:
        return
    if not within_tracking_window(now, commence):
        return

    state = event_live_state(game)
    tr = trackers.get(event_id)
    if tr is None:
        tr = EventTracker(sport=sport, event_id=event_id,
                          home=game.get("home_team", ""), away=game.get("away_team", ""),
                          commence=commence)
        trackers[event_id] = tr

    prev = tr.state
    transition = classify_transition(prev, state)
    if state == PREGAME:
        tr.pregame_poll_times.append(now)
    tr.state = state

    if transition and not tr.transition_logged:
        tr.detected_at = now
        decision = simulate_finalize(tr.pregame_poll_times, now)
        log.emit("start_transition", {
            "sport": sport, "event_id": event_id, "matchup": tr.matchup,
            "commence": iso(commence), "transition": transition,
            "detected_at": iso(now),
            "detected_minus_commence_s": (now - commence).total_seconds(),
            "pregame_polls": len(tr.pregame_poll_times),
            "would_finalize": decision,
        })
        tr.transition_logged = True

    if tr.transition_logged:
        maybe_log_mlb_first_pitch(log, tr)


def _prune(log: ShadowLog, trackers: dict[str, EventTracker], now: datetime) -> None:
    for event_id in list(trackers.keys()):
        tr = trackers[event_id]
        past_cap = now > tr.commence + timedelta(minutes=POST_MINUTES)
        if not past_cap:
            continue
        # Cap reached still pregame and never detected live → SAFE_BUT_EARLY-equivalent.
        if not tr.transition_logged and not tr.capped:
            log.emit("cap_reached_pregame", {
                "sport": tr.sport, "event_id": event_id, "matchup": tr.matchup,
                "commence": iso(tr.commence), "pregame_polls": len(tr.pregame_poll_times),
                "would_finalize": {"quality": WOULD_SAFE_BUT_EARLY},
            })
            tr.capped = True
        del trackers[event_id]


def run_daily_probes(log: ShadowLog) -> None:
    # The in-season /sports set is cached for the process lifetime; drop it so a
    # multi-day run re-probes coverage daily (Phase 0 item 2).
    odds_api._active_sports_cache = None
    sports = sports_to_watch()
    if PINNACLE_PROBE_ON:
        for sport in sports:
            probe_pinnacle_coverage(log, sport)
    if HISTORICAL_PROBE_ON:
        for sport in sports:
            probe_historical_granularity(log, sport)


def main():
    if not ODDS_API_KEY:
        raise RuntimeError("ODDS_API_KEY is required")
    log = ShadowLog(LOG_PATH)
    trackers: dict[str, EventTracker] = {}
    coverage = CoverageCache()
    active_keys = fetch_active_sport_keys()
    log.emit("monitor_started", {
        "poll_seconds": POLL_SECONDS, "margin_seconds": MARGIN_SECONDS,
        "max_age_seconds": MAX_AGE_SECONDS, "sports": sports_to_watch(),
        "probes": {"book_freshness": BOOK_FRESHNESS_ON, "events_stability": EVENTS_STABILITY_ON,
                   "historical": HISTORICAL_PROBE_ON, "pinnacle": PINNACLE_PROBE_ON},
    })
    run_daily_probes(log)
    next_daily = time.monotonic() + DAILY_PROBE_SECONDS
    while True:
        try:
            active_keys = fetch_active_sport_keys() or active_keys
            poll_once(log, trackers, coverage, active_keys)
            if time.monotonic() >= next_daily:
                run_daily_probes(log)
                next_daily = time.monotonic() + DAILY_PROBE_SECONDS
        except Exception as exc:  # never let one bad poll kill the monitor
            log.emit("loop_error", {"error": redact_request_error(exc)})
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
