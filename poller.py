from datetime import datetime, timezone
import re
import pytz
from config import (
    POLL_START_BUFFER_SECONDS,
    POLL_MAX_DURATION_SECONDS,
    RESULT_NEEDS_REVIEW,
    RESULT_WIN,
    RESULT_LOSS,
    BET_CATEGORY_PROFIT_BOOST,
    BET_CATEGORY_BONUS_BET,
    BOOK_FEE_TYPE_PERCENT_OF_WIN_PROFIT,
    BOOK_FEE_TYPE_PERCENT_OF_STAKE_ON_WIN,
    MANUAL_PAYOUT_REQUIRED_BOOKS,
    PAYOUT_ROUND_NEAREST_BOOKS,
    BET_TYPE_PARLAY,
    BET_TYPE_MONEYLINE,
)
from sources import odds_api, espn_tennis
from resolver import (
    resolve, resolve_parlay, calculate_pl_and_payout, derive_pl_from_payout,
    _american_odds_profit,
)
from sheets_writer import (
    write_result, write_pl_payout, write_pl_only,
    flag_pl_blocked, clear_pl_blocked_flag, flag_payout_caution,
)
from sheets_reader import (
    get_promo_boost_percentage, get_book_fee_before_odds, get_book_fee_config,
    load_unresolved_pl_bets, load_manual_payout_pending_pl_bets,
)

CENTRAL = pytz.timezone("America/Chicago")


