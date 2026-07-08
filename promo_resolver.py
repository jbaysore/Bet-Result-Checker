"""
promo_resolver.py
─────────────────────────────────────────────────────────────────────────────
Pure decision logic for the Promotion Updater -- given a Pending Promotions
row and every Bets row linked to it by Promo ID, decides whether that promo
can be finalized yet, and if so, with what Status/Realized Amount.

Mirrors resolver.py's separation of concerns: no Sheets I/O happens in this
file at all (no gspread imports, no network calls) -- promo_trigger.py is
responsible for reading via sheets_reader.py and writing via
sheets_writer.py. The one exception is importing resolver.py itself (also
pure -- no I/O) to reuse its calculate_pl_and_payout() as the single source
of truth for Profit Boost's unboosted-baseline calculation, rather than
maintaining a second, divergent copy of that fee/boost math here.

Built across several conversations with the user (2026-06-21) -- sports
betting promo mechanics vary enough between types that each needed its own
dedicated design discussion before any of this logic could be trusted with
real money. All four types are now implemented:

  - Bonus Bet      (fully automated)
  - Profit Boost   (fully automated, reuses Bonus Bet's multi-grant core)
  - Deposit Bonus  (automated for the single-bet case; multi-bet handling
                    explicitly left open -- only one real example existed
                    during design, see evaluate_deposit_bonus_promo)
  - Insurance Bet  (automated EXCEPT the unclaimed-refund-token forfeiture
                    sub-case, which the user explicitly chose to leave for
                    manual close-out rather than approximate -- see
                    _evaluate_single_day_insurance's docstring. Also
                    supports a multi-day variant added 2026-06-23 for
                    promos like "one insured bet/day for N days" -- see
                    _evaluate_multi_day_insurance.)
─────────────────────────────────────────────────────────────────────────────
"""

from datetime import datetime, date, timedelta

from config import (
    BET_CATEGORY_QUALIFYING, BET_CATEGORY_BONUS_BET, BET_CATEGORY_PROFIT_BOOST,
    BET_CATEGORY_DEPOSIT_BONUS, BET_CATEGORY_INSURANCE_BET, BET_CATEGORY_STANDARD,
    BET_CATEGORY_ODDS_BOOST,
    RESULT_WIN, RESULT_LOSS, RESULT_PUSH, RESULT_VOID, RESULT_CASHOUT,
    PROMO_STATUS_REALIZED, PROMO_STATUS_UNUSED, PROMO_TYPE_PROFIT_BOOST_DAILY,
    PROMO_TYPE_BONUS_BET, PROMO_TYPE_PROFIT_BOOST,
    PROMO_TYPE_DEPOSIT_BONUS, PROMO_TYPE_INSURANCE_BET, PROMO_TYPE_ODDS_BOOST,
    REWARD_TIMING_PER_QUALIFYING_BET, REWARD_TIMING_GRANTED,
    PAYOUT_ROUND_NEAREST_BOOKS,
)
from resolver import calculate_pl_and_payout

# A bet's Result column is genuinely "settled" only when it holds one of
# these four values. NEEDS_REVIEW and PENDING are both non-blank but are
# NOT final outcomes -- a reward/leg-2 bet sitting at either of those must
# still be treated as "waiting," not "settled with $0." (This was a real
# bug in an earlier version of this file's Bonus Bet logic, caught while
# wiring up Profit Boost -- fixed here for all four types at once.)
_FINAL_RESULTS = {RESULT_WIN, RESULT_LOSS, RESULT_PUSH, RESULT_VOID, RESULT_CASHOUT}


def _is_final(result: str) -> bool:
    return result in _FINAL_RESULTS


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


def _safe_float(s: str, default: float = 0.0) -> float:
    try:
        return float(s) if s else default
    except (ValueError, TypeError):
        return default


# ════════════════════════════════════════════════════════════════════
# ── Bonus Bet / Profit Boost: shared multi-grant token model ────────
# ════════════════════════════════════════════════════════════════════

