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
from datetime import timedelta, timezone

from config import (
    ODDS_API_KEY, ODDS_API_BASE,
    BET_TYPE_MONEYLINE, BET_TYPE_SPREAD, BET_TYPE_TOTAL, BET_TYPE_DRAW,
    CLOSING_ODDS_GAME_NOT_FOUND, CLOSING_ODDS_BOOK_NOT_FOUND, CLOSING_ODDS_SELECTION_NOT_FOUND,
)
from poller import _parse_game_datetime


# ── Region routing ────────────────────────────────────────────────────────────
# Mirrors odds-tool's bookConstants.js regionForBookKey() and
# Betting-Tracker's region_for_book_key() exactly.

_EXCHANGE_KEYS = {"polymarket", "kalshi", "novig", "betopenly", "prophetx"}
_US2_KEYS = {
    "ballybet", "betanysports", "betparx", "espnbet", "fliff",
    "hardrockbet", "hardrockbet_az", "hardrockbet_fl",
    "hardrockbet_oh", "rebet",
}


def region_for_book_key(book_key: str) -> str:
    k = (book_key or "").strip().lower()
    if k in _EXCHANGE_KEYS:
        return "us_ex"
    if k in _US2_KEYS:
        return "us2"
    return "us"


# ── Selection parsing ─────────────────────────────────────────────────────────
# Ports backfillClosingOdds.js:parseSelection().

def parse_selection(bet_type: str, selection: str) -> dict | None:
    """
    Maps bet_type + selection string to the Odds API market + lookup fields.

    Returns a dict with keys: market, selection_team, selection_side, selection_point.
    Returns None for unsupported types (Parlay, Prop) or unparseable formats.
    """
    t = (bet_type or "").strip()
    s = (selection or "").strip()

    if t == BET_TYPE_MONEYLINE:
        return {"market": "h2h", "selection_team": s, "selection_side": None, "selection_point": None}

    if t == BET_TYPE_DRAW:
        return {"market": "h2h", "selection_team": "Draw", "selection_side": None, "selection_point": None}

    if t == BET_TYPE_SPREAD:
        # Format: "Team Name +/-X.X"
        m = re.match(r"^(.+?)\s+([-+]?\d+(?:\.\d+)?)\s*$", s)
        if not m:
            print(f"[closing_odds] Unrecognised spread format: '{s}'")
            return None
        return {
            "market": "spreads",
            "selection_team": m.group(1).strip(),
            "selection_side": None,
            "selection_point": float(m.group(2)),
        }

    if t == BET_TYPE_TOTAL:
        # Format: "Over X.X" or "Under X.X"
        m = re.match(r"^(Over|Under)\s+([\d.]+)\s*$", s, re.IGNORECASE)
        if not m:
            print(f"[closing_odds] Unrecognised total format: '{s}'")
            return None
        return {
            "market": "totals",
            "selection_team": None,
            "selection_side": m.group(1).lower(),
            "selection_point": float(m.group(2)),
        }

    # Parlay, Prop, etc. — not supported by the odds endpoint
    return None


# ── Game matching ─────────────────────────────────────────────────────────────
# Ports backfillClosingOdds.js:findEvent() — case-insensitive substring
# matching, proven to work reliably for The Odds API team names.

def find_event(events: list, team1: str, team2: str) -> dict | None:
    t1 = team1.lower().strip()
    t2 = team2.lower().strip()
    for ev in (events or []):
        home = ev.get("home_team", "").lower()
        away = ev.get("away_team", "").lower()
        if (t1 in home or t1 in away) and (t2 in home or t2 in away):
            return ev
    return None


# ── Odds extraction ───────────────────────────────────────────────────────────
# Ports backfillClosingOdds.js:extractOdds().