def poll_bet(bet: dict) -> bool:
    """
    Checks a single bet for a final result ONCE, then returns -- does not
    sleep, loop, or retry internally.

    This is a deliberate architectural change (2026-06-20): this script runs
    as a GitHub Actions workflow, triggered via workflow_dispatch every 30
    minutes (POLL_INTERVAL_SECONDS) by an external cron-job.org schedule
    calling GitHub's REST API. The original design had this function sleep
    internally between retries -- which meant any bet without an immediate
    result would reliably blow through the runner's timeout and get killed
    mid-poll, while a NEW triggered run started in parallel every 30 minutes
    regardless, re-reading the same pending bets from scratch. That's not a
    fixable timeout-tuning problem; it's two retry mechanisms (an internal
    sleep loop and an external 30-min schedule) fighting each other. The fix
    is to have exactly one retry mechanism -- the external schedule -- and
    make each invocation do a single, fast check per bet.

    Skip conditions (returns False without writing anything, no error):
      - Game hasn't started yet (+ buffer) -- nothing to check yet, the
        NEXT triggered run will pick it up once it's time.
      - Game started, buffer has passed, but it's been LESS than
        POLL_START_BUFFER_SECONDS + POLL_MAX_DURATION_SECONDS since game
        start -- a normal "not final yet, try again next scheduled run"
        case, not an error.

    Writes NEEDS_REVIEW (returns False) if it's been more than
    POLL_START_BUFFER_SECONDS + POLL_MAX_DURATION_SECONDS since game
    start with no final score -- this threshold is computed fresh from
    game_dt every call, with no need to track "time since first attempt"
    anywhere, since there's no longer a single process that owns that
    state across the whole polling window.

    Args:
        bet:  A pending bet dict from sheets_reader.load_pending_bets()

    Returns:
        One of:
          "resolved"      -- a final result was found and written this call
          "not_yet_time"  -- game hasn't started (+ buffer) yet
          "still_pending" -- checked, no final score yet, within the normal
                             window -- the NEXT triggered run will
                             check again. This is the expected outcome for
                             most bets on most runs, not an error.
          "needs_review"  -- past the give-up threshold, NEEDS_REVIEW was
                             written
          "error"         -- couldn't parse the game datetime, or the
                             resolver raised
    """
    bet_id   = bet["bet_id"]
    sport    = bet["sport"]
    row_idx  = bet["row_idx"]

    game_dt = _parse_game_datetime(bet["game_date"], bet["game_start"])
    if game_dt is None:
        print(f"[poller] ❌ BetID {bet_id}: could not parse game datetime "
              f"('{bet['game_date']}' '{bet['game_start']}'). Skipping.")
        return "error"

    now_utc = datetime.now(timezone.utc)
    game_dt_utc = game_dt.astimezone(timezone.utc).replace(tzinfo=timezone.utc)
    poll_start = _shift_seconds(game_dt_utc, POLL_START_BUFFER_SECONDS)
    give_up_at = _shift_seconds(game_dt_utc, POLL_START_BUFFER_SECONDS + POLL_MAX_DURATION_SECONDS)

    if now_utc < poll_start:
        print(f"[poller] BetID {bet_id}: game starts at {game_dt.strftime('%m/%d/%Y %I:%M %p CT')}, "
              f"not yet past the {_format_duration(POLL_START_BUFFER_SECONDS)} buffer. "
              f"Skipping this run -- next triggered run will check again.")
        return "not_yet_time"

    # Parlays settle from their legs, not a single game lookup. The gating
    # above already used the parlay row's Game Date/Start (set to the LATEST
    # leg), so by here every leg's game should have started.
    if bet.get("is_parlay"):
        return _poll_parlay(bet, now_utc, give_up_at)

    print(f"[poller] BetID {bet_id}: checking now — {now_utc.strftime('%H:%M:%S UTC')}")

    # Kalshi single bets: settle from the Kalshi market's own resolution first.
    # We stored the SELECTION's market ticker at log time, so a 'yes' settlement
    # is a WIN and 'no' a LOSS with no team matching -- authoritative (the exact
    # contract you held) and not dependent on the Odds API scores feed. If the
    # market isn't finalized yet, fall through to the normal score-based path.
    if (bet.get("book") or "").strip().lower() == "kalshi" and bet.get("kalshi_ticker"):
        from sources.kalshi import get_market_result
        kr = get_market_result(bet["kalshi_ticker"], f"BetID {bet_id}")
        if kr in ("yes", "no"):
            result = RESULT_WIN if kr == "yes" else RESULT_LOSS
            pl, payout = _safe_calculate_pl_payout(bet, result)
            success = write_result(row_idx, result, bet_id, book=bet.get("book"),
                                   pl=pl, payout=payout)
            if success and pl is not None:
                clear_pl_blocked_flag(row_idx, bet_id)
            print(f"[poller] BetID {bet_id}: Kalshi market settled '{kr}' → {result}.")
            return "resolved" if success else "error"

    game = _fetch_result(bet, sport)

    if game is not None:
        try:
            result = resolve(bet, game)
        except ValueError as e:
            print(f"[poller] ❌ BetID {bet_id}: resolver error — {e}")
            return "error"

        pl, payout = _safe_calculate_pl_payout(bet, result)
        success = write_result(row_idx, result, bet_id, book=bet.get("book"), pl=pl, payout=payout)
        if success and pl is not None:
            clear_pl_blocked_flag(row_idx, bet_id)
        return "resolved" if success else "error"

    if now_utc >= give_up_at:
        # Past the give-up threshold (buffer + max duration since game
        # start) with no final score across however many scheduled
        # executions have checked by now -- write NEEDS_REVIEW, not
        # PENDING. The Odds API never returned a final score well past
        # game time, which is consistent with either a slow-to-report
        # game OR a cancellation Odds API can't see. NEEDS_REVIEW
        # surfaces this on the Stats page, where a human can trigger a
        # one-off live ESPN check before falling back to fully manual
        # handling -- no automatic ESPN call happens here.
        print(f"[poller] ⚠️  BetID {bet_id}: past the give-up threshold "
              f"({_format_duration(POLL_START_BUFFER_SECONDS + POLL_MAX_DURATION_SECONDS)} "
              f"since game start) without a final result. Writing NEEDS_REVIEW.")
        write_result(row_idx, RESULT_NEEDS_REVIEW, bet_id)
        return "needs_review"

    print(f"[poller] BetID {bet_id}: not final yet. Next triggered run will check again.")
    return "still_pending"