def _evaluate_multi_grant_promo(promo: dict, linked_bets: list[dict], today: date,
                                 reward_category: str, compute_token_value) -> dict:
    """
    Shared core for any promo type with Bonus Bet's multi-grant token
    model -- currently Bonus Bet and Profit Boost, confirmed during
    design to share identical timing/expiration/FIFO-matching rules,
    differing only in how a claimed token's dollar value is computed.

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
                          guaranteed to be one of the 4 final values) --
                          returns that claimed token's dollar
                          contribution to Realized Amount.

    Returns a verdict dict:
        {
          "qualifying_cost_fill": float | None,  # write if not None
          "finalize": {"status": ..., "realized_amount": ...} | None,
          "log": [str, ...]  # human-readable trace, for promo_trigger.py
        }
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

    try:
        expected_count = int(promo["expected_reward_count"]) if promo["expected_reward_count"] else 1
    except ValueError:
        expected_count = 1
    expected_count = max(expected_count, 1)

    reward_timing = promo["reward_timing"]
    is_granted = reward_timing == REWARD_TIMING_GRANTED

    if is_granted:
        earned_count = expected_count
        window_closed = True
        counted_qualifiers = []
        qc_fill = None
        log.append(f"Granted promo -- {earned_count} token(s) available without qualifying bet(s).")
    else:
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

        earned_count = min(len(counted_qualifiers), expected_count)

        # ── Has the qualifying window genuinely closed? ─────────────────────
        window_closed = (expiration is not None and today > expiration) or (earned_count >= expected_count)

        if not window_closed:
            log.append(f"Qualifying window still open ({len(counted_qualifiers)}/{expected_count} "
                        f"qualifier(s) so far, expiration {expiration or 'none set'}) -- leaving Pending.")
            return {"qualifying_cost_fill": None, "finalize": None, "log": log}

        qc_fill = None
        if not promo["qualifying_cost"]:
            qc_fill = round(sum(
                _safe_float(b["stake"]) + _safe_float(b["fee"]) for b in counted_qualifiers
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

    token_deadlines = []
    for i in range(earned_count):
        if is_granted:
            # Expiration is the hard "use by" date for granted tokens.
            if expiration is not None:
                token_deadlines.append(expiration)
            else:
                token_deadlines.append(None)
        elif reward_timing == REWARD_TIMING_PER_QUALIFYING_BET and i < len(counted_qualifiers):
            anchor = _parse_date(counted_qualifiers[i]["date_placed"])
            if anchor is not None and usage_days is not None:
                token_deadlines.append(anchor + timedelta(days=usage_days))
            else:
                token_deadlines.append(None)
        else:
            anchor = expiration or (
                _parse_date(counted_qualifiers[0]["date_placed"]) if counted_qualifiers else None
            )
            if anchor is not None and usage_days is not None:
                token_deadlines.append(anchor + timedelta(days=usage_days))
            else:
                token_deadlines.append(None)

    # ── FIFO-match claimed reward bets to earned tokens, oldest first ───
    token_values = []
    blocked = False
    for i in range(earned_count):
        if i < len(reward_bets):
            rb = reward_bets[i]
            if not _is_final(rb["result"]):
                status_desc = rb["result"] or "blank"
                log.append(f"Token {i + 1}/{earned_count}: claimed (BetID {rb['bet_id']}) "
                           f"but not yet settled (Result='{status_desc}') -- waiting.")
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
    win for this category, $0 on a loss. No derivation needed.
    """
    def token_value(reward_bet: dict) -> float:
        return _safe_float(reward_bet["pl"])

    return _evaluate_multi_grant_promo(promo, linked_bets, today, BET_CATEGORY_BONUS_BET, token_value)


def evaluate_profit_boost_promo(promo: dict, linked_bets: list[dict], today: date,
                                 fee_before_odds_lookup: dict) -> dict:
    """
    Profit Boost's token-value function: the BOOST's value, not the
    bet's full P/L (confirmed during design -- the stake itself is real
    money you'd have wagered anyway; only the extra profit the boost
    added is the promo's contribution).

    Computed by reusing resolver.calculate_pl_and_payout() -- the single
    source of truth for this fee/boost math -- to work out what the SAME
    bet's P/L would have been WITHOUT the boost (passing bet_category as
    Standard instead of Profit Boost, boost_pct omitted). The difference
    between that hypothetical and the bet's real stored P/L is the
    boost's actual contribution. This naturally comes out to exactly
    $0 for a loss/push/void with no special-casing needed, since boost
    only ever affects WIN math in calculate_pl_and_payout() -- confirmed
    during design ("Yes, correct" re: boost having no effect on
    loss/push/void).

    fee_before_odds_lookup: {book: bool}, since the hypothetical
    recomputation needs to use the SAME fee mechanic the real bet used
    (only the boost itself should differ between the two calculations).
    Built by promo_trigger.py via sheets_reader.get_book_fee_before_odds()
    -- a live Sheets lookup, which is why it's passed in as plain data
    rather than looked up from inside this (intentionally I/O-free) file.
    """
    def token_value(reward_bet: dict) -> float:
        actual_pl = _safe_float(reward_bet["pl"])
        stake = _safe_float(reward_bet["stake"])
        odds_taken = _safe_float(reward_bet["odds_taken"])
        fee = _safe_float(reward_bet["fee"])
        fee_before_odds = fee_before_odds_lookup.get(reward_bet["book"], False)

        unboosted_pl, _ = calculate_pl_and_payout(
            reward_bet["result"], stake, odds_taken,
            bet_category=BET_CATEGORY_STANDARD, fee=fee, fee_before_odds=fee_before_odds,
            round_to_nearest=(reward_bet["book"] or "").strip().lower() in PAYOUT_ROUND_NEAREST_BOOKS
        )
        return round(actual_pl - unboosted_pl, 2)

    return _evaluate_multi_grant_promo(promo, linked_bets, today, BET_CATEGORY_PROFIT_BOOST, token_value)


# ════════════════════════════════════════════════════════════════════
# ── Deposit Bonus ─────────────────────────────────────────────────────
# ════════════════════════════════════════════════════════════════════

