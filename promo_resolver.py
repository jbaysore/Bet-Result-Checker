"""
promo_resolver.py
─────────────────────────────────────────────────────────────────────────────
Pure decision logic for the Promotion Updater -- given a Pending Promotions
row and every Bets row linked to it by Promo ID, decides whether that promo
can be finalized yet, and if so, with what Status/Realized Amount.

Mirrors resolver.py's separation of concerns: no Sheets I/O happens in this
file at all (no gspread imports, no network calls) -- promo_trigger.py is
responsible for reading via sheets_reader.py and writing via
sheets_writer.py. This file only ever takes plain dicts in and returns a
plain "verdict" dict out, which keeps the actual decision logic something
that could be unit-tested in isolation, same as resolver.py's functions.

Built 2026-06-21 against the conceptual design worked out directly with the
user (sport betting promo mechanics vary enough between types -- Bonus Bet,
Profit Boost, Deposit Bonus, Insurance Bet -- that each needed its own
dedicated conversation before any of this logic could be trusted). Per the
agreed incremental build plan, ONLY Bonus Bet is actually implemented here.
The other three intentionally raise/skip rather than guess.

THE BONUS BET MODEL (confirmed in design conversation, 2026-06-21):
  - A token is earned by placing a Qualifying Bet (Bet Category =
    "Qualifying Bet", same Promo ID) before the promo's Expiration Date --
    the qualifying bet's RESULT does not matter, only that it was placed
    in-window. Each promo grants up to Expected Reward Count tokens this
    way (blank = 1, single-grant).
  - If Expiration Date passes with ZERO qualifying bets ever linked, the
    promo is "Unused" (distinct from a $0 Realized promo -- see
    config.PROMO_STATUS_UNUSED).
  - Once the qualifying window has genuinely closed (Expiration Date has
    passed, OR Expected Reward Count has already been hit), the earned
    token count is locked in -- no need to keep waiting even if Expiration
    Date is still in the future, once the max has been reached.
  - Each earned token has its own usage deadline: either anchored to its
    own qualifying bet's placement date ("Per Qualifying Bet" timing) or
    to Expiration Date itself ("End of Window" timing, all tokens share
    one clock) -- plus Token Usage Window (days).
  - Each token is claimed by its own Bets row (Bet Category = "Bonus
    Bet", same Promo ID). Since there is no per-token identifier anywhere
    in the sheet, claimed reward bets are matched to earned tokens
    chronologically (oldest qualifying bet's token <-> earliest unclaimed
    reward bet) -- confirmed as the intended behavior, not a guess.
  - A token's final disposition is either: claimed AND settled (its
    P/L is the token's value -- Bonus Bet category P/L is already
    profit-only on a win per resolver.calculate_pl_and_payout, so no
    further derivation is needed), or unclaimed with its deadline passed
    (forfeited, value = $0).
  - The promo only finalizes once EVERY earned token has a final
    disposition. This can take weeks if a late-claimed token sits on a
    slow-resolving game -- that's fine, it just stays Pending.
  - Qualifying Cost is backfilled (only if currently blank) as soon as
    the qualifying window closes, independent of whether the rest of the
    promo can finalize yet -- it's a stable number at that point even if
    reward tokens are still pending.
─────────────────────────────────────────────────────────────────────────────
"""

from datetime import datetime, date, timedelta

from config import (
    BET_CATEGORY_QUALIFYING, BET_CATEGORY_BONUS_BET,
    PROMO_STATUS_REALIZED, PROMO_STATUS_UNUSED,
    PROMO_TYPE_BONUS_BET,
    REWARD_TIMING_PER_QUALIFYING_BET,
)


def _parse_date(s: str) -> date | None:
    """
    Parses either a plain "YYYY-MM-DD" (Expiration Date, as written by
    PromotionWizard.jsx's <input type="date">) or a "YYYY-MM-DD HH:MM"
    timestamp (Bets' Date Placed, in Central time per
    LogBetWizard.jsx's getChicagoDateTime()) into a date, discarding any
    time component -- every comparison this module makes is date-level
    only ("was this bet placed before Expiration Date," not "at what
    hour"), matching the placement-only rule confirmed in the design
    conversation (result/settlement timing is irrelevant to whether a
    bet counts, only to whether Realized Amount can be computed yet).

    Returns None for blank or unparseable input -- callers must handle
    None explicitly rather than this function guessing at a default.
    """
    if not s:
        return None
    try:
        date_part = s.strip().split(" ")[0]
        return datetime.strptime(date_part, "%Y-%m-%d").date()
    except ValueError:
        return None