def _poll_parlay(bet: dict, now_utc, give_up_at) -> str:
    """
    Settles a parlay by checking every leg's game and combining the results.

    A parlay can only be WON once every leg's game is final, so this waits
    until all legs report a final score before settling. (A LOSS the moment
    one leg loses is a valid future optimization, but v1 keeps the simpler,
    always-correct "wait for all legs" rule.)

    Returns the same status strings as poll_bet():
      "resolved"      -- combined result written this call
      "still_pending" -- at least one leg has no final score yet, still
                         within the give-up window
      "needs_review"  -- past the give-up threshold with a leg still missing
                         (a leg game that never reported, or a cancellation
                         the Odds API can't see) -- handed to manual review
      "error"         -- a leg selection couldn't be resolved
    """
    bet_id  = bet["bet_id"]
    row_idx = bet["row_idx"]
    legs    = bet.get("legs") or []

    print(f"[poller] BetID {bet_id}: parlay with {len(legs)} legs — "
          f"checking each leg now ({now_utc.strftime('%H:%M:%S UTC')})")

    leg_games = []
    for i, leg in enumerate(legs, start=1):
        game = _fetch_game_for(leg["sport"], leg["team1"], leg["team2"],
                               leg.get("game_date"), leg.get("bet_type"))
        if game is None:
            print(f"[poller]   leg {i}/{len(legs)} ({leg['team1']} vs {leg['team2']}): "
                  f"no final score yet.")
        leg_games.append(game)

    if any(g is None for g in leg_games):
        if now_utc >= give_up_at:
            print(f"[poller] ⚠️  BetID {bet_id}: past the give-up threshold with at "
                  f"least one parlay leg still unresolved. Writing NEEDS_REVIEW.")
            write_result(row_idx, RESULT_NEEDS_REVIEW, bet_id)
            return "needs_review"
        print(f"[poller] BetID {bet_id}: not all parlay legs final yet. "
              f"Next triggered run will check again.")
        return "still_pending"

    # Every leg has a final game — combine into one parlay outcome.
    try:
        combined_result, effective_decimal = resolve_parlay(legs, leg_games)
    except ValueError as e:
        print(f"[poller] ❌ BetID {bet_id}: parlay resolver error — {e}")
        return "error"

    # On a WIN, settle at the EFFECTIVE combined DECIMAL (push/void legs
    # dropped out and re-priced the parlay). Passing the decimal directly
    # avoids the lossy American round-trip the stored OddsTaken went through
    # -- a parlay's combined price (a product of leg decimals) usually has no
    # exact American form, so settling from American could be off by up to a
    # cent on the dollar. For LOSS/PUSH/VOID the odds don't affect P/L.
    decimal_odds = effective_decimal if combined_result == RESULT_WIN else None
    pl, payout = _safe_calculate_pl_payout(bet, combined_result, decimal_odds=decimal_odds)
    success = write_result(row_idx, combined_result, bet_id,
                           book=bet.get("book"), pl=pl, payout=payout)
    if success and pl is not None:
        clear_pl_blocked_flag(row_idx, bet_id)
    return "resolved" if success else "error"


def _fetch_game_for(sport: str, team1: str, team2: str,
                    game_date: str = None, bet_type: str = None) -> dict | None:
    """
    Routes a result lookup to the right source by sport:

      - Tennis (tennis_atp_* / tennis_wta_*): ESPN's scoreboard, since The Odds
        API /scores feed doesn't cover tennis. ESPN is viable here -- unlike
        the general team-sport league-mapping problem -- because the Odds API
        key already encodes the tour (atp/wta), a deterministic prefix. Only
        Moneyline (match winner) is graded; a tennis Spread/Total returns None
        (can't auto-grade from a winner-only result) and routes to NEEDS_REVIEW.
      - Everything else: The Odds API /scores feed, keyed by its own sport_key.

    Returns a game dict (or None) in the same shape resolver.resolve() expects,
    regardless of which source answered.
    """
    if espn_tennis.is_tennis(sport):
        if bet_type and bet_type.strip() != BET_TYPE_MONEYLINE:
            return None
        return espn_tennis.get_match_result(sport, team1, team2, game_date)
    return odds_api.get_game_result(sport, team1, team2)


def _fetch_result(bet: dict, sport: str) -> dict | None:
    """
    Fetches a final result for a single bet via _fetch_game_for(), which
    dispatches by sport: ESPN for tennis (Moneyline), The Odds API /scores
    for team sports.

    For non-tennis sports ESPN is still NOT consulted automatically -- that
    needed a fragile sport->league mapping (rejected; see git history) and
    remains a human-confirmed fallback via the Stats-page notification bell.
    Tennis is the exception only because its tour is unambiguous from the
    sport key. See RESULT_NEEDS_REVIEW in config.py.
    """
    return _fetch_game_for(sport, bet["team1"], bet["team2"],
                           bet.get("game_date"), bet.get("bet_type"))