def evaluate_deposit_bonus_promo(promo: dict, linked_bets: list[dict], today: date) -> dict:
    """
    Deposit Bonus model (confirmed during design, kept deliberately
    looser than Bonus Bet/Profit Boost per the user's own request --
    "I have less knowledge about how deposit bonuses... could work"):

      - No Qualifying Bet involved at all -- the deposit itself is the
        trigger, not a bet. Qualifying Cost backfill from linked
        Qualifying Bet rows (the multi-grant promos' mechanism) does
        NOT apply here.
      - The bonus money has a playthrough requirement before it's real,
        withdrawable cash -- recorded as a SINGLE Bets row, Bet Category
        = "Deposit Bonus", same Promo ID. That one row is BOTH the
        playthrough and the payout event.
      - Deposit Bonus is in config.PROMO_FUNDED_CATEGORIES, so
        resolver.calculate_pl_and_payout() already computes this bet's
        P/L correctly on its own (win = stake+profit becomes real money;
        loss = $0, the bonus money evaporates) -- that stored P/L IS the
        Realized Amount directly, no derivation needed.
      - No expiration confirmed for this type -- stays Pending
        indefinitely until a matching bet shows up and settles.
      - Multi-bet handling (more than one Deposit Bonus-category row
        linked to one promo) is explicitly UNCONFIRMED -- only one real
        case existed during design, and it needed just one bet. This
        function takes the earliest such row if more than one exists,
        logs a loud warning, and flags it for manual review rather than
        guessing at how multiple should combine.
    """
    log = []
    deposit_bets = sorted(
        [b for b in linked_bets if b["bet_category"] == BET_CATEGORY_DEPOSIT_BONUS],
        key=lambda b: _parse_date(b["date_placed"]) or date.min
    )

    if not deposit_bets:
        log.append("No Deposit Bonus-category bet linked yet. This promo type has no "
                    "expiration in the current model -- leaving Pending indefinitely "
                    "until one shows up.")
        return {"qualifying_cost_fill": None, "finalize": None, "log": log}

    if len(deposit_bets) > 1:
        log.append(f"⚠️  {len(deposit_bets)} Deposit Bonus-category bets are linked to "
                    f"this promo. Multi-bet handling was never confirmed during design "
                    f"(only one real case existed) -- using the earliest one and "
                    f"ignoring the rest. Recommend manual review.")

    bet = deposit_bets[0]
    if not _is_final(bet["result"]):
        status_desc = bet["result"] or "blank"
        log.append(f"Deposit Bonus bet (BetID {bet['bet_id']}) placed but not yet settled "
                   f"(Result='{status_desc}') -- waiting.")
        return {"qualifying_cost_fill": None, "finalize": None, "log": log}

    pl = _safe_float(bet["pl"])
    log.append(f"Deposit Bonus bet (BetID {bet['bet_id']}) settled as {bet['result']} -- "
               f"its own P/L ({pl}) is already the correct Realized Amount "
               f"(promo-funded category: win=stake+profit, loss=$0).")
    return {
        "qualifying_cost_fill": None,
        "finalize": {"status": PROMO_STATUS_REALIZED, "realized_amount": pl},
        "log": log,
    }


# ════════════════════════════════════════════════════════════════════
# ── Odds Boost ────────────────────────────────────────────────────────
# ════════════════════════════════════════════════════════════════════

