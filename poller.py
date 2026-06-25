from datetime import datetime, timezone
import pytz
from config import (
    POLL_START_BUFFER_SECONDS,
    POLL_MAX_DURATION_SECONDS,
    RESULT_NEEDS_REVIEW,
    RESULT_WIN,
    BET_CATEGORY_PROFIT_BOOST,
)
from sources import odds_api
from resolver import resolve, calculate_pl_and_payout
from sheets_writer import write_result, write_pl_payout, flag_pl_blocked, clear_pl_blocked_flag
from sheets_reader import get_promo_boost_percentage, get_book_fee_before_odds, load_unresolved_pl_bets

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

    print(f"[poller] BetID {bet_id}: checking now — {now_utc.strftime('%H:%M:%S UTC')}")

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


def _fetch_result(bet: dict, sport: str) -> dict | None:
    """
    Fetches a final score from the Odds API only. ESPN is intentionally
    NOT consulted here during normal polling -- ESPN requires knowing
    which league path corresponds to this sport, and reliably determining
    that mapping turned out to need either a fragile fuzzy match or a
    cached lookup table, both of which were rejected: GitHub Actions runners
    have no persistent disk between runs for a cache, and a stored sport->league mapping
    is exactly the kind of stale, unverified data source this project has
    been actively moving away from. Odds API needs no such mapping --
    it uses its own sport_key directly, the same one already on the bet.

    ESPN is still used, but only for the rare "this bet is stuck well past
    game time" case, and only via a human-confirmed live lookup triggered
    from the Stats page -- never automatically, never from a cached
    mapping. See RESULT_NEEDS_REVIEW in config.py.
    """
    team1 = bet["team1"]
    team2 = bet["team2"]
    return odds_api.get_game_result(sport, team1, team2)


def _parse_game_datetime(game_date: str, game_start: str) -> datetime | None:
    """
    Parses the sheet's date and time columns into a timezone-aware datetime.

    Expected formats:
        game_date:  "M/D/YYYY"  e.g. "5/30/2026" or "11/3/2025"
        game_start: "H:MM:SS AM/PM"  e.g. "4:10:00 AM" or "2:00:00 PM"

    Returns a Central-timezone-aware datetime, or None if parsing fails.
    """
    try:
        dt_str = f"{game_date.strip()} {game_start.strip()}"
        dt_naive = datetime.strptime(dt_str, "%m/%d/%Y %I:%M:%S %p")
        return CENTRAL.localize(dt_naive)
    except ValueError as e:
        print(f"[poller] Could not parse datetime '{game_date} {game_start}': {e}")
        return None


def _shift_seconds(dt: datetime, seconds: int) -> datetime:
    from datetime import timedelta
    return dt + timedelta(seconds=seconds)


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

    pl, payout = _safe_calculate_pl_payout(bet, result)

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


def _safe_calculate_pl_payout(bet: dict, result: str) -> tuple[float | None, float | None]:
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
    book = bet.get("book", "").strip()
    fee_before_odds = get_book_fee_before_odds(book) if book else False

    try:
        return calculate_pl_and_payout(result, stake, odds_taken, bet_category, boost_pct, fee, fee_before_odds)
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