def _evaluate_multi_grant_promo(promo: dict, linked_bets: list[dict], today: date,
                                 reward_category: str, compute_token_value) -> dict:
    """
    Shared core for any promo type with Bonus Bet's multi-grant token
    model (currently: Bonus Bet only -- Profit Boost will reuse this
    exact function once it's built, differing only in
    `compute_token_value`, per the design conversation confirming both
    types share identical timing/expiration/FIFO-matching rules).

    Args:
        promo:           One row from sheets_reader.load_pending_promotions()
        linked_bets:      All Bets rows sharing this promo's Promo ID
                          (from sheets_reader.load_bets_by_promo_id()),
                          in ANY state -- settled or not.
        today:            Current date (Central time), injected rather
                          than computed here so this function stays pure
                          and testable without mocking datetime.now().
        reward_category:  The Bet Category string that claims a token
                          (e.g. config.BET_CATEGORY_BONUS_BET).
        compute_token_value: Callable(reward_bet: dict) -> float. Called
                          ONLY on a SETTLED reward bet (result is
                          guaranteed non-blank) -- returns that claimed
                          token's dollar contribution to Realized Amount.

    Returns a verdict dict:
        {
          "qualifying_cost_fill": float | None,  # write if not None
          "finalize": {"status": ..., "realized_amount": ...} | None,
          "log": [str, ...]  # human-readable trace of the decision, for
                              # promo_trigger.py to print
        }
    At most one of qualifying_cost_fill/finalize fires per call in
    practice for Unused (finalize only), but both can fire together on
    the same call when the qualifying window just closed AND all reward
    tokens already happen to have a final disposition in the same run.
    """
    log = []

    expiration = _parse_date(promo["expiration_date"])

    qualifying_bets = sorted(
        [b for b in linked_bets if b["bet_category"] == BET_CATEGORY_QUALIFYING],
        key=lambda b: _parse_date(b["date_placed"]) or date.min
    )
    reward_bets = sorted(
        [b for b in linked_bets if b["bet_category"] == reward_category],
        key=lambda b: _parse_date(b["date_placed"]) or date.min
    )

    # ── "Never used at all" check ──────────────────────────────────────
    if not qualifying_bets:
        if expiration is None:
            log.append("No qualifying bets linked yet, and no Expiration Date set -- "
                        "cannot determine Unused. Leaving Pending.")
            return {"qualifying_cost_fill": None, "finalize": None, "log": log}
        if today > expiration:
            log.append(f"Expiration ({expiration}) passed with zero qualifying bets "
                        f"ever linked -- finalizing as Unused.")
            return {
                "qualifying_cost_fill": None,
                "finalize": {"status": PROMO_STATUS_UNUSED, "realized_amount": 0.0},
                "log": log,
            }
        log.append(f"No qualifying bets yet, expiration ({expiration}) hasn't passed -- "
                    f"leaving Pending.")
        return {"qualifying_cost_fill": None, "finalize": None, "log": log}

    # ── Which qualifying bets actually count (placed in-window)? ───────
    if expiration is not None:
        counted_qualifiers = [
            b for b in qualifying_bets
            if (_parse_date(b["date_placed"]) or date.max) <= expiration
        ]
    else:
        counted_qualifiers = qualifying_bets

    try:
        expected_count = int(promo["expected_reward_count"]) if promo["expected_reward_count"] else 1
    except ValueError:
        expected_count = 1
    expected_count = max(expected_count, 1)

    earned_count = min(len(counted_qualifiers), expected_count)

    # ── Has the qualifying window genuinely closed? ─────────────────────
    # Either the calendar deadline passed, or the max possible tokens were
    # already earned -- no point waiting on the calendar once the cap is hit.
    window_closed = (expiration is not None and today > expiration) or (earned_count >= expected_count)

    if not window_closed:
        log.append(f"Qualifying window still open ({len(counted_qualifiers)}/{expected_count} "
                    f"qualifier(s) so far, expiration {expiration or 'none set'}) -- leaving Pending.")
        return {"qualifying_cost_fill": None, "finalize": None, "log": log}

    # Window closed -- the qualifying bet set is now final and stable, safe
    # to backfill Qualifying Cost (write function itself no-ops if a value
    # already exists, so it's safe to compute this on every run).
    qc_fill = None
    if not promo["qualifying_cost"]:
        qc_fill = round(sum(
            float(b["stake"] or 0) + float(b["fee"] or 0) for b in counted_qualifiers
        ), 2)
        log.append(f"Qualifying window closed -- backfilling Qualifying Cost = {qc_fill} "
                    f"(Stake+Fee across {len(counted_qualifiers)} qualifying bet(s)).")

    if earned_count == 0:
        log.append("Qualifying window closed with zero counted qualifying bets -- "
                    "finalizing as Unused.")
        return {
            "qualifying_cost_fill": qc_fill,
            "finalize": {"status": PROMO_STATUS_UNUSED, "realized_amount": 0.0},
            "log": log,
        }

    # ── Per-token usage deadlines ────────────────────────────────────────
    try:
        usage_days = int(promo["token_usage_window"]) if promo["token_usage_window"] else None
    except ValueError:
        usage_days = None

    reward_timing = promo["reward_timing"]

    token_deadlines = []
    for i in range(earned_count):
        if reward_timing == REWARD_TIMING_PER_QUALIFYING_BET and i < len(counted_qualifiers):
            anchor = _parse_date(counted_qualifiers[i]["date_placed"])
        else:
            # End of Window (or unset/ambiguous, e.g. a single-grant promo
            # with no Reward Timing chosen): anchor off Expiration Date,
            # falling back to the lone qualifying bet's date if there's no
            # Expiration Date at all.
            anchor = expiration or (
                _parse_date(counted_qualifiers[0]["date_placed"]) if counted_qualifiers else None
            )

        if anchor is not None and usage_days is not None:
            token_deadlines.append(anchor + timedelta(days=usage_days))
        else:
            token_deadlines.append(None)  # can't auto-forfeit without a real deadline

    # ── FIFO-match claimed reward bets to earned tokens, oldest first ───
    token_values = []
    blocked = False
    for i in range(earned_count):
        if i < len(reward_bets):
            rb = reward_bets[i]
            if not rb["result"]:
                log.append(f"Token {i + 1}/{earned_count}: claimed (BetID {rb['bet_id']}) "
                           f"but not yet settled -- waiting.")
                blocked = True
                break
            value = compute_token_value(rb)
            log.append(f"Token {i + 1}/{earned_count}: claimed and settled "
                       f"(BetID {rb['bet_id']}), value={value}.")
            token_values.append(value)
        else:
            deadline = token_deadlines[i]
            if deadline is None:
                log.append(f"Token {i + 1}/{earned_count}: unclaimed, deadline can't be "
                           f"determined (missing Token Usage Window or anchor date) -- waiting.")
                blocked = True
                break
            if today > deadline:
                log.append(f"Token {i + 1}/{earned_count}: unclaimed, deadline ({deadline}) "
                           f"passed -- forfeited, value=0.")
                token_values.append(0.0)
            else:
                log.append(f"Token {i + 1}/{earned_count}: unclaimed, deadline ({deadline}) "
                           f"hasn't passed yet -- waiting.")
                blocked = True
                break

    if blocked:
        return {"qualifying_cost_fill": qc_fill, "finalize": None, "log": log}

    realized_amount = round(sum(token_values), 2)
    log.append(f"All {earned_count} token(s) have a final disposition -- finalizing as "
               f"Realized, Realized Amount={realized_amount}.")
    return {
        "qualifying_cost_fill": qc_fill,
        "finalize": {"status": PROMO_STATUS_REALIZED, "realized_amount": realized_amount},
        "log": log,
    }