def evaluate_odds_boost_promo(promo: dict, linked_bets: list[dict], today: date,
                               fee_before_odds_lookup: dict) -> dict:
    """
    Odds Boost: a single boosted line, used once. The linked reward bet is
    logged at the BOOSTED odds with Bet Category = "Odds Boost", so its own
    stored P/L already reflects the boosted result (real-money math, no boost %
    -- the odds themselves are the boost). The promo's value is the EXTRA that
    the boost bought versus the original line:

        Realized Amount = actual P/L (at boosted odds)
                          - P/L the same stake would have returned at the
                            promo's Original Odds

    computed by reusing resolver.calculate_pl_and_payout() as a Standard bet at
    the original odds (same fee mechanic), then differencing -- the identical
    pattern to evaluate_profit_boost_promo, just with an odds baseline instead
    of a boost-% baseline. It comes out to exactly $0 on a loss/push/void, since
    both sides return the same when the bet didn't win.

    Single-use: expects one linked Odds Boost bet. If none is placed and the
    promo has expired, it finalizes as Unused ($0); otherwise it waits.

    fee_before_odds_lookup: {book: bool}, so the original-odds baseline uses the
    SAME fee mechanic the real bet used (built by promo_trigger.py).
    """
    log = []
    boost_bets = sorted(
        [b for b in linked_bets if b["bet_category"] == BET_CATEGORY_ODDS_BOOST],
        key=lambda b: _parse_date(b["date_placed"]) or date.min
    )

    if not boost_bets:
        expiry = _parse_date(promo.get("expiration_date", ""))
        if expiry is not None and today > expiry:
            log.append(f"No Odds Boost bet was ever placed and the promo expired "
                       f"{expiry} -- finalizing as Unused ($0).")
            return {"qualifying_cost_fill": None,
                    "finalize": {"status": PROMO_STATUS_UNUSED, "realized_amount": 0.0},
                    "log": log}
        log.append("No Odds Boost-category bet linked yet -- waiting (Pending).")
        return {"qualifying_cost_fill": None, "finalize": None, "log": log}

    if len(boost_bets) > 1:
        ignored = ", ".join(str(b["bet_id"]) for b in boost_bets[1:])
        log.append(f"⚠️  {len(boost_bets)} Odds Boost bets are linked, but this is a "
                   f"single-line boost -- using the earliest (BetID {boost_bets[0]['bet_id']}) "
                   f"and ignoring BetID {ignored}. Recommend manual review.")

    bet = boost_bets[0]
    if not _is_final(bet["result"]):
        log.append(f"Odds Boost bet (BetID {bet['bet_id']}) placed but not yet settled "
                   f"(Result='{bet['result'] or 'blank'}') -- waiting.")
        return {"qualifying_cost_fill": None, "finalize": None, "log": log}

    original_odds_raw = (promo.get("original_odds") or "").strip()
    try:
        original_odds = float(original_odds_raw.replace("+", "")) if original_odds_raw else None
    except (ValueError, TypeError):
        original_odds = None
    if original_odds is None:
        log.append(f"Odds Boost bet (BetID {bet['bet_id']}) settled {bet['result']}, but the "
                   f"promo's Original Odds field is blank/unparseable ('{original_odds_raw}') -- "
                   f"can't measure the boost's value. Fill in Original Odds; leaving Pending.")
        return {"qualifying_cost_fill": None, "finalize": None, "log": log}

    actual_pl = _safe_float(bet["pl"])
    stake = _safe_float(bet["stake"])
    fee = _safe_float(bet["fee"])
    fee_before_odds = (fee_before_odds_lookup or {}).get(bet["book"], False)

    baseline_pl, _ = calculate_pl_and_payout(
        bet["result"], stake, original_odds,
        bet_category=BET_CATEGORY_STANDARD, fee=fee, fee_before_odds=fee_before_odds,
        round_to_nearest=(bet["book"] or "").strip().lower() in PAYOUT_ROUND_NEAREST_BOOKS
    )
    realized = round(actual_pl - baseline_pl, 2)
    log.append(f"Odds Boost bet (BetID {bet['bet_id']}) settled {bet['result']}. Boosted P/L "
               f"{actual_pl} vs {baseline_pl} at original odds {original_odds_raw} -> boost value "
               f"{realized}. Finalizing as Realized.")
    return {"qualifying_cost_fill": None,
            "finalize": {"status": PROMO_STATUS_REALIZED, "realized_amount": realized},
            "log": log}


# ════════════════════════════════════════════════════════════════════
# ── Insurance Bet ──────────────────────────────────────────────────────
# ════════════════════════════════════════════════════════════════════

def evaluate_insurance_bet_promo(promo: dict, linked_bets: list[dict], today: date) -> dict:
    """
    Dispatches to the single-day or multi-day Insurance Bet evaluator,
    based on Expected Reward Count (added 2026-06-23 to support promos
    like "10 insurance bets of $100, one per day for 10 days" -- a
    different shape than the original single "first bet" model below).

    Expected Reward Count blank or 1 -> single-day (original model,
    unchanged). Expected Reward Count > 1 -> multi-day, which REQUIRES
    Start Date to be set (otherwise which calendar days are covered is
    ambiguous) -- left Pending with a clear log line if it's missing,
    rather than guessing or silently falling back to single-day (which
    would silently ignore every day after the first).
    """
    try:
        expected_count = int(promo["expected_reward_count"]) if promo.get("expected_reward_count") else 1
    except (ValueError, TypeError):
        expected_count = 1
    expected_count = max(expected_count, 1)

    if expected_count <= 1:
        return _evaluate_single_day_insurance(promo, linked_bets, today)

    start_date = _parse_date(promo.get("start_date", ""))
    if start_date is None:
        return {
            "qualifying_cost_fill": None,
            "finalize": None,
            "log": [f"Expected Reward Count is {expected_count} (multi-day Insurance Bet) "
                    f"but Start Date is not set -- cannot determine which calendar days "
                    f"this promo covers. Set Start Date on this promo to continue. "
                    f"Leaving Pending."],
        }

    return _evaluate_multi_day_insurance(promo, linked_bets, today, start_date, expected_count)


