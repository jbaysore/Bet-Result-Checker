"""
Classify bet rows for the retry_closing_odds one-shot script.

Buckets:
  skip   — unsupported type, unparseable selection, NEEDS_REVIEW, game not started
  manual — exchange / prediction-market books without Kalshi ticker backfill
  retry  — automatable bet the historical fetch can attempt
"""

from datetime import datetime, timezone

from closing_odds import needs_manual_closing_odds, parse_selection
from config import (
    AUTOMATED_BET_TYPES, BET_TYPE_PARLAY, BET_TYPE_PROP, RESULT_NEEDS_REVIEW,
)
from market_key_infer import infer_market_key
from parlay import all_legs_automatable, parse_legs
from poller import _parse_game_datetime


def _game_has_started(game_date: str, game_start: str) -> bool:
    game_dt = _parse_game_datetime(game_date, game_start)
    if game_dt is None:
        return False
    now_utc = datetime.now(timezone.utc)
    return game_dt.astimezone(timezone.utc).replace(tzinfo=timezone.utc) <= now_utc


def classify_closing_retry_row(bet: dict) -> dict:
    """
    Classify a single bet row for closing-odds retry.

    Returns:
        bucket: "skip" | "manual" | "retry"
        reason: human-readable explanation
        inferred_market_key: from infer_market_key (may be "")
    """
    bet_type = (bet.get("bet_type") or "").strip()
    selection = bet.get("selection") or ""
    notes = bet.get("notes") or ""
    book = (bet.get("book") or "").strip().lower()
    result = (bet.get("result") or "").strip()
    inferred = infer_market_key(notes, bet_type, selection)

    if result == RESULT_NEEDS_REVIEW:
        return {
            "bucket": "skip",
            "reason": "Result is NEEDS_REVIEW",
            "inferred_market_key": inferred,
        }

    if bet_type == BET_TYPE_PROP:
        return {
            "bucket": "skip",
            "reason": "Prop bets are not auto-priced",
            "inferred_market_key": inferred,
        }

    is_parlay = bet_type == BET_TYPE_PARLAY
    if is_parlay:
        legs = bet.get("legs")
        if legs is None:
            legs = parse_legs(bet.get("legs_json") or bet.get("legs_raw") or "")
        if not all_legs_automatable(legs):
            return {
                "bucket": "skip",
                "reason": "Parlay has non-automatable leg(s)",
                "inferred_market_key": inferred,
            }
    elif bet_type not in AUTOMATED_BET_TYPES:
        return {
            "bucket": "skip",
            "reason": f"Unsupported bet type '{bet_type}'",
            "inferred_market_key": inferred,
        }
    elif not is_parlay and parse_selection(bet_type, selection) is None:
        return {
            "bucket": "skip",
            "reason": "Unparseable selection for bet type",
            "inferred_market_key": inferred,
        }

    if not _game_has_started(bet.get("game_date", ""), bet.get("game_start", "")):
        return {
            "bucket": "skip",
            "reason": "Game has not started yet",
            "inferred_market_key": inferred,
        }

    if needs_manual_closing_odds(book):
        ticker = (bet.get("kalshi_ticker") or "").strip()
        if book == "kalshi" and ticker:
            return {
                "bucket": "retry",
                "reason": "Kalshi with ticker — venue API",
                "inferred_market_key": inferred,
            }
        return {
            "bucket": "manual",
            "reason": f"Book '{book}' requires manual closing odds",
            "inferred_market_key": inferred,
        }

    return {
        "bucket": "retry",
        "reason": "Automatable — historical fetch can run",
        "inferred_market_key": inferred,
    }
