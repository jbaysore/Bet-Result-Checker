"""
Fetches historical closing odds from The Odds API for bets whose games
have already started. Ports the proven logic from odds-tool's
backfillClosingOdds.js and Betting-Tracker's main.py into Python.

The historical endpoint (/v4/historical/sports/{sport}/odds) returns a
snapshot of odds at a specific UTC timestamp — queried at 1 minute before
recorded game start, which gives true closing-line odds regardless of
when this script actually runs.
"""

import re
import time
import requests
from datetime import datetime, timedelta, timezone

from config import (
    ODDS_API_KEY, ODDS_API_BASE,
    BET_TYPE_MONEYLINE, BET_TYPE_SPREAD, BET_TYPE_TOTAL, BET_TYPE_DRAW,
    CLOSING_ODDS_GAME_NOT_FOUND, CLOSING_ODDS_BOOK_NOT_FOUND,
    CLOSING_ODDS_SELECTION_NOT_FOUND, CLOSING_ODDS_MANUAL_REQUIRED,
    CLOSING_ODDS_SPORT_NOT_ON_API,
)
from poller import _parse_game_datetime
from sources.odds_api import sport_has_odds_feed

# One historical snapshot per (sport, timestamp, book, market) per process —
# multiple bets on the same game/book reuse the same API response.
_snapshot_cache: dict[tuple[str, str, str, str], list | None] = {}
# Proactive worker dedupe: bets sharing sport/book/market in one queue cycle
# reuse a live response. The TTL stays below the worker's default 30s poll so a
# later ladder step always receives a fresh quote.
_live_snapshot_cache: dict[tuple, tuple[float, list]] = {}
_live_events_cache: dict[str, tuple[float, list]] = {}
_LIVE_SNAPSHOT_TTL_SECONDS = 20

# Odds API market keys that share spread-style extraction (team + point).
_SPREAD_MARKETS = frozenset({"spreads", "alternate_spreads", "spreads_h1"})
# Odds API market keys that share game-total extraction (Over/Under + point).
_TOTAL_MARKETS = frozenset({"totals", "alternate_totals", "totals_1st_5_innings"})
_TEAM_TOTAL_MARKET = "team_totals"
_ADDITIONAL_MARKETS = frozenset({
    "alternate_spreads", "alternate_totals", "team_totals",
    "spreads_h1", "totals_1st_5_innings",
})


# ── Region routing ────────────────────────────────────────────────────────────
# Mirrors odds-tool's bookConstants.js regionForBookKey() and
# Betting-Tracker's region_for_book_key() exactly.

# Books served under The Odds API's us_ex region. All of these route their
# historical snapshot request to regions=us_ex (see region_for_book_key).
# Mirrors odds-tool bookConstants.js EXCHANGE_KEYS.
_EXCHANGE_KEYS = {"polymarket", "kalshi", "novig", "betopenly", "prophetx"}

# Books whose closing line is NOT retrievable from The Odds API historical feed
# and therefore always route to manual entry (or a book-specific API, as Kalshi
# does via its own candlesticks). This is a STRICT SUBSET of _EXCHANGE_KEYS:
# ProphetX and Novig are intentionally excluded because both are carried on The
# Odds API under the us_ex region (same as odds-tool's live fetches), so their
# historical snapshots can and should be queried like any other book.
_NON_ODDS_API_KEYS = {"polymarket", "kalshi", "betopenly"}

_US2_KEYS = {
    "ballybet", "betanysports", "betparx", "espnbet", "fliff",
    "hardrockbet", "hardrockbet_az", "hardrockbet_fl",
    "hardrockbet_oh", "rebet",
}


def is_exchange_book(book_key: str) -> bool:
    """True for us_ex-region exchange / prediction-market books (includes ProphetX)."""
    return (book_key or "").strip().lower() in _EXCHANGE_KEYS