def _parse_game_datetime(game_date: str, game_start: str) -> datetime | None:
    """
    Parses the sheet's date and time columns into a timezone-aware datetime.

    Supported formats (matches odds-tool Log Bet Wizard + backfillClosingOdds):
        game_date:  "M/D/YYYY" or "YYYY-MM-DD"
        game_start: "H:MM:SS AM/PM", "H:MM AM/PM", or 24h "HH:MM" / "HH:MM:SS"

    Returns a Central-timezone-aware datetime, or None if parsing fails.
    """
    date_raw = (game_date or "").strip()
    time_raw = (game_start or "").strip()
    if not date_raw or not time_raw:
        return None

    try:
        if "-" in date_raw:
            year, month, day = map(int, date_raw.split("-"))
        elif "/" in date_raw:
            month, day, year = map(int, date_raw.split("/"))
        else:
            raise ValueError("unrecognized date format")

        time_match = re.match(
            r"^(\d{1,2}):(\d{2})(?::(\d{2}))?\s*(AM|PM)?$",
            time_raw,
            re.IGNORECASE,
        )
        if not time_match:
            raise ValueError("unrecognized time format")

        hour = int(time_match.group(1))
        minute = int(time_match.group(2))
        second = int(time_match.group(3) or 0)
        ampm = time_match.group(4)

        if ampm:
            ampm = ampm.upper()
            if ampm == "PM" and hour != 12:
                hour += 12
            elif ampm == "AM" and hour == 12:
                hour = 0

        dt_naive = datetime(year, month, day, hour, minute, second)
        return CENTRAL.localize(dt_naive)
    except (ValueError, TypeError) as e:
        print(f"[poller] Could not parse datetime '{game_date} {game_start}': {e}")
        return None


def _shift_seconds(dt: datetime, seconds: int) -> datetime:
    from datetime import timedelta
    return dt + timedelta(seconds=seconds)


def complete_manual_payout_pl(bet: dict) -> str:
    """
    Derives and writes P/L for a single bet where Payout has been
    manually entered (config.MANUAL_PAYOUT_REQUIRED_BOOKS, e.g. Kalshi --
    see that constant's docstring and resolver.derive_pl_from_payout) but
    P/L is still blank -- see sheets_reader.load_manual_payout_pending_pl_bets()
    for how these rows are found.

    Unlike complete_pl_payout(), this does NOT attempt to compute Payout
    at all -- it's trusted as already correct, typed in by the user from
    their real account, specifically because the American-odds formula
    can't be relied on for this book. The only job here is the cheap,
    certain arithmetic of turning a known Payout into P/L.

    Also runs a non-blocking typo guard: if the manually-entered Payout
    deviates from the rough American-odds estimate by more than 20%, a
    caution note is added via flag_payout_caution() -- NOT a rejection,
    since the manual entry is still trusted as ground truth, just a
    "you might want to double-check this" nudge (e.g. catches a stray
    extra digit or misplaced decimal point).

    Args:
        bet: A dict from sheets_reader.load_manual_payout_pending_pl_bets().

    Returns:
        "completed" if P/L was written. "skipped" if parsing failed or
        the write was rejected (e.g. a race where P/L was no longer
        blank by write time).
    """
    bet_id = bet.get("bet_id", "?")
    row_idx = bet.get("row_idx")
    result = bet.get("result")

    try:
        payout = float(str(bet["payout"]).replace("$", "").replace(",", ""))
        stake = float(str(bet["stake"]).replace("$", "").replace(",", ""))
        bet_category = bet.get("bet_category", "").strip()
        fee_raw = bet.get("fee", "").strip()
        fee = float(fee_raw.replace("$", "").replace(",", "")) if fee_raw else 0.0
    except (KeyError, ValueError, TypeError) as e:
        print(f"[poller] ⚠️  BetID {bet_id}: could not parse Payout/Stake/Fee for manual "
              f"P/L derivation ({e}). Skipping.")
        return "skipped"

    if result != RESULT_WIN:
        # Category-aware math (Bonus Bet LOSS is P/L 0, not -stake). The naive
        # payout - stake - fee formula is only valid for real-money categories.
        book = (bet.get("book") or "").strip()
        fee_before_odds = get_book_fee_before_odds(book) if book else False
        try:
            odds_taken = float(str(bet.get("odds_taken", "0")).replace("+", ""))
        except (ValueError, TypeError):
            odds_taken = 0.0
        pl, _ = calculate_pl_and_payout(
            result, stake, odds_taken, bet_category,
            boost_pct=None, fee=fee, fee_before_odds=fee_before_odds,
        )
    else:
        pl = derive_pl_from_payout(payout, stake, fee, bet_category)

        try:
            odds_taken = float(str(bet.get("odds_taken", "")).replace("+", ""))
            rough_profit = _american_odds_profit(stake, odds_taken)
            rough_payout = rough_profit if bet_category == BET_CATEGORY_BONUS_BET else stake + rough_profit
            if rough_payout > 0 and abs(payout - rough_payout) / rough_payout > 0.20:
                flag_payout_caution(
                    row_idx, bet_id,
                    f"manually-entered Payout (${payout:.2f}) differs from the rough American-odds "
                    f"estimate (${rough_payout:.2f}) by more than 20% -- worth a quick double-check "
                    f"for a typo, though the entered value is what's being used."
                )
        except (ValueError, TypeError):
            pass  # No odds to sanity-check against -- not a blocker, just skip the caution check.

    success = write_pl_only(row_idx, bet_id, pl)
    if success:
        clear_pl_blocked_flag(row_idx, bet_id)
    return "completed" if success else "skipped"