def _evaluate_single_day_insurance(promo: dict, linked_bets: list[dict], today: date) -> dict:
    """
    Insurance Bet's original two-leg model (confirmed during design, cross-
    checked against real BetMGM and bet365 promo terms):

      Leg 1 -- the insured bet itself, Bet Category = "Insurance Bet",
      same Promo ID. Only a LOSS triggers a refund (confirmed against
      both real promos' terms: a void/push/no-action CLOSES the promo
      with no refund, same as a win). This system has no concept of a
      partial-return bet (no each-way/place market exists in
      config.BET_TYPE_*), so unlike bet365's partial-loss example, a
      loss here is always treated as a TOTAL loss -- refund = the full
      stake, capped at the promo's Bonus Amount field ("up to $X back").

      Leg 2 -- ONLY exists if Leg 1 triggered a refund. The refund is
      Bonus Bet credit (logged as Bet Category = "Bonus Bet", same
      Promo ID) -- structurally identical to a single-grant Bonus Bet
      token. Realized Amount = Leg 2's own settled P/L (Leg 1's win/loss
      P/L is an ordinary bet outcome you'd have had regardless of the
      promo, and is deliberately NOT counted here).

    KNOWN GAP (accepted by the user 2026-06-21, not a bug): if Leg 1
    loses and NO Leg 2 row is ever linked, this function cannot
    determine whether the refund token's usage deadline has passed --
    that would require knowing WHEN Leg 1 settled, and the Bets tab has
    no "Result Date" column (only Date Placed). Rather than approximate
    using Game Date as a stand-in for actual settlement time, this case
    is left Pending indefinitely with a clear log line; the user chose
    manual close-out over adding a new column for this single sub-case.
    """
    log = []
    insured_bets = sorted(
        [b for b in linked_bets if b["bet_category"] == BET_CATEGORY_INSURANCE_BET],
        key=lambda b: _parse_date(b["date_placed"]) or date.min
    )

    if not insured_bets:
        log.append("No Insurance Bet-category bet linked yet -- leaving Pending.")
        return {"qualifying_cost_fill": None, "finalize": None, "log": log}

    if len(insured_bets) > 1:
        log.append(f"⚠️  {len(insured_bets)} Insurance Bet-category bets are linked to "
                    f"this promo -- by definition this should be a single 'first bet.' "
                    f"Using the earliest one and ignoring the rest. Recommend manual review.")

    leg1 = insured_bets[0]
    if not _is_final(leg1["result"]):
        status_desc = leg1["result"] or "blank"
        log.append(f"Leg 1 (the insured bet, BetID {leg1['bet_id']}) not yet settled "
                   f"(Result='{status_desc}') -- waiting.")
        return {"qualifying_cost_fill": None, "finalize": None, "log": log}

    if leg1["result"] != RESULT_LOSS:
        log.append(f"Leg 1 settled as {leg1['result']}, not a loss -- per confirmed promo "
                    f"terms, only a loss triggers a refund (win/push/void close the promo "
                    f"with no refund). Finalizing as Realized, $0.")
        return {
            "qualifying_cost_fill": None,
            "finalize": {"status": PROMO_STATUS_REALIZED, "realized_amount": 0.0},
            "log": log,
        }

    stake = _safe_float(leg1["stake"])
    # Bonus Amount is stored as a currency string (e.g. "$100.00") -- strip
    # the formatting before parsing, same convention used everywhere else
    # a money field is read (e.g. poller.py's Fee parsing). _safe_float()
    # alone isn't enough here: it swallows a ValueError into its default
    # (0.0), which would silently cap every refund at $0 instead of failing
    # loudly -- confirmed 2026-06-27, this exact bug crashed the run instead
    # since the raw float() call raised first.
    bonus_amount_raw = (promo["bonus_amount"] or "").replace("$", "").replace(",", "").strip()
    cap = float(bonus_amount_raw) if bonus_amount_raw else None
    refund = min(stake, cap) if cap is not None else stake

    log.append(f"Leg 1 (BetID {leg1['bet_id']}) lost -- refund of {refund} triggered as "
               f"Bonus Bet credit (Leg 2).")

    reward_bets = sorted(
        [b for b in linked_bets if b["bet_category"] == BET_CATEGORY_BONUS_BET],
        key=lambda b: _parse_date(b["date_placed"]) or date.min
    )

    if not reward_bets:
        log.append("No Leg 2 (Bonus Bet-category) row claiming the refund yet. This "
                    "system has no 'Result Date' column, so the refund token's usage "
                    "deadline can't be computed honestly -- per your decision "
                    "(2026-06-21), this is left for manual close-out rather than "
                    "guessed at. Leaving Pending.")
        return {"qualifying_cost_fill": None, "finalize": None, "log": log}

    if len(reward_bets) > 1:
        log.append(f"⚠️  {len(reward_bets)} Bonus Bet-category rows are linked -- only "
                    f"one refund token should exist per Insurance Bet promo. Using the "
                    f"earliest one and ignoring the rest. Recommend manual review.")

    leg2 = reward_bets[0]
    if not _is_final(leg2["result"]):
        status_desc = leg2["result"] or "blank"
        log.append(f"Leg 2 (BetID {leg2['bet_id']}) claimed but not yet settled "
                   f"(Result='{status_desc}') -- waiting.")
        return {"qualifying_cost_fill": None, "finalize": None, "log": log}

    leg2_pl = _safe_float(leg2["pl"])
    log.append(f"Leg 2 (BetID {leg2['bet_id']}) settled -- its P/L ({leg2_pl}) is the "
               f"promo's Realized Amount (Leg 1's own bet outcome is not counted, per design).")
    return {
        "qualifying_cost_fill": None,
        "finalize": {"status": PROMO_STATUS_REALIZED, "realized_amount": leg2_pl},
        "log": log,
    }