def extract_odds(market: str, outcomes: list,
                 selection_team: str | None, selection_side: str | None,
                 selection_point: float | None) -> int | None:
    if market == "h2h":
        sel = (selection_team or "").lower()
        if sel == "draw":
            o = next((o for o in outcomes if o.get("name", "").lower() == "draw"), None)
        else:
            o = next((o for o in outcomes if sel in o.get("name", "").lower()), None)
        return o["price"] if o else None

    if market == "spreads":
        sel = (selection_team or "").lower()
        o = next(
            (o for o in outcomes
             if sel in o.get("name", "").lower() and o.get("point") == selection_point),
            None,
        )
        return o["price"] if o else None

    if market == "totals":
        o = next(
            (o for o in outcomes
             if o.get("name", "").lower() == selection_side and o.get("point") == selection_point),
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
                print(f"[closing_odds] Sport key '{sport}' not recognised.")
                return None
            if resp.status_code == 429:
                print("[closing_odds] Odds API quota exceeded.")
                return None

            resp.raise_for_status()

            remaining = resp.headers.get("x-requests-remaining", "unknown")
            used = resp.headers.get("x-requests-used", "unknown")
            print(f"[closing_odds] Credits used: {used} | Remaining: {remaining}")

            data = resp.json()
            return data.get("data", [])

        except requests.RequestException as e:
            if attempt < 2:
                print(f"[closing_odds] Request failed (attempt {attempt + 1}/3): {e}. Retrying...")
                time.sleep(5)
            else:
                print(f"[closing_odds] Request failed after 3 attempts: {e}")
                return None

    return None


# ── Main entry point ──────────────────────────────────────────────────────────

def fetch_closing_odds(bet: dict) -> dict:
    """
    Fetches historical closing odds for a single bet.

    Args:
        bet: A dict from sheets_reader.load_bets_needing_closing_odds(),
             with keys: sport, book, team1, team2, game_date, game_start,
             bet_type, selection, odds_taken.

    Returns a dict with:
        closing_odds:     American odds string (e.g. "-110") or None on failure
        decimal_closing:  Decimal float or None
        clv:              Sheet-format CLV (e.g. 0.0543) or None
    """
    bet_id = bet.get("bet_id", "?")
    sport = (bet.get("sport") or "").strip()
    book = (bet.get("book") or "").strip().lower()
    team1 = bet.get("team1", "")
    team2 = bet.get("team2", "")
    bet_type = bet.get("bet_type", "")
    selection = bet.get("selection", "")
    odds_taken_raw = bet.get("odds_taken", "")

    # error=None → transient failure, don't write to sheet, retry next run
    # error=<code> → permanent failure, write code to ClosingOdds for human review
    _transient = {"closing_odds": None, "decimal_closing": None, "clv": None, "error": None}

    def _permanent(code):
        return {"closing_odds": None, "decimal_closing": None, "clv": None, "error": code}

    # Parse selection into market + lookup fields
    sel = parse_selection(bet_type, selection)
    if sel is None:
        print(f"[closing_odds] BetID {bet_id}: unsupported bet type '{bet_type}' — skipping.")
        return _transient

    # Convert game start to UTC, subtract 1 minute to get closing snapshot time
    game_dt = _parse_game_datetime(bet.get("game_date", ""), bet.get("game_start", ""))
    if game_dt is None:
        print(f"[closing_odds] BetID {bet_id}: could not parse game datetime — skipping.")
        return _transient

    snapshot_dt = game_dt.astimezone(timezone.utc) - timedelta(minutes=1)
    date_iso = snapshot_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    print(f"[closing_odds] BetID {bet_id}: fetching {sel['market']} snapshot "
          f"for {team1} vs {team2} at {date_iso} (book: {book})")

    events = _fetch_historical_snapshot(sport, date_iso, book, sel["market"])
    if events is None:
        # API error (timeout, quota, network) — transient, retry next run
        return _transient

    event = find_event(events, team1, team2)
    if event is None:
        print(f"[closing_odds] BetID {bet_id}: game not found in snapshot "
              f"({team1} vs {team2}, {sport}).")
        return _permanent(CLOSING_ODDS_GAME_NOT_FOUND)

    # Find the requested bookmaker in the event
    bk = next(
        (b for b in event.get("bookmakers", [])
         if b.get("key", "").lower() == book),
        None,
    )
    if bk is None:
        print(f"[closing_odds] BetID {bet_id}: book '{book}' not in snapshot.")
        return _permanent(CLOSING_ODDS_BOOK_NOT_FOUND)

    # Find the market
    mkt = next(
        (m for m in bk.get("markets", [])
         if m.get("key") == sel["market"]),
        None,
    )
    if mkt is None:
        print(f"[closing_odds] BetID {bet_id}: market '{sel['market']}' not in snapshot.")
        return _permanent(CLOSING_ODDS_SELECTION_NOT_FOUND)

    price = extract_odds(
        sel["market"],
        mkt.get("outcomes", []),
        sel.get("selection_team"),
        sel.get("selection_side"),
        sel.get("selection_point"),
    )

    if price is None:
        print(f"[closing_odds] BetID {bet_id}: could not extract price for "
              f"selection '{selection}' from snapshot.")
        return _permanent(CLOSING_ODDS_SELECTION_NOT_FOUND)

    closing_odds_str = fmt_odds(price)
    decimal_closing = to_decimal_odds(price)

    # Compute CLV if OddsTaken is available
    clv = None
    try:
        odds_taken = float(str(odds_taken_raw).replace("+", "").strip())
        decimal_taken = to_decimal_odds(odds_taken)
        clv_pct = calc_clv(decimal_taken, decimal_closing)
        if clv_pct is not None:
            clv = clv_for_sheet(clv_pct)
    except (ValueError, TypeError):
        pass  # Missing or unparseable OddsTaken — CLV left as None, not a blocker

    print(f"[closing_odds] BetID {bet_id}: ClosingOdds={closing_odds_str}, "
          f"DecimalClosing={decimal_closing}, CLV={clv}")

    return {
        "closing_odds": closing_odds_str,
        "decimal_closing": decimal_closing,
        "clv": clv,
        "error": None,
    }
