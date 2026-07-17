"""
Pure helpers for parlay bets: parsing the Legs JSON column, converting
between American and decimal odds, and combining per-leg results into a
single parlay outcome + effective combined price.

No I/O and no Sheets/Odds-API calls live here -- this is the math/logic
layer reused by sheets_reader (parse), poller/resolver (combine results),
and closing_odds (combine closing lines). Keeping it dependency-free also
keeps it trivially unit-testable.
"""

import json

from config import (
    RESULT_WIN, RESULT_LOSS, RESULT_PUSH, RESULT_VOID,
    RESULT_HALF_WIN, RESULT_HALF_LOSS,
    AUTOMATED_LEG_BET_TYPES, BET_TYPE_MONEYLINE,
)


# ── Leg JSON parsing ──────────────────────────────────────────────────────────

def parse_legs(legs_raw: str) -> list[dict]:
    """
    Parses the Legs cell (a JSON array written by the Log Bet Wizard) into a
    list of normalized leg dicts whose keys match what resolver.resolve() and
    closing_odds.fetch_closing_odds() already expect (snake_case: team1,
    team2, game_date, game_start, bet_type, selection, odds_taken, sport,
    book) -- so each leg can be passed straight into those functions with no
    further reshaping.

    Returns [] for blank/invalid input rather than raising -- a malformed
    Legs cell should make the parlay fall back to manual handling, not crash
    the whole run.
    """
    if not legs_raw or not legs_raw.strip():
        return []
    try:
        data = json.loads(legs_raw)
    except (ValueError, TypeError):
        print(f"[parlay] Could not parse Legs JSON: {legs_raw!r}")
        return []
    if not isinstance(data, list):
        return []

    legs = []
    for leg in data:
        if not isinstance(leg, dict):
            continue
        legs.append({
            "sport":      str(leg.get("sport", "")).strip(),
            "book":       str(leg.get("book", "")).strip(),
            "team1":      str(leg.get("team1", "")).strip(),
            "team2":      str(leg.get("team2", "")).strip(),
            "game_date":  str(leg.get("gameDate", leg.get("game_date", ""))).strip(),
            "game_start": str(leg.get("gameStart", leg.get("game_start", ""))).strip(),
            "bet_type":   str(leg.get("betType", leg.get("bet_type", ""))).strip(),
            "selection":  str(leg.get("selection", "")).strip(),
            "odds_taken": str(leg.get("oddsTaken", leg.get("odds_taken", ""))).strip(),
            "market_key": str(leg.get("marketKey", leg.get("market_key", ""))).strip(),
            "event_id":   str(leg.get("eventId", leg.get("event_id", ""))).strip(),
        })
    return legs


def all_legs_automatable(legs: list[dict]) -> bool:
    """
    True only if every leg is a bet type the resolver/closing pipeline can
    handle automatically AND carries the game-identifying fields needed to
    look it up. A parlay with even one Prop / manually-tracked / incomplete
    leg can't be auto-resolved or auto-priced -- it falls back to manual
    Result entry (which calculate_pl_and_payout still handles fine from the
    stored combined odds).
    """
    if not legs:
        return False
    for leg in legs:
        if leg["bet_type"] not in AUTOMATED_LEG_BET_TYPES:
            return False
        # Tennis is auto-graded via ESPN, but only for match-winner
        # (Moneyline) bets -- a tennis Spread/Total leg can't be auto-graded
        # from a winner-only result yet, so the whole parlay falls back to
        # manual settlement.
        if leg["sport"].lower().startswith("tennis_") and leg["bet_type"] != BET_TYPE_MONEYLINE:
            return False
        if not (leg["sport"] and leg["team1"] and leg["selection"]
                and leg["game_date"] and leg["game_start"]):
            return False
    return True


# ── Odds conversion ───────────────────────────────────────────────────────────

def american_to_decimal(american) -> float | None:
    """American odds (int/float/str like '-110' or '+150') → decimal odds."""
    try:
        a = float(str(american).replace("+", "").strip())
    except (TypeError, ValueError):
        return None
    if a > 0:
        return 1 + a / 100.0
    if a < 0:
        return 1 + 100.0 / abs(a)
    return None