def needs_manual_closing_odds(book_key: str) -> bool:
    """
    True for books whose closing line can't be pulled from The Odds API
    historical feed (prediction markets priced via their own venue). ProphetX
    and Novig are deliberately NOT here — both are on The Odds API us_ex region
    and are fetched like normal books.
    """
    return (book_key or "").strip().lower() in _NON_ODDS_API_KEYS


def region_for_book_key(book_key: str) -> str:
    k = (book_key or "").strip().lower()
    if k in _EXCHANGE_KEYS:
        return "us_ex"
    if k in _US2_KEYS:
        return "us2"
    return "us"


def _points_equal(a, b) -> bool:
    """Tolerant float compare for spread/total lines (3 vs 3.0)."""
    try:
        return abs(float(a) - float(b)) < 1e-6
    except (TypeError, ValueError):
        return False


def _extract_mode_for_market(market: str) -> str:
    if market in _SPREAD_MARKETS:
        return "spread"
    if market in _TOTAL_MARKETS:
        return "total"
    if market == _TEAM_TOTAL_MARKET:
        return "team_total"
    if market == "h2h":
        return "h2h"
    return "unknown"


def _apply_market_key_override(sel: dict, market_key: str) -> dict:
    """When Market Key is set on the sheet, query only that market."""
    mk = (market_key or "").strip()
    if not mk:
        return sel
    out = dict(sel)
    out["markets_to_try"] = [mk]
    out["extract_mode"] = _extract_mode_for_market(mk)
    return out


def _apply_live_market_family(sel: dict, market_key: str) -> dict:
    """Prefer the logged market, then try its main/alternate sibling live.

    A line that was the mainline when the bet was logged can become an
    alternate before kickoff. Historical resolution keeps its existing strict
    Market Key semantics; the proactive worker can safely inspect both live
    markets while the event is still available.
    """
    mk = (market_key or "").strip()
    if not mk:
        return sel
    siblings = {
        "spreads": ["spreads", "alternate_spreads"],
        "alternate_spreads": ["alternate_spreads", "spreads"],
        "totals": ["totals", "alternate_totals"],
        "alternate_totals": ["alternate_totals", "totals"],
        "team_totals": ["team_totals"],
        "spreads_h1": ["spreads_h1"],
        "totals_1st_5_innings": ["totals_1st_5_innings"],
        "h2h": ["h2h"],
    }
    out = dict(sel)
    out["markets_to_try"] = siblings.get(mk, [mk])
    out["extract_mode"] = _extract_mode_for_market(mk)
    return out


# ── Selection parsing ─────────────────────────────────────────────────────────
# Ports backfillClosingOdds.js:parseSelection() with alternate/team-total support.

def parse_selection(bet_type: str, selection: str) -> dict | None:
    """
    Maps bet_type + selection string to Odds API markets + lookup fields.

    Returns a dict with keys:
        markets_to_try, extract_mode, selection_team, selection_side, selection_point
    Returns None for unsupported types (Parlay, Prop) or unparseable formats.
    """
    t = (bet_type or "").strip()
    s = (selection or "").strip()

    if t == BET_TYPE_MONEYLINE:
        return {
            "markets_to_try": ["h2h"],
            "extract_mode": "h2h",
            "selection_team": s,
            "selection_side": None,
            "selection_point": None,
        }

    if t == BET_TYPE_DRAW:
        return {
            "markets_to_try": ["h2h"],
            "extract_mode": "h2h",
            "selection_team": "Draw",
            "selection_side": None,
            "selection_point": None,
        }

    if t == BET_TYPE_SPREAD:
        # Format: "Team Name +/-X.X"
        m = re.match(r"^(.+?)\s+([-+]?\d+(?:\.\d+)?)\s*$", s)
        if not m:
            print(f"[closing_odds] Unrecognised spread format: '{s}'")
            return None
        return {
            "markets_to_try": ["spreads", "alternate_spreads"],
            "extract_mode": "spread",
            "selection_team": m.group(1).strip(),
            "selection_side": None,
            "selection_point": float(m.group(2)),
        }

    if t == BET_TYPE_TOTAL:
        # Team total: "Braves Team Total Over 4.5" (odds-tool scanner format)
        tm = re.match(
            r"^(.+?)\s+Team Total\s+(Over|Under)\s+([\d.]+)\s*$",
            s,
            re.IGNORECASE,
        )
        if tm:
            return {
                "markets_to_try": [_TEAM_TOTAL_MARKET],
                "extract_mode": "team_total",
                "selection_team": tm.group(1).strip(),
                "selection_side": tm.group(2).lower(),
                "selection_point": float(tm.group(3)),
            }

        # Game total: "Over X.X" or "Under X.X"
        m = re.match(r"^(Over|Under)\s+([\d.]+)\s*$", s, re.IGNORECASE)
        if not m:
            print(f"[closing_odds] Unrecognised total format: '{s}'")
            return None
        return {
            "markets_to_try": ["totals", "alternate_totals"],
            "extract_mode": "total",
            "selection_team": None,
            "selection_side": m.group(1).lower(),
            "selection_point": float(m.group(2)),
        }

    # Parlay, Prop, etc. — not supported by the odds endpoint
    return None