def complete_pl_payout(bet: dict) -> str:
    """
    Computes and writes P/L (and Payout, if applicable) for a single bet
    that already has a Result but is missing both P/L and Payout -- see
    sheets_reader.load_unresolved_pl_bets() for how these rows are found.

    This is the third automated tool alongside the Closing Odds Importer
    (writes ClosingOdds) and the Bet Result Checker (writes Result):
    given a bet already has Stake, Fee, OddsTaken, Bet Category, and now
    Result, this is the only missing piece -- and unlike Result, P/L/
    Payout can be computed for ANY bet type, including Parlay/Prop,
    since the math only needs those already-correct inputs, not a
    determination of which outcome won (confirmed against real Parlay
    data, BetID 11, 2026-06-20).

    Reuses _safe_calculate_pl_payout() -- the exact same calculation
    path poll_bet() already uses -- so a manually-resolved bet gets
    identical math to an automatically-resolved one.

    Args:
        bet:  A dict from sheets_reader.load_unresolved_pl_bets(), which
              includes "result" as a key (unlike load_pending_bets()'s
              dicts, where result is determined by this module, not
              already known).

    Returns:
        "completed" if P/L (and Payout, where applicable) were written.
        "skipped" if the calculation couldn't be completed (missing
        Fee, missing Boost %, parse error, etc. -- _safe_calculate_pl_payout
        already prints the specific reason) or the write was rejected
        (e.g. a race where the row was no longer blank by write time).
    """
    bet_id = bet["bet_id"]
    row_idx = bet["row_idx"]
    result = bet["result"]

    # For a manually-resolved parlay, settle a WIN from the exact combined
    # decimal stored in DecimalOddsTaken (if present) rather than the rounded
    # American OddsTaken -- same precision reasoning as the auto-resolution
    # path. Non-parlay rows pass None and use American odds as before.
    decimal_odds = None
    if result == RESULT_WIN and bet.get("bet_type", "").strip() == BET_TYPE_PARLAY:
        try:
            dec = float(str(bet.get("decimal_odds", "")).strip())
            if dec > 1:
                decimal_odds = dec
        except (ValueError, TypeError):
            pass

    pl, payout = _safe_calculate_pl_payout(bet, result, decimal_odds=decimal_odds)

    if pl is None:
        # _safe_calculate_pl_payout already printed the specific reason
        # (missing Fee, missing Boost %, parse error, etc.) AND flagged it
        # in Notes via flag_pl_blocked() -- visible on the sheet itself,
        # not just this script's own console log.
        return "skipped"

    success = write_pl_payout(row_idx, bet_id, pl, payout)
    if success:
        clear_pl_blocked_flag(row_idx, bet_id)
    return "completed" if success else "skipped"