def decimal_to_american(decimal: float | None) -> int | None:
    """
    Decimal odds → American odds, truncating toward zero on the magnitude so a
    half-point price (decimal 2.105 = +110.5) becomes +110, matching how books
    present it and the odds-tool wizard/server. American is a display label
    here; settlement uses the exact decimal.
    """
    import math
    if decimal is None or decimal <= 1:
        return None
    # Epsilon before flooring: a price that is mathematically an integer (e.g.
    # decimal 1.909090... = exactly -110) can come back as 109.99999999 in
    # float, which a bare floor would wrongly truncate to -109. 1e-9 is far
    # smaller than the half-point gap we intend to truncate, so it only
    # corrects float noise, never a genuine .5.
    eps = 1e-9
    if decimal >= 2.0:
        return math.floor((decimal - 1) * 100 + eps)
    return -math.floor(100.0 / (decimal - 1) + eps)


def fmt_american(american: int | None) -> str | None:
    """American int → string with an explicit + for positives ('+150', '-110')."""
    if american is None:
        return None
    return f"+{american}" if american > 0 else str(american)


def combined_decimal(decimals: list[float]) -> float | None:
    """Product of a list of decimal odds. None if the list is empty/invalid."""
    if not decimals:
        return None
    product = 1.0
    for d in decimals:
        if d is None or d <= 0:
            return None
        product *= d
    return product


# ── Result combination ────────────────────────────────────────────────────────

def combine_parlay_results(leg_outcomes: list[tuple[str, float]]) -> tuple[str, float | None]:
    """
    Collapses per-leg (result, taken_decimal) pairs into a single parlay
    outcome and the EFFECTIVE combined decimal price to settle it at.

    Rules (standard sportsbook parlay behavior):
      - Any LOSS leg  -> the whole parlay LOSES. (Effective price irrelevant.)
      - PUSH / VOID legs DROP OUT of the parlay -- they neither win nor lose
        it; the parlay reduces to the remaining legs and re-prices at the
        product of only the surviving (won) legs' odds. This is why the
        effective price is recomputed here rather than trusting the stored
        combined OddsTaken, which assumed every leg counted.
      - If there is at least one WIN and no LOSS -> WIN at the product of the
        surviving legs' decimal odds.
      - If every leg pushed/voided (no win, no loss) -> stake is returned:
        VOID if every leg voided, otherwise PUSH. Effective decimal 1.0.

    Args:
        leg_outcomes: list of (result, taken_decimal) -- result is one of
                      WIN/LOSS/PUSH/VOID, taken_decimal is that leg's decimal
                      odds as placed (used only for surviving WIN legs).

    Returns:
        (combined_result, effective_decimal)
          combined_result: WIN / LOSS / PUSH / VOID
          effective_decimal: the decimal price to feed P/L math on a WIN, or
                             1.0 for an all-push/void refund, or None for a LOSS.
    """
    if not leg_outcomes:
        return RESULT_VOID, 1.0

    results = [r for r, _ in leg_outcomes]

    # A HALF WIN / HALF LOSS leg means a quarter line inside the parlay. Books'
    # conventions for combining a half-result into a parlay vary (some settle the
    # leg at reduced stake, some at reduced odds) and it's rare — accuracy first,
    # route the whole parlay to manual (trap #1) rather than pick a convention.
    if RESULT_HALF_WIN in results or RESULT_HALF_LOSS in results:
        raise ValueError(
            "[parlay] parlay contains a HALF WIN/HALF LOSS (quarter-line) leg -- "
            "book conventions vary; manual settlement"
        )

    if RESULT_LOSS in results:
        return RESULT_LOSS, None

    surviving = [d for r, d in leg_outcomes if r == RESULT_WIN]

    if surviving:
        return RESULT_WIN, combined_decimal(surviving)

    # No wins and no losses -> every leg pushed or voided -> stake returned.
    if all(r == RESULT_VOID for r in results):
        return RESULT_VOID, 1.0
    return RESULT_PUSH, 1.0