# ── Game matching ─────────────────────────────────────────────────────────────
# Case-insensitive substring matching on team names, then commence-time
# disambiguation. Mirrors sources/odds_api.py:_select_matching_game() so a
# repeated matchup (a doubleheader, or the same two teams on back-to-back days
# both present in one snapshot) can't resolve to the wrong — possibly
# not-yet-started — game and record a non-closing price as the closing line.

# Same tolerance the /scores matcher uses. A doubleheader's two games are only
# a few hours apart, so the closest commence still wins well inside this window.
GAME_TIME_MATCH_TOLERANCE_SECONDS = 12 * 60 * 60


def _parse_iso_utc(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def find_event(events: list, team1: str, team2: str,
               expected_start: datetime | None = None) -> dict | None:
    t1 = team1.lower().strip()
    t2 = team2.lower().strip()
    candidates = []
    for ev in (events or []):
        home = ev.get("home_team", "").lower()
        away = ev.get("away_team", "").lower()
        if (t1 in home or t1 in away) and (t2 in home or t2 in away):
            candidates.append(ev)

    if not candidates:
        return None

    # A lone name match has no ambiguity — return it even if the event carries no
    # commence_time. The wrong-game risk only arises with a repeated matchup.
    if len(candidates) == 1:
        return candidates[0]

    # 2+ candidates (doubleheader, or same teams across days in one snapshot):
    # the closest commence to the expected start wins. Without an expected start
    # there's nothing to disambiguate with, so defer rather than guess.
    if expected_start is None:
        return None
    expected_utc = expected_start.astimezone(timezone.utc)
    timed = []
    for ev in candidates:
        commence = _parse_iso_utc(ev.get("commence_time"))
        if commence is None:
            continue
        delta = abs((commence - expected_utc).total_seconds())
        if delta <= GAME_TIME_MATCH_TOLERANCE_SECONDS:
            timed.append((delta, ev))
    timed.sort(key=lambda item: item[0])
    return timed[0][1] if timed else None


# ── Odds extraction ───────────────────────────────────────────────────────────
# Ports backfillClosingOdds.js:extractOdds().

def extract_odds(extract_mode: str, outcomes: list,
                 selection_team: str | None, selection_side: str | None,
                 selection_point: float | None) -> int | None:
    if extract_mode == "h2h":
        sel = (selection_team or "").lower()
        if sel == "draw":
            o = next((o for o in outcomes if o.get("name", "").lower() == "draw"), None)
        else:
            o = next((o for o in outcomes if sel in o.get("name", "").lower()), None)
        return o["price"] if o else None

    if extract_mode == "spread":
        sel = (selection_team or "").lower()
        o = next(
            (o for o in outcomes
             if sel in o.get("name", "").lower()
             and _points_equal(o.get("point"), selection_point)),
            None,
        )
        return o["price"] if o else None

    if extract_mode == "total":
        o = next(
            (o for o in outcomes
             if o.get("name", "").lower() == selection_side
             and _points_equal(o.get("point"), selection_point)),
            None,
        )
        return o["price"] if o else None

    if extract_mode == "team_total":
        sel = (selection_team or "").lower()
        o = next(
            (o for o in outcomes
             if o.get("name", "").lower() == selection_side
             and sel in (o.get("description") or "").lower()
             and _points_equal(o.get("point"), selection_point)),
            None,
        )
        return o["price"] if o else None

    return None


# ── Odds math ─────────────────────────────────────────────────────────────────
# Port of Betting-Tracker main.py functions and noVig.js equivalents.

def to_decimal_odds(american: int | float) -> float | None:
    try:
        v = float(american)
    except (TypeError, ValueError):
        return None
    if v > 0:
        return round(1 + v / 100, 8)
    if v < 0:
        return round(1 + 100 / abs(v), 8)
    return None


def calc_clv(decimal_taken: float | None, decimal_closing: float | None) -> float | None:
    if not decimal_taken or not decimal_closing:
        return None
    return round((decimal_taken / decimal_closing - 1) * 100, 2)


def clv_for_sheet(clv_pct: float) -> float:
    """Divides CLV% by 100 so Sheets formats it as a percentage (e.g. 0.0543 → 5.43%)."""
    return round(clv_pct / 100, 6)


def fmt_odds(price: int) -> str:
    """Formats American price as a string with explicit + for positives."""
    return f"+{price}" if price > 0 else str(price)


# ── API call ──────────────────────────────────────────────────────────────────

def _fetch_historical_snapshot(sport: str, date_iso: str, book_key: str,
                                market: str) -> list | None:
    """
    Calls the historical odds endpoint and returns the events list, or None
    on any error. Retries up to 3 times on transient failures.
    """
    cache_key = (sport, date_iso, book_key.lower(), market)
    if cache_key in _snapshot_cache:
        return _snapshot_cache[cache_key]

    url = f"{ODDS_API_BASE}/historical/sports/{sport}/odds"
    params = {
        "apiKey":      ODDS_API_KEY,
        "regions":     region_for_book_key(book_key),
        "markets":     market,
        "oddsFormat":  "american",
        "dateFormat":  "iso",
        "date":        date_iso,
        "bookmakers":  book_key,
    }

    for attempt in range(3):
        try:
            resp = requests.get(url, params=params, timeout=15)

            if resp.status_code == 401:
                print("[closing_odds] Invalid API key.")
                return None
            if resp.status_code == 422:
                detail = ""
                try:
                    err = resp.json()
                    detail = f" — {err.get('error_code') or ''}: {err.get('message') or resp.text[:200]}"
                except ValueError:
                    detail = f" — {resp.text[:200]}"
                print(f"[closing_odds] Historical odds request rejected for sport '{sport}' "
                      f"(HTTP 422){detail}")
                return None
            if resp.status_code == 429:
                print("[closing_odds] Odds API quota exceeded.")
                return None

            resp.raise_for_status()

            remaining = resp.headers.get("x-requests-remaining", "unknown")
            used = resp.headers.get("x-requests-used", "unknown")
            print(f"[closing_odds] Credits used: {used} | Remaining: {remaining}")

            data = resp.json()
            events = data.get("data", [])
            _snapshot_cache[cache_key] = events
            return events

        except requests.RequestException as e:
            if attempt < 2:
                print(f"[closing_odds] Request failed (attempt {attempt + 1}/3): {e}. Retrying...")
                time.sleep(5)
            else:
                print(f"[closing_odds] Request failed after 3 attempts: {e}")
                return None

    return None


def _fetch_live_events(sport: str) -> list:
    """Fetch the current event roster used to resolve event-specific markets."""
    cached = _live_events_cache.get(sport)
    if cached and time.monotonic() - cached[0] < _LIVE_SNAPSHOT_TTL_SECONDS:
        return cached[1]
    url = f"{ODDS_API_BASE}/sports/{sport}/events"
    try:
        resp = requests.get(url, params={"apiKey": ODDS_API_KEY, "dateFormat": "iso"}, timeout=15)
        if resp.status_code == 401:
            raise RuntimeError("invalid Odds API key")
        if resp.status_code == 429:
            raise RuntimeError("Odds API quota exceeded")
        resp.raise_for_status()
        events = resp.json()
        _live_events_cache[sport] = (time.monotonic(), events)
        return events
    except requests.RequestException as exc:
        raise RuntimeError(f"live Odds API event request failed: {exc}") from exc


def _fetch_live_snapshot(sport: str, book_key: str, market: str,
                         team1: str = "", team2: str = "",
                         expected_start: datetime | None = None) -> list | None:
    """Fetch a current pregame market, using the event endpoint when required."""
    event_id = None
    if market in _ADDITIONAL_MARKETS:
        event = find_event(_fetch_live_events(sport), team1, team2, expected_start)
        event_id = event.get("id") if event else None
        if not event_id:
            raise RuntimeError(f"game unavailable for additional market: {team1} vs {team2}")
    cache_key = (sport, event_id or "sport", book_key.lower(), market)
    cached = _live_snapshot_cache.get(cache_key)
    if cached and time.monotonic() - cached[0] < _LIVE_SNAPSHOT_TTL_SECONDS:
        return cached[1]

    if event_id:
        url = f"{ODDS_API_BASE}/sports/{sport}/events/{event_id}/odds"
    else:
        url = f"{ODDS_API_BASE}/sports/{sport}/odds"
    params = {
        "apiKey": ODDS_API_KEY,
        "regions": region_for_book_key(book_key),
        "markets": market,
        "oddsFormat": "american",
        "dateFormat": "iso",
        "bookmakers": book_key,
    }
    try:
        resp = requests.get(url, params=params, timeout=15)
        if resp.status_code == 401:
            raise RuntimeError("invalid Odds API key")
        if resp.status_code == 422:
            raise RuntimeError(f"sport/market unavailable: {sport}/{market}")
        if resp.status_code == 429:
            raise RuntimeError("Odds API quota exceeded")
        resp.raise_for_status()
        remaining = resp.headers.get("x-requests-remaining", "unknown")
        print(f"[closing-capture] live {sport}/{market}/{book_key}; credits remaining: {remaining}")
        payload = resp.json()
        events = [payload] if event_id and isinstance(payload, dict) else payload
        _live_snapshot_cache[cache_key] = (time.monotonic(), events)
        return events
    except requests.RequestException as exc:
        raise RuntimeError(f"live Odds API request failed: {exc}") from exc


def fetch_live_closing_odds(bet: dict) -> dict:
    """
    Capture the current live pregame price for one queued single-selection bet.

    Unlike fetch_closing_odds(), this uses the current /odds endpoint and does
    not write anything. The worker keeps ladder samples in ClosingCapture and
    finalizes the latest success separately.
    """
    sport = (bet.get("sport") or "").strip()
    book = (bet.get("book") or "").strip().lower()
    team1 = bet.get("team1", "")
    team2 = bet.get("team2", "")
    bet_type = bet.get("bet_type", "")
    selection = bet.get("selection", "")

    if needs_manual_closing_odds(book):
        return {"closing_odds": None, "decimal_closing": None,
                "error": CLOSING_ODDS_MANUAL_REQUIRED}

    sel = parse_selection(bet_type, selection)
    if sel is None:
        return {"closing_odds": None, "decimal_closing": None,
                "error": f"unsupported bet type/selection: {bet_type} / {selection}"}
    sel = _apply_live_market_family(sel, bet.get("market_key", ""))

    # Disambiguate a repeated matchup by expected start. The worker passes the
    # queued Commence UTC directly; fall back to the Bets-row date/time columns.
    expected_start = _parse_iso_utc(bet.get("commence_utc"))
    if expected_start is None:
        expected_start = _parse_game_datetime(
            bet.get("game_date", ""), bet.get("game_start", ""))

    errors = []
    for market in sel["markets_to_try"]:
        try:
            events = _fetch_live_snapshot(sport, book, market, team1, team2, expected_start)
        except RuntimeError as exc:
            errors.append(str(exc))
            continue
        result = _price_from_snapshot(
            events, sport, book, team1, team2, market, sel,
            f"BetID {bet.get('bet_id', '?')} live",
            diagnose_missing=True, expected_start=expected_start,
        )
        if result and result.get("price") is not None:
            price = result["price"]
            return {
                "closing_odds": fmt_odds(price),
                "decimal_closing": to_decimal_odds(price),
                "error": None,
            }
        if result and result.get("error"):
            errors.append(result["error"])

    # Exact-point diagnostics are more actionable than a sibling market being
    # absent, so retain them when the family cascade exhausts every market.
    best_error = next(
        (error for error in errors if error.startswith("EXACT SELECTION NOT FOUND")),
        errors[-1] if errors else CLOSING_ODDS_SELECTION_NOT_FOUND,
    )

    return {
        "closing_odds": None,
        "decimal_closing": None,
        "error": best_error,
    }


def _price_from_snapshot(events: list | None, sport: str, book: str,
                         team1: str, team2: str, market: str, sel: dict,
                         label: str, diagnose_missing: bool = False,
                         expected_start: datetime | None = None) -> dict | None:
    """
    Try to extract closing price from an already-fetched snapshot.
    Returns {"price": int} on success, or a permanent-failure dict, or None
    if this market should be skipped (missing market/outcome — try next).
    """
    def _permanent(code):
        return {"price": None, "error": code}

    if events is None:
        return None  # transient — caller handles

    event = find_event(events, team1, team2, expected_start)
    if event is None:
        print(f"[closing_odds] {label}: game not found in {market} snapshot "
              f"({team1} vs {team2}, {sport}).")
        return _permanent(CLOSING_ODDS_GAME_NOT_FOUND)

    bk = next(
        (b for b in event.get("bookmakers", [])
         if b.get("key", "").lower() == book),
        None,
    )
    if bk is None:
        print(f"[closing_odds] {label}: book '{book}' not in {market} snapshot.")
        return _permanent(CLOSING_ODDS_BOOK_NOT_FOUND)

    mkt = next(
        (m for m in bk.get("markets", [])
         if m.get("key") == market),
        None,
    )
    if mkt is None:
        # Book/game found but this market absent — try next market in cascade.
        if diagnose_missing:
            return {"price": None, "error": f"MARKET NOT FOUND: {market}"}
        return None

    price = extract_odds(
        sel["extract_mode"],
        mkt.get("outcomes", []),
        sel.get("selection_team"),
        sel.get("selection_side"),
        sel.get("selection_point"),
    )

    if price is None:
        # Market present but exact line missing — try next market.
        if diagnose_missing:
            points = set()
            for outcome in mkt.get("outcomes", []):
                if (sel.get("selection_side") is not None
                        and outcome.get("name", "").lower() != sel.get("selection_side")):
                    continue
                try:
                    points.add(float(outcome["point"]))
                except (KeyError, TypeError, ValueError):
                    continue
            points = sorted(points)
            available = ", ".join(f"{p:g}" for p in points[:12]) or "none"
            if sel.get("selection_point") is None:
                detail = f"outcome {sel.get('selection_team') or sel.get('selection_side')}"
            else:
                detail = (
                    f"point {sel.get('selection_point')}; "
                    f"available points: {available}"
                )
            return {
                "price": None,
                "error": f"EXACT SELECTION NOT FOUND: {market} {detail}",
            }
        return None

    return {"price": price, "error": None}


# ── Per-bet/leg closing price lookup ──────────────────────────────────────────

def _fetch_closing_price(bet: dict, label: str) -> dict:
    """
    Core historical-snapshot lookup for ONE selection (a single bet, or one
    leg of a parlay). Returns the closing American price, or a transient /
    permanent failure signal.

    Args:
        bet:   A dict with keys sport, book, team1, team2, game_date,
               game_start, bet_type, selection, and optional market_key.
        label: A short identifier for log lines, e.g. "BetID 42" or
               "BetID 42 leg 2/3".

    Returns a dict:
        {"price": int}                     -> success (American odds int)
        {"price": None, "error": None}     -> TRANSIENT (API/parse issue) — retry
        {"price": None, "error": <CODE>}   -> PERMANENT (not found) — needs review
    """
    sport = (bet.get("sport") or "").strip()
    book = (bet.get("book") or "").strip().lower()
    team1 = bet.get("team1", "")
    team2 = bet.get("team2", "")
    bet_type = bet.get("bet_type", "")
    selection = bet.get("selection", "")

    _transient = {"price": None, "error": None}

    def _permanent(code):
        return {"price": None, "error": code}

    if needs_manual_closing_odds(book):
        if book == "kalshi":
            ticker = (bet.get("kalshi_ticker") or "").strip()
            if ticker:
                from sources.kalshi import get_closing_american
                game_dt = _parse_game_datetime(bet.get("game_date", ""), bet.get("game_start", ""))
                american = get_closing_american(ticker, game_dt, label)
                if american is not None:
                    return {"price": american, "error": None}
        print(f"[closing_odds] {label}: book '{book}' closing line not available "
              f"automatically — manual entry required.")
        return _permanent(CLOSING_ODDS_MANUAL_REQUIRED)

    if not sport_has_odds_feed(sport):
        print(f"[closing_odds] {label}: sport '{sport}' is not currently "
              f"active on The Odds API — manual entry required.")
        return _permanent(CLOSING_ODDS_SPORT_NOT_ON_API)

    sel = parse_selection(bet_type, selection)
    if sel is None:
        print(f"[closing_odds] {label}: unsupported bet type '{bet_type}' — skipping.")
        return _transient

    sel = _apply_market_key_override(sel, bet.get("market_key", ""))

    game_dt = _parse_game_datetime(bet.get("game_date", ""), bet.get("game_start", ""))
    if game_dt is None:
        print(f"[closing_odds] {label}: could not parse game datetime — skipping.")
        return _transient

    snapshot_dt = game_dt.astimezone(timezone.utc) - timedelta(minutes=1)
    date_iso = snapshot_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    markets_to_try = sel["markets_to_try"]
    had_transient = False
    last_permanent = None

    for market in markets_to_try:
        print(f"[closing_odds] {label}: fetching {market} snapshot "
              f"for {team1} vs {team2} at {date_iso} (book: {book})")

        events = _fetch_historical_snapshot(sport, date_iso, book, market)
        if events is None:
            had_transient = True
            continue

        result = _price_from_snapshot(
            events, sport, book, team1, team2, market, sel, label,
            expected_start=game_dt,
        )
        if result is None:
            continue  # try next market in cascade
        if result.get("price") is not None:
            return result
        # Permanent game/book failure — don't cascade to other markets.
        if result.get("error") in (
            CLOSING_ODDS_GAME_NOT_FOUND,
            CLOSING_ODDS_BOOK_NOT_FOUND,
        ):
            return result
        last_permanent = result

    if had_transient and last_permanent is None:
        return _transient

    print(f"[closing_odds] {label}: could not extract price for "
          f"selection '{selection}' from any market tried "
          f"({', '.join(markets_to_try)}).")
    return last_permanent or _permanent(CLOSING_ODDS_SELECTION_NOT_FOUND)


def _clv_from_decimals(decimal_taken: float | None, decimal_closing: float | None) -> float | None:
    """Sheet-format CLV from a taken/closing decimal pair, or None if unknown."""
    clv_pct = calc_clv(decimal_taken, decimal_closing)
    return clv_for_sheet(clv_pct) if clv_pct is not None else None


# ── Main entry points ─────────────────────────────────────────────────────────

def fetch_closing_odds(bet: dict) -> dict:
    """
    Fetches historical closing odds for a single (non-parlay) bet.

    Args:
        bet: A dict from sheets_reader.load_bets_needing_closing_odds(),
             with keys: sport, book, team1, team2, game_date, game_start,
             bet_type, selection, odds_taken, and optional market_key.

    Returns a dict with:
        closing_odds:     American odds string (e.g. "-110") or None on failure
        decimal_closing:  Decimal float or None
        clv:              Sheet-format CLV (e.g. 0.0543) or None
        error:            None for transient failures (retry), an error code
                          string for permanent ones (write to sheet for review)
    """
    bet_id = bet.get("bet_id", "?")
    res = _fetch_closing_price(bet, f"BetID {bet_id}")

    if res["price"] is None:
        return {"closing_odds": None, "decimal_closing": None, "clv": None,
                "error": res["error"]}

    price = res["price"]
    closing_odds_str = fmt_odds(price)
    decimal_closing = to_decimal_odds(price)

    clv = None
    try:
        odds_taken = float(str(bet.get("odds_taken", "")).replace("+", "").strip())
        clv = _clv_from_decimals(to_decimal_odds(odds_taken), decimal_closing)
    except (ValueError, TypeError):
        pass

    print(f"[closing_odds] BetID {bet_id}: ClosingOdds={closing_odds_str}, "
          f"DecimalClosing={decimal_closing}, CLV={clv}")

    return {
        "closing_odds": closing_odds_str,
        "decimal_closing": decimal_closing,
        "clv": clv,
        "error": None,
    }


def fetch_parlay_closing_odds(bet: dict) -> dict:
    """
    Fetches the combined historical closing line for a parlay: the product of
    each leg's closing decimal odds. CLV compares the parlay's combined OddsTaken
    against this combined closing line.
    """
    from parlay import american_to_decimal, decimal_to_american, fmt_american, combined_decimal

    bet_id = bet.get("bet_id", "?")
    legs = bet.get("legs") or []

    if not legs:
        print(f"[closing_odds] BetID {bet_id}: parlay has no parsed legs — skipping.")
        return {"closing_odds": None, "decimal_closing": None, "clv": None, "error": None}

    leg_decimals = []
    for i, leg in enumerate(legs, start=1):
        res = _fetch_closing_price(leg, f"BetID {bet_id} leg {i}/{len(legs)}")
        if res["price"] is None:
            return {"closing_odds": None, "decimal_closing": None, "clv": None,
                    "error": res["error"]}
        leg_decimals.append(to_decimal_odds(res["price"]))

    decimal_closing = combined_decimal(leg_decimals)
    if decimal_closing is None:
        return {"closing_odds": None, "decimal_closing": None, "clv": None, "error": None}

    decimal_closing = round(decimal_closing, 8)
    closing_odds_str = fmt_american(decimal_to_american(decimal_closing))

    clv = None
    decimal_taken = american_to_decimal(bet.get("odds_taken", ""))
    if decimal_taken is not None:
        clv = _clv_from_decimals(decimal_taken, decimal_closing)

    print(f"[closing_odds] BetID {bet_id}: parlay ({len(legs)} legs) "
          f"ClosingOdds={closing_odds_str}, DecimalClosing={decimal_closing}, CLV={clv}")

    return {
        "closing_odds": closing_odds_str,
        "decimal_closing": decimal_closing,
        "clv": clv,
        "error": None,
    }