def _safe_calculate_pl_payout(bet: dict, result: str,
                              decimal_odds: float = None) -> tuple[float | None, float | None]:
    """
    Wraps resolver.calculate_pl_and_payout with defensive parsing of the
    sheet's stake/odds_taken strings. Returns (None, None) on any parsing
    failure rather than raising — a malformed Stake or OddsTaken cell
    shouldn't prevent Result from being written; it just means P/L/Payout
    are left for manual entry on that one row, same as before this feature
    existed.

    Every skip path also calls flag_pl_blocked() to leave a visible note
    on the bet's row itself -- otherwise the only record of why P/L is
    blank is this function's print() output, which nobody sees between
    scheduled runs (confirmed 2026-06-24: a real Insurance Bet loss sat
    with a silently-blank P/L because of exactly this).
    """
    bet_id = bet.get("bet_id", "?")
    row_idx = bet.get("row_idx")
    book = bet.get("book", "").strip()
    try:
        stake = float(str(bet["stake"]).replace("$", "").replace(",", ""))
        odds_taken = float(str(bet["odds_taken"]).replace("+", ""))
        bet_category = bet.get("bet_category", "").strip()
    except (KeyError, ValueError, TypeError) as e:
        reason = (f"could not parse Stake ('{bet.get('stake')}') or "
                  f"OddsTaken ('{bet.get('odds_taken')}').")
        print(f"[poller] ⚠️  BetID {bet_id}: {reason} Result will be written without P/L/Payout.")
        flag_pl_blocked(row_idx, bet_id, reason)
        return None, None

    # Books whose real-money WIN payout can't be reliably computed from
    # American odds at all (config.MANUAL_PAYOUT_REQUIRED_BOOKS, e.g.
    # Kalshi's contracts-based settlement -- see that constant's
    # docstring) -- route to manual Payout entry instead of writing a
    # confidently wrong number. Only WIN is uncertain; LOSS always costs
    # the full stake regardless of settlement mechanics, so it's left to
    # the normal math below. The rough estimate is included purely as a
    # cross-check hint for the person entering the real number, not a
    # value to trust on its own.
    book_lower = book.strip().lower() if isinstance(book, str) else ""
    if book_lower in MANUAL_PAYOUT_REQUIRED_BOOKS and result == RESULT_WIN:
        try:
            rough_profit = _american_odds_profit(stake, odds_taken)
            rough_estimate = f"${stake + rough_profit:.2f}" if bet_category != BET_CATEGORY_BONUS_BET else f"${rough_profit:.2f}"
        except Exception:
            rough_estimate = "(could not estimate)"
        reason = (f"{book} payout can't be reliably computed from American odds (contracts-based "
                  f"settlement, confirmed off by several percent on real bets, 2026-06-26). Enter the "
                  f"exact Payout from your {book} account in this row's Payout column -- P/L will be "
                  f"derived automatically once you do. Rough odds-based estimate for reference only "
                  f"(don't enter this blindly): {rough_estimate}.")
        print(f"[poller] ⚠️  BetID {bet_id}: {reason}")
        flag_pl_blocked(row_idx, bet_id, reason)
        return None, None

    # Win-only percent-fee books (Book Settings "Fee Type" = Percent Of Win
    # Profit or Percent Of Stake On Win) derive their fee at settlement time
    # -- there's nothing to
    # manually enter, so they're exempt from the "Fee is required" check
    # below entirely. Looked up before that check (not after, like
    # fee_before_odds further down) specifically so it can skip it. The
    # two are mutually exclusive per book (a book's Fee Type is one value),
    # kept as separate flags rather than one boolean so the correct
    # resolver parameter (fee_pct_on_win_only vs fee_pct_on_win_stake) gets
    # passed below without conflating the two formulas.
    fee_config = get_book_fee_config(book) if book else {"fee_type": "", "fee_percent": None}
    is_win_profit_fee_book = (
        fee_config["fee_type"] == BOOK_FEE_TYPE_PERCENT_OF_WIN_PROFIT
        and fee_config["fee_percent"] is not None
    )
    is_win_stake_fee_book = (
        fee_config["fee_type"] == BOOK_FEE_TYPE_PERCENT_OF_STAKE_ON_WIN
        and fee_config["fee_percent"] is not None
    )

    if is_win_profit_fee_book or is_win_stake_fee_book:
        fee = 0.0  # Real win-only fee is derived inside calculate_pl_and_payout.
    else:
        # Fee is required, even for bets logged before this field existed --
        # treating a missing Fee as $0 would silently assume no fee was ever
        # charged, when in reality older rows simply predate this column.
        # Refusing to guess surfaces the gap so it gets backfilled deliberately
        # (e.g. via the kind of balance reconciliation done on 2026-06-20),
        # rather than quietly under-reporting P/L by an unknown amount forever.
        fee_raw = bet.get("fee", "").strip()
        if fee_raw == "":
            reason = "Fee column is blank. Fill in Fee (even 0) to calculate P/L."
            print(f"[poller] ⚠️  BetID {bet_id}: {reason} This likely predates the Fee field "
                  f"and needs to be backfilled (see your book-by-book balance reconciliation "
                  f"for known fee totals). Result will be written without P/L/Payout.")
            flag_pl_blocked(row_idx, bet_id, reason)
            return None, None
        try:
            fee = float(fee_raw.replace("$", "").replace(",", ""))
        except ValueError:
            reason = f"could not parse Fee value '{fee_raw}'."
            print(f"[poller] ⚠️  BetID {bet_id}: {reason} Result will be written without P/L/Payout.")
            flag_pl_blocked(row_idx, bet_id, reason)
            return None, None

    # Profit Boost wins need the boost percentage from the linked Promotions
    # row -- fetched fresh, uncached, every time, since this only triggers
    # on the rare case of an actual Profit Boost win.
    boost_pct = None
    if bet_category == BET_CATEGORY_PROFIT_BOOST and result == RESULT_WIN:
        promo_id = bet.get("promo_id", "").strip()
        if not promo_id:
            reason = "Profit Boost WIN but no Promo ID on this row -- cannot look up boost percentage."
            print(f"[poller] ⚠️  BetID {bet_id}: {reason} Result will be written without P/L/Payout.")
            flag_pl_blocked(row_idx, bet_id, reason)
            return None, None
        boost_pct = get_promo_boost_percentage(promo_id)
        if boost_pct is None:
            reason = f"Profit Boost WIN but no Boost % found on Promotions row for Promo ID '{promo_id}'."
            print(f"[poller] ⚠️  BetID {bet_id}: {reason} Result will be written without P/L/Payout.")
            flag_pl_blocked(row_idx, bet_id, reason)
            return None, None

    # Look up whether this book deducts its fee from the stake before
    # applying odds (confirmed for Polymarket), vs. charging it as a flat
    # pass-through on top (the traditional sportsbook default). Fetched
    # fresh every call, same as the boost-percentage lookup above -- no
    # caching, since this only runs once per bet resolution.
    fee_before_odds = get_book_fee_before_odds(book) if book else False

    try:
        return calculate_pl_and_payout(
            result, stake, odds_taken, bet_category, boost_pct, fee, fee_before_odds,
            fee_pct_on_win_only=fee_config["fee_percent"] if is_win_profit_fee_book else None,
            bet_type=bet.get("bet_type", "") if is_win_profit_fee_book else "",
            fee_pct_on_win_stake=fee_config["fee_percent"] if is_win_stake_fee_book else None,
            decimal_odds=decimal_odds,
            round_to_nearest=book_lower in PAYOUT_ROUND_NEAREST_BOOKS,
        )
    except ValueError as e:
        reason = f"could not calculate P/L -- {e}"
        print(f"[poller] ⚠️  BetID {bet_id}: {reason}. Result will be written without P/L/Payout.")
        flag_pl_blocked(row_idx, bet_id, reason)
        return None, None


def _format_duration(seconds: float) -> str:
    """Formats a duration in seconds as a human-readable string."""
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    elif seconds < 3600:
        return f"{seconds // 60}m"
    else:
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