def _evaluate_multi_day_insurance(promo: dict, linked_bets: list[dict], today: date,
                                   start_date: date, num_days: int) -> dict:
    """
    Multi-day Insurance Bet (added 2026-06-23): N independent daily slots,
    each its own Leg 1/Leg 2 pair -- day i covers calendar date
    start_date + (i-1). This is NOT the Bonus Bet/Profit Boost multi-grant
    model (no FIFO pool of "any qualifying bet within a window") -- each
    day is its own use-it-or-lose-it opportunity, confirmed against the
    user's actual promo terms ("the first bet I place, up to $100, each
    day for 10 days").

    Per day:
      - No Insurance Bet-category bet placed that day, and the day has
        already elapsed -> contributes $0 (day's slot went unused).
      - More than one Insurance Bet-category bet placed that day -> takes
        the earliest as that day's Leg 1, logs the rest as ignored (same
        rule as the single-day model, confirmed by the user 2026-06-23
        rather than blocking the whole promo for manual review).
      - Leg 1 not yet settled -> blocks the WHOLE promo as Pending (can't
        determine this day's contribution yet, and days are evaluated in
        order since a later day's Leg 2 claim can't be FIFO-matched
        correctly while an earlier day is still unresolved).
      - Leg 1 settled WIN/PUSH/VOID -> contributes $0 (matches the
        single-day model's confirmed terms: only a loss triggers a
        refund).
      - Leg 1 LOSS -> needs a Leg 2 (Bonus Bet-category) row to claim the
        refund, FIFO-matched against every day's loss in chronological
        order (mirrors how Bonus Bet/Profit Boost FIFO-match reward bets
        to earned tokens). Same KNOWN GAP as the single-day model applies
        per-day: if a day loses and no Leg 2 is linked yet, this is left
        Pending rather than guessing at a deadline (no Result Date column
        exists to compute one).

    Extra Bonus Bet-category rows beyond what any day's losses need are
    logged as a warning (shouldn't happen, but Sheets data can always be
    hand-edited).
    """
    log = []
    # Same currency-string-stripping fix as _evaluate_single_day_insurance --
    # see that function's comment for why _safe_float() alone isn't safe here.
    bonus_amount_raw = (promo.get("bonus_amount") or "").replace("$", "").replace(",", "").strip()
    cap = float(bonus_amount_raw) if bonus_amount_raw else None

    insured_bets = sorted(
        [b for b in linked_bets if b["bet_category"] == BET_CATEGORY_INSURANCE_BET],
        key=lambda b: _parse_date(b["date_placed"]) or date.min
    )
    by_day: dict[date, list[dict]] = {}
    for b in insured_bets:
        d = _parse_date(b["date_placed"])
        if d is not None:
            by_day.setdefault(d, []).append(b)

    reward_bets = sorted(
        [b for b in linked_bets if b["bet_category"] == BET_CATEGORY_BONUS_BET],
        key=lambda b: _parse_date(b["date_placed"]) or date.min
    )

    values = []
    reward_idx = 0

    for i in range(num_days):
        day = start_date + timedelta(days=i)
        day_label = f"Day {i + 1}/{num_days} ({day})"
        bets_that_day = by_day.get(day, [])

        if len(bets_that_day) > 1:
            ignored_ids = ", ".join(str(b["bet_id"]) for b in bets_that_day[1:])
            log.append(f"{day_label}: {len(bets_that_day)} Insurance Bet-category bets "
                        f"placed this day -- using earliest (BetID {bets_that_day[0]['bet_id']}), "
                        f"ignoring the rest (BetID {ignored_ids}).")

        leg1 = bets_that_day[0] if bets_that_day else None

        if leg1 is None:
            if today > day:
                log.append(f"{day_label}: no Insurance Bet placed and the day has passed -- "
                            f"slot unused, contributes $0.")
                values.append(0.0)
                continue
            log.append(f"{day_label}: no Insurance Bet placed yet, day hasn't passed -- waiting.")
            return {"qualifying_cost_fill": None, "finalize": None, "log": log}

        if not _is_final(leg1["result"]):
            status_desc = leg1["result"] or "blank"
            log.append(f"{day_label}: Leg 1 (BetID {leg1['bet_id']}) not yet settled "
                        f"(Result='{status_desc}') -- waiting.")
            return {"qualifying_cost_fill": None, "finalize": None, "log": log}

        if leg1["result"] != RESULT_LOSS:
            log.append(f"{day_label}: Leg 1 (BetID {leg1['bet_id']}) settled as "
                        f"{leg1['result']}, not a loss -- contributes $0.")
            values.append(0.0)
            continue

        stake = _safe_float(leg1["stake"])
        refund_cap = min(stake, cap) if cap is not None else stake

        if reward_idx >= len(reward_bets):
            log.append(f"{day_label}: Leg 1 (BetID {leg1['bet_id']}) lost, refund up to "
                        f"{refund_cap} expected -- no Leg 2 (Bonus Bet) row linked yet. "
                        f"No Result Date column exists to compute a deadline -- per the "
                        f"accepted limitation for this promo type, leaving Pending until "
                        f"a Leg 2 row appears.")
            return {"qualifying_cost_fill": None, "finalize": None, "log": log}

        leg2 = reward_bets[reward_idx]
        reward_idx += 1

        if not _is_final(leg2["result"]):
            status_desc = leg2["result"] or "blank"
            log.append(f"{day_label}: Leg 1 (BetID {leg1['bet_id']}) lost -- Leg 2 "
                        f"(BetID {leg2['bet_id']}) claimed but not yet settled "
                        f"(Result='{status_desc}') -- waiting.")
            return {"qualifying_cost_fill": None, "finalize": None, "log": log}

        leg2_pl = _safe_float(leg2["pl"])
        log.append(f"{day_label}: Leg 1 (BetID {leg1['bet_id']}) lost -- Leg 2 "
                    f"(BetID {leg2['bet_id']}) settled, value={leg2_pl}.")
        values.append(leg2_pl)

    if reward_idx < len(reward_bets):
        extra = len(reward_bets) - reward_idx
        log.append(f"⚠️  {extra} extra Bonus Bet-category row(s) linked beyond what any "
                    f"day's losses needed -- ignored. Recommend manual review.")

    realized_amount = round(sum(values), 2)
    log.append(f"All {num_days} day(s) have a final disposition -- finalizing as Realized, "
               f"Realized Amount={realized_amount}.")
    return {
        "qualifying_cost_fill": None,
        "finalize": {"status": PROMO_STATUS_REALIZED, "realized_amount": realized_amount},
        "log": log,
    }