def evaluate_bonus_bet_promo(promo: dict, linked_bets: list[dict], today: date) -> dict:
    """
    Bonus Bet's token-value function: the stored P/L on a claimed,
    settled Bonus Bet-category row already IS the token's real cash
    value -- resolver.calculate_pl_and_payout() pays profit-only on a
    win for this category (the stake token is consumed regardless of
    outcome, only profit converts to real money), and $0 on a loss. No
    derivation needed, unlike Profit Boost will require.
    """
    def token_value(reward_bet: dict) -> float:
        try:
            return float(reward_bet["pl"]) if reward_bet["pl"] else 0.0
        except ValueError:
            return 0.0

    return _evaluate_multi_grant_promo(promo, linked_bets, today, BET_CATEGORY_BONUS_BET, token_value)


def evaluate_promo(promo: dict, linked_bets: list[dict], today: date) -> dict:
    """
    Dispatches a Pending promo to its type-specific evaluator. ONLY
    Bonus Bet is implemented (2026-06-21) -- Profit Boost, Deposit
    Bonus, and Insurance Bet are deliberately not yet wired up, per the
    agreed incremental build plan (each type needed -- and got -- its
    own dedicated design conversation before being trusted with real
    money math; building ahead of that would mean guessing).

    Unimplemented types return a "not_implemented" verdict rather than
    raising, so promo_trigger.py can log and move on without crashing
    the whole run over one promo type that isn't ready yet.
    """
    promo_type = promo["promo_type"]

    if promo_type == PROMO_TYPE_BONUS_BET:
        return evaluate_bonus_bet_promo(promo, linked_bets, today)

    return {
        "qualifying_cost_fill": None,
        "finalize": None,
        "log": [f"Promo Type '{promo_type}' automation isn't built yet -- skipping."],
        "not_implemented": True,
    }