def evaluate_profit_boost_daily_promo(promo: dict, linked_bets: list[dict], today: date,
                                        start_date: date, fee_before_odds_lookup: dict) -> dict:
    """
    "Profit Boost (Daily Until Win)": one boosted bet per day starting
    start_date, reissued automatically by the book on every loss, with NO
    fixed end date or count -- it just keeps going until the first win
    closes it out. Structurally this is a day-walker like
    _evaluate_multi_day_insurance (no FIFO token pool, the boosted bet
    itself IS the day's bet), but two things are deliberately different:

      - No num_days bound -- the loop runs until a win is found. A
        SAFETY_CAP_DAYS guard exists purely to avoid an infinite loop on
        bad data (e.g. a Start Date typo'd decades in the past); hitting
        it is treated as a data problem, logged, and left Pending rather
        than silently finalizing.
      - A day with no bet placed and the day has passed contributes $0
        and the promo CONTINUES (this is expected -- the user just
        hasn't placed that day's reissued boost yet), it does NOT close
        out as Unused the way a single bounded opportunity would.

    Per day:
      - No Profit Boost-category bet placed yet, day hasn't passed ->
        whole promo waits (Pending) -- this is the "today, still open" day.
      - No Profit Boost-category bet placed, day has passed -> $0,
        continue to next day (the user skipped boosting that day).
      - More than one Profit Boost-category bet placed that day -> takes
        the earliest, logs the rest as ignored (same rule as Insurance's
        day-walker).
      - Bet not yet settled -> blocks the WHOLE promo as Pending.
      - Bet settled WIN -> this is the terminating day. Boost value is
        derived the same way evaluate_profit_boost_promo's token_value()
        does: actual P/L minus what the SAME bet's P/L would have been
        unboosted (via resolver.calculate_pl_and_payout with
        bet_category=BET_CATEGORY_STANDARD). Finalizes as Realized with
        the SUM of every day's contribution (every losing day is $0, so
        this reduces to just the winning day's boost value, but summing
        keeps the shape consistent and correct even if a loss day's
        unboosted recompute were ever non-zero for some future Bet
        Category quirk).
      - Bet settled LOSS/PUSH/VOID -> contributes $0, continue.
    """
    SAFETY_CAP_DAYS = 365
    log = []

    boost_bets = sorted(
        [b for b in linked_bets if b["bet_category"] == BET_CATEGORY_PROFIT_BOOST],
        key=lambda b: _parse_date(b["date_placed"]) or date.min
    )
    by_day: dict[date, list[dict]] = {}
    for b in boost_bets:
        d = _parse_date(b["date_placed"])
        if d is not None:
            by_day.setdefault(d, []).append(b)

    def token_value(reward_bet: dict) -> float:
        actual_pl = _safe_float(reward_bet["pl"])
        stake = _safe_float(reward_bet["stake"])
        odds_taken = _safe_float(reward_bet["odds_taken"])
        fee = _safe_float(reward_bet["fee"])
        fee_before_odds = fee_before_odds_lookup.get(reward_bet["book"], False)

        unboosted_pl, _ = calculate_pl_and_payout(
            reward_bet["result"], stake, odds_taken,
            bet_category=BET_CATEGORY_STANDARD, fee=fee, fee_before_odds=fee_before_odds,
            round_to_nearest=(reward_bet["book"] or "").strip().lower() in PAYOUT_ROUND_NEAREST_BOOKS
        )
        return round(actual_pl - unboosted_pl, 2)

    values = []

    for i in range(SAFETY_CAP_DAYS):
        day = start_date + timedelta(days=i)
        day_label = f"Day {i + 1} ({day})"
        bets_that_day = by_day.get(day, [])

        if len(bets_that_day) > 1:
            ignored_ids = ", ".join(str(b["bet_id"]) for b in bets_that_day[1:])
            log.append(f"{day_label}: {len(bets_that_day)} Profit Boost-category bets "
                        f"placed this day -- using earliest (BetID {bets_that_day[0]['bet_id']}), "
                        f"ignoring the rest (BetID {ignored_ids}).")

        bet = bets_that_day[0] if bets_that_day else None

        if bet is None:
            if today > day:
                log.append(f"{day_label}: no Profit Boost bet placed and the day has "
                            f"passed -- contributes $0, promo continues to the next day.")
                values.append(0.0)
                continue
            log.append(f"{day_label}: no Profit Boost bet placed yet, day hasn't passed "
                        f"-- waiting.")
            return {"qualifying_cost_fill": None, "finalize": None, "log": log}

        if not _is_final(bet["result"]):
            status_desc = bet["result"] or "blank"
            log.append(f"{day_label}: bet (BetID {bet['bet_id']}) not yet settled "
                        f"(Result='{status_desc}') -- waiting.")
            return {"qualifying_cost_fill": None, "finalize": None, "log": log}

        if bet["result"] == RESULT_LOSS:
            log.append(f"{day_label}: bet (BetID {bet['bet_id']}) lost -- contributes $0, "
                        f"the book reissues the boost the next day, continuing.")
            values.append(0.0)
            continue

        if bet["result"] != RESULT_WIN:
            log.append(f"{day_label}: bet (BetID {bet['bet_id']}) settled as "
                        f"{bet['result']} (push/void) -- contributes $0, continuing.")
            values.append(0.0)
            continue

        value = token_value(bet)
        values.append(value)
        log.append(f"{day_label}: bet (BetID {bet['bet_id']}) WON -- boost value "
                    f"(actual P/L minus unboosted P/L) = {value}. This is the first win, "
                    f"closing out the promo.")
        realized_amount = round(sum(values), 2)
        return {
            "qualifying_cost_fill": None,
            "finalize": {"status": PROMO_STATUS_REALIZED, "realized_amount": realized_amount},
            "log": log,
        }

    log.append(f"⚠️  Reached the {SAFETY_CAP_DAYS}-day safety cap (Start Date={start_date}) "
                f"without a win -- this almost certainly means Start Date is wrong. "
                f"Leaving Pending for manual review rather than guessing.")
    return {"qualifying_cost_fill": None, "finalize": None, "log": log}


# ════════════════════════════════════════════════════════════════════
# ── Dispatcher ─────────────────────────────────────────────────────────
# ════════════════════════════════════════════════════════════════════

def evaluate_promo(promo: dict, linked_bets: list[dict], today: date,
                    fee_before_odds_lookup: dict | None = None) -> dict:
    """
    Dispatches a Pending promo to its type-specific evaluator. All four
    Promo Types are implemented as of 2026-06-21:
      - Bonus Bet, Profit Boost: fully automated
      - Deposit Bonus: automated for the (so-far only confirmed)
        single-bet case
      - Insurance Bet: automated except the unclaimed-refund-token
        forfeiture sub-case (accepted gap, see
        evaluate_insurance_bet_promo's docstring)

    fee_before_odds_lookup is only consulted for Profit Boost and Profit
    Boost (Daily Until Win); pass {} or None for any other type.

    An unrecognised Promo Type (shouldn't happen given the wizard's
    fixed PROMO_TYPES list, but Sheets data can always be hand-edited)
    returns a "not_implemented" verdict rather than raising, so
    promo_trigger.py can log and move on without crashing the run.
    """
    promo_type = promo["promo_type"]

    if promo_type == PROMO_TYPE_BONUS_BET:
        return evaluate_bonus_bet_promo(promo, linked_bets, today)

    if promo_type == PROMO_TYPE_PROFIT_BOOST:
        return evaluate_profit_boost_promo(promo, linked_bets, today, fee_before_odds_lookup or {})

    if promo_type == PROMO_TYPE_DEPOSIT_BONUS:
        return evaluate_deposit_bonus_promo(promo, linked_bets, today)

    if promo_type == PROMO_TYPE_INSURANCE_BET:
        return evaluate_insurance_bet_promo(promo, linked_bets, today)

    if promo_type == PROMO_TYPE_ODDS_BOOST:
        return evaluate_odds_boost_promo(promo, linked_bets, today, fee_before_odds_lookup or {})

    if promo_type == PROMO_TYPE_PROFIT_BOOST_DAILY:
        start_date = _parse_date(promo.get("start_date", ""))
        if start_date is None:
            return {
                "qualifying_cost_fill": None,
                "finalize": None,
                "log": [f"Profit Boost (Daily Until Win) requires Start Date to know "
                        f"which calendar day the daily boost cycle began -- not set. "
                        f"Leaving Pending."],
            }
        return evaluate_profit_boost_daily_promo(promo, linked_bets, today, start_date,
                                                    fee_before_odds_lookup or {})

    return {
        "qualifying_cost_fill": None,
        "finalize": None,
        "log": [f"Unrecognised Promo Type '{promo_type}' -- skipping."],
        "not_implemented": True,
    }
