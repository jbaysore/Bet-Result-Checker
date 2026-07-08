from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP

from config import (
    BET_TYPE_SPREAD, BET_TYPE_MONEYLINE, BET_TYPE_TOTAL, BET_TYPE_DRAW, BET_TYPE_PARLAY,
    RESULT_WIN, RESULT_LOSS, RESULT_PUSH, RESULT_VOID,
    GAME_STATUS_CANCELLED,
    PROMO_FUNDED_CATEGORIES, REAL_MONEY_CATEGORIES,
    BET_CATEGORY_PROFIT_BOOST, BET_CATEGORY_BONUS_BET,
)


def resolve(bet: dict, game: dict) -> str:
    """
    Compares a logged bet against a final game result and returns
    RESULT_WIN, RESULT_LOSS, RESULT_PUSH, or RESULT_VOID.

    Args:
        bet:  A pending bet dict from sheets_reader.load_pending_bets()
              Keys used: bet_type, selection, team1, team2, sport (sport
              is only used by Moneyline, to tell a real soccer-style Draw
              option apart from a sport where a tie just can't happen)
        game: A final game dict from espn.get_game_result() or
              odds_api.get_game_result()
              Keys used: home_team, away_team, home_score, away_score,
              and optionally "status" — if present and equal to
              config.GAME_STATUS_CANCELLED, the bet is resolved as VOID
              without requiring or trusting score fields. Any data source
              feeding this function is responsible for translating its
              own status vocabulary into config.GAME_STATUS_* values.

    Returns:
        "WIN", "LOSS", "PUSH", or "VOID"

    Raises:
        ValueError if the bet type is unrecognised or selection can't be parsed.
    """
    # Check for a cancelled/postponed game before touching scores at all —
    # a cancelled game's score fields may be missing or meaningless, so this
    # must short-circuit ahead of every other branch, not be inferred from
    # a tied or zero score.
    if game.get("status") == GAME_STATUS_CANCELLED:
        return RESULT_VOID

    bet_type  = bet["bet_type"].strip()
    selection = bet["selection"].strip()
    team1     = bet["team1"].strip()
    team2     = bet["team2"].strip()
    sport     = bet.get("sport", "")

    home_team  = game["home_team"]
    away_team  = game["away_team"]
    home_score = game["home_score"]
    away_score = game["away_score"]

    if bet_type == BET_TYPE_MONEYLINE:
        return _resolve_moneyline(selection, team1, team2, home_team, away_team,
                                  home_score, away_score, sport)

    elif bet_type == BET_TYPE_SPREAD:
        return _resolve_spread(selection, team1, team2, home_team, away_team,
                               home_score, away_score)

    elif bet_type == BET_TYPE_TOTAL:
        return _resolve_total(selection, home_score, away_score)

    elif bet_type == BET_TYPE_DRAW:
        return _resolve_draw(home_score, away_score)

    else:
        raise ValueError(f"[resolver] Unrecognised bet type: '{bet_type}'")


def resolve_parlay(legs: list[dict], leg_games: list[dict]) -> tuple[str, float | None]:
    """
    Resolves a whole parlay from its legs and each leg's final game.

    Each leg in `legs` is a normalized dict (see parlay.parse_legs) with the
    same keys resolve() needs (bet_type, selection, team1, team2, sport) plus
    odds_taken. `leg_games` is the parallel list of final game dicts (one per
    leg, same order) as returned by odds_api.get_game_result() -- the caller
    (poller) is responsible for having fetched a FINAL game for every leg
    before calling this; a None entry here is a caller bug.

    Returns (combined_result, effective_decimal) from
    parlay.combine_parlay_results -- WIN/LOSS/PUSH/VOID plus the effective
    decimal price to settle a win at (after dropping any push/void legs).

    Raises ValueError if a leg's selection can't be parsed (propagated from
    resolve()), so the poller can treat the parlay the same way it treats a
    single-bet resolver error.
    """
    from parlay import american_to_decimal, combine_parlay_results

    leg_outcomes = []
    for leg, game in zip(legs, leg_games):
        leg_result = resolve(leg, game)
        leg_decimal = american_to_decimal(leg["odds_taken"])
        leg_outcomes.append((leg_result, leg_decimal))

    return combine_parlay_results(leg_outcomes)


def calculate_pl_and_payout(result: str, stake: float, odds_taken: float,
                            bet_category: str, boost_pct: float = None,
                            fee: float = 0.0, fee_before_odds: bool = False,
                            fee_pct_on_win_only: float = None, bet_type: str = "",
                            fee_pct_on_win_stake: float = None,
                            decimal_odds: float = None,
                            round_to_nearest: bool = False) -> tuple[float, float | None]:
    """
    Computes P/L and Payout for a resolved bet, given American odds and the
    bet's category (which determines whether the stake was real cash or
    promotional credit -- this changes the cost of a loss and the refund
    on a void). Category also affects WIN math specifically for Profit
    Boost: the stake is always real money (confirmed against real usage),
    but the PROFIT portion is boosted by boost_pct, e.g. a 100% boost
    doubles the normal profit. The stake itself is never boosted.

    Args:
        result:       One of RESULT_WIN, RESULT_LOSS, RESULT_PUSH, RESULT_VOID
        stake:        The wagered amount, always a positive number
        odds_taken:   American odds, e.g. 154 or -453
        bet_category: One of the values in config.PROMO_FUNDED_CATEGORIES
                      or config.REAL_MONEY_CATEGORIES
        boost_pct:    Required when bet_category is "Profit Boost" and
                      result is WIN -- the boost percentage from the
                      linked Promotions row (e.g. 100.0 for a 100% boost).
                      Ignored for every other category/result combination.
        fee:          Any per-bet fee charged by the book. Defaults to 0.0
                      since most bets/books have none and this field is
                      filled in only when discovered during reconciliation
                      against the book's real balance, not at logging time.
        fee_before_odds: When True (looked up per-book from the "Book
                      Settings" tab's Fee Before Odds column), the fee is
                      treated as reducing the effective stake BEFORE
                      profit/payout is computed from odds, rather than as
                      a flat deduction layered on top afterward. Confirmed
                      against two real Polymarket bets, reconciled exactly
                      (within Polymarket's own UI rounding) against their
                      quoted "to win" payout figures on 2026-06-20:
                      Polymarket's share-based mechanism means the fee
                      genuinely comes out of the amount actually wagered,
                      not out of the final profit. Traditional sportsbooks
                      (confirmed for Caesars via the Illinois Gaming
                      Board's wager-tax FAQ) charge the fee as a separate
                      pass-through on top of normal odds math instead --
                      this is the default (False) behavior.
        fee_pct_on_win_only: For books like ProphetX whose fee is a
                      percentage of NET PROFIT, charged ONLY on a win
                      (never a loss/push/void, never a parlay regardless
                      of outcome -- confirmed via ProphetX's published fee
                      terms, 2026-06-25). When set, this OVERRIDES `fee`
                      entirely: forced to 0.0 for every non-WIN result and
                      for parlay wins, and derived as
                      round(profit * fee_pct_on_win_only / 100, 2) for a
                      non-parlay win, since the fee isn't knowable as a
                      fixed dollar amount until profit itself is computed
                      (unlike every other book, where `fee` is a pre-known
                      value entered/looked up before this function runs).
        bet_type:     Required when fee_pct_on_win_only is set, to apply
                      the parlay exemption (config.BET_TYPE_PARLAY).
                      Ignored otherwise.
        fee_pct_on_win_stake: For books whose fee is a percentage of STAKE
                      (not profit, unlike fee_pct_on_win_only), charged ONLY
                      on a win (never a loss/push/void). No parlay exemption
                      (unlike ProphetX's published carve-out for
                      fee_pct_on_win_only) -- applies on parlay and
                      non-parlay wins alike. When
                      set, this OVERRIDES `fee` entirely: forced to 0.0 for
                      every non-WIN result, derived as
                      round(stake * fee_pct_on_win_stake / 100, 2) on a win.
        round_to_nearest: How the winning profit's fractional cent is
                      handled. False (default) truncates in the house's favor
                      (DraftKings and most books); True rounds to the nearest
                      cent (FanDuel). Set per-book by the caller from
                      config.PAYOUT_ROUND_NEAREST_BOOKS. Only affects a WIN.

    Returns:
        (pl, payout) -- payout is None when nothing is paid out (a loss,
        or a void on promo-funded credit that was never real money to
        begin with).

    Raises:
        ValueError if bet_category is "Profit Boost", result is WIN, and
        boost_pct is None -- this is a deliberate refusal to guess. Paying
        out unboosted profit on a Profit Boost win would silently
        underpay every such win with no visible error, which is worse
        than failing loudly and requiring the Boost % to actually be set
        on the linked Promotions row first.

    Rules when fee_before_odds is False (traditional sportsbooks, default):
      WIN:  pays stake + profit for every category EXCEPT Bonus Bet, which
            pays profit only -- the bonus-bet token itself is consumed on
            any outcome and never converts to withdrawable cash, even on
            a win. For Profit Boost, profit is multiplied by
            (1 + boost_pct/100) before being added to the stake for Payout.
            Fee is then subtracted from P/L (not Payout).
      LOSS: promo-funded categories (Bonus Bet, Deposit Bonus) cost
            nothing, P/L = 0, since the stake was never real cash.
            Everything else loses the full stake, P/L = -stake. Fee is
            included in this loss regardless of fee_before_odds -- the
            full original stake (fee included) was always confirmed lost
            either way (see Decision Log, 2026-06-20).
      PUSH: stake is returned in full (Payout = stake), P/L = -fee (push
            still incurs the fee -- confirmed via DraftKings/IL FAQ).
      VOID: real-money categories get the stake back (Payout = stake,
            P/L = 0, fee NOT charged). Promo-funded categories get
            nothing back (Payout = None, P/L = 0).

    Rules when fee_before_odds is True (confirmed for Polymarket):
      WIN:  effective_stake = stake - fee; profit is computed from
            effective_stake (not the full stake); Payout =
            effective_stake + profit; P/L = profit. No separate fee
            subtraction afterward -- it's already reflected in the
            smaller effective_stake.
      LOSS: P/L = -stake, the full original stake (fee already included
            in what Polymarket charged as Cost, so it's not subtracted
            again). This is different from the False case above (where
            fee IS subtracted separately on a loss), specifically because
            for fee_before_odds books the fee was already part of the
            stake you paid, not a separate charge layered on top.
      PUSH: Payout = stake - fee, P/L = -fee (not 0 -- corrected
            2026-06-20: the fee is a real, permanent loss even though
            the stake itself is returned, same reasoning as VOID below).
      VOID: Payout = stake - fee, P/L = -fee -- confirmed directly: a
            real Polymarket VOID on a $51.24 stake with $1.08 fee
            returned exactly $50.16 (stake - fee). P/L was originally
            set to 0 (treating this as break-even), which was wrong --
            staking $51.24 and getting back $50.16 is a genuine $1.08
            loss, not a wash.
    """
    is_promo_funded = bet_category in PROMO_FUNDED_CATEGORIES

    # ProphetX-style books: force fee=0 here for every non-WIN result and
    # for parlay wins, so callers never have to remember the exemption --
    # the WIN branch below derives the real fee from profit once it's
    # known, for the one case (non-parlay win) where a fee actually applies.
    if fee_pct_on_win_only is not None and (result != RESULT_WIN or bet_type.strip() == BET_TYPE_PARLAY):
        fee = 0.0
    # Stake-on-win books: force fee=0 on every non-WIN result; no parlay
    # exemption -- see fee_pct_on_win_stake's docstring above.
    if fee_pct_on_win_stake is not None and result != RESULT_WIN:
        fee = 0.0

    if result == RESULT_WIN:
        effective_stake = (stake - fee) if fee_before_odds else stake
        # A parlay passes its exact combined decimal price (decimal_odds) so
        # profit is computed from it directly, instead of from a lossy
        # American round-trip of the same product. Single bets pass American
        # odds as before (decimal_odds is None).
        profit = (_decimal_odds_profit(effective_stake, decimal_odds, round_to_nearest)
                  if decimal_odds is not None
                  else _american_odds_profit(effective_stake, odds_taken, round_to_nearest))
        if bet_category == BET_CATEGORY_PROFIT_BOOST:
            if boost_pct is None:
                raise ValueError(
                    "[resolver] Cannot resolve a Profit Boost WIN without a boost "
                    "percentage -- set 'Boost %' on the linked Promotions row "
                    "before resolving this bet. Refusing to guess at an unboosted "
                    "payout, since that would silently underpay this win."
                )
            profit = round(profit * (1 + boost_pct / 100), 2)

        if fee_pct_on_win_only is not None and bet_type.strip() != BET_TYPE_PARLAY:
            fee = round(profit * (fee_pct_on_win_only / 100), 2)

        if fee_pct_on_win_stake is not None:
            fee = round(stake * (fee_pct_on_win_stake / 100), 2)

        if fee_before_odds:
            # Fee is already reflected in effective_stake -- no separate
            # subtraction here, that would double-deduct it.
            if bet_category == BET_CATEGORY_BONUS_BET:
                return round(profit, 2), round(profit, 2)
            return round(profit, 2), round(effective_stake + profit, 2)

        # Bonus Bet is the one category where a WIN does not return the
        # stake -- the bonus-bet token is consumed regardless of outcome,
        # and only the profit becomes real, withdrawable money. Every
        # other category (including Deposit Bonus, which behaves as real
        # credit once granted) pays stake + profit on a win.
        if bet_category == BET_CATEGORY_BONUS_BET:
            return round(profit - fee, 2), round(profit, 2)
        return round(profit - fee, 2), round(stake + profit, 2)

    if result == RESULT_LOSS:
        if is_promo_funded:
            return round(0.0 - fee, 2), None
        if fee_before_odds:
            # Fee is already part of the original stake (it was deducted
            # from the Cost Polymarket charged) -- the full stake lost
            # already reflects it. Subtracting fee again here would
            # double-charge it, which is exactly the bug this branch
            # exists to avoid (caught by test_polymarket_fee_mechanics.py).
            return round(-stake, 2), None
        # Traditional sportsbook: fee is a separate charge layered on top
        # of the lost stake (confirmed via test_fee.py against DraftKings/
        # Illinois fee rules).
        return round(-stake - fee, 2), None

    if result == RESULT_PUSH:
        if fee_before_odds:
            # Same fix as VOID below: P/L = -fee, not 0 -- you staked the
            # full amount including fee, got back stake-fee, so the fee
            # is a genuine, permanent loss even on a push.
            return round(-fee, 2), round(stake - fee, 2)
        # Push still incurs the fee -- confirmed against the Illinois Gaming
        # Board's FAQ via DraftKings' support page: "the pass-through tax
        # will not be returned if the bet is graded as a push."
        return round(0.0 - fee, 2), stake

    if result == RESULT_VOID:
        if fee_before_odds:
            # Confirmed directly from a real Polymarket VOID: $51.24 stake,
            # $1.08 fee, returned exactly $50.16 -- fee is never refunded.
            # P/L = -fee (not 0): you staked the full amount including fee,
            # got back stake-fee, so you're genuinely down the fee amount.
            # This corrects an earlier error (2026-06-20) where P/L was
            # set to 0.0, treating the void as break-even -- it isn't,
            # since the fee is a real, permanent loss even when the bet
            # itself never happened.
            if is_promo_funded:
                return round(-fee, 2), None
            return round(-fee, 2), round(stake - fee, 2)
        # Void does NOT incur the fee for traditional sportsbooks, unlike
        # push -- confirmed: "pushed bets will pay the fee, but voided
        # bets do not" (Legal Sports Report, citing IL sportsbook policy).
        if is_promo_funded:
            return 0.0, None
        return 0.0, stake

    raise ValueError(f"[resolver] Cannot calculate P/L for unrecognised result: '{result}'")


def derive_pl_from_payout(payout: float, stake: float, fee: float, bet_category: str) -> float:
    """
    The inverse of calculate_pl_and_payout()'s WIN branch: given a REAL,
    already-known Payout (entered manually because the book's settlement
    can't be reliably computed from American odds -- see
    config.MANUAL_PAYOUT_REQUIRED_BOOKS, e.g. Kalshi's contracts-based
    exchange), derives P/L from it instead of computing Payout from odds.

    Mirrors the same per-category relationship calculate_pl_and_payout()
    uses on a WIN:
      - Bonus Bet: Payout IS the profit (the token itself, not stake, is
        what's "returned" on a win) -> P/L = Payout - fee.
      - Every other category: Payout = stake + profit -> P/L =
        Payout - stake - fee.

    Only meaningful for a WIN (the only result where a book's nonstandard
    settlement mechanics create genuine uncertainty -- a LOSS always
    costs the full stake regardless of settlement quirks, so it never
    needs manual Payout entry in the first place).
    """
    if bet_category == BET_CATEGORY_BONUS_BET:
        return round(payout - fee, 2)
    return round(payout - stake - fee, 2)


def derive_cashout_pl(
    payout: float,
    stake: float,
    fee: float,
    bet_category: str,
    *,
    fee_before_odds: bool = False,
) -> float:
    """
    P/L for a manual cashout when the user enters the dollars returned.
    Mirrors odds-tool/cashoutPl.js deriveCashoutPl().
    """
    if bet_category == BET_CATEGORY_BONUS_BET:
        return round(payout - fee, 2)
    if fee_before_odds:
        return round(payout - stake, 2)
    return round(payout - stake - fee, 2)


def _american_odds_profit(stake: float, odds: float, round_to_nearest: bool = False) -> float:
    """
    Converts American odds to profit on a winning bet.
    Positive odds (e.g. +154): profit = stake * (odds / 100)
    Negative odds (e.g. -453): profit = stake * (100 / abs(odds))

    The fractional-cent rounding is PER-BOOK (see config's per-book sets and
    the callers that pass round_to_nearest):

      - round_to_nearest=False (default): truncate DOWN to the cent, in the
        house's favor. Confirmed against two real DraftKings settlements that
        disagreed with nearest-cent rounding by $0.01 (stake $325.76 @ -255:
        DraftKings paid $127.74, not $127.75; stake $100 @ -453: paid $22.07,
        not $22.08).
      - round_to_nearest=True: round to the NEAREST cent. Confirmed for
        FanDuel against a real settlement (stake $284 @ -430: profit
        66.0465... paid as $66.05, where truncation gives $66.04). Earlier
        code assumed truncation was industry-wide; FanDuel disproved that.

    Uses Decimal (not float + math.floor) because binary floats can't exactly
    represent most stake/odds combinations -- a stray float imprecision could
    flip an exact cent value the wrong way. Decimal(str(x)) on the ORIGINAL
    inputs keeps the division exact-as-typed before rounding only once, at the
    very end.
    """
    if odds > 0:
        raw = Decimal(str(stake)) * Decimal(str(odds)) / Decimal(100)
    else:
        raw = Decimal(str(stake)) * Decimal(100) / Decimal(str(abs(odds)))
    rounding = ROUND_HALF_UP if round_to_nearest else ROUND_DOWN
    return float(raw.quantize(Decimal('0.01'), rounding=rounding))


def _decimal_odds_profit(stake: float, decimal_odds: float, round_to_nearest: bool = False) -> float:
    """
    Profit on a winning bet priced in DECIMAL odds: stake * (decimal - 1).

    Exists for parlays, whose combined price is a product of leg decimals
    (e.g. 2.105008) that usually has no exact American representation -- so
    round-tripping it through American (+110/+111) would settle a win at the
    wrong price by up to a cent on the dollar. Same per-book cent rounding as
    _american_odds_profit (truncate by default; nearest for FanDuel and other
    round_to_nearest books) and the same exact-Decimal arithmetic, just from
    the decimal price directly.
    """
    raw = Decimal(str(stake)) * (Decimal(str(decimal_odds)) - Decimal(1))
    rounding = ROUND_HALF_UP if round_to_nearest else ROUND_DOWN
    return float(raw.quantize(Decimal('0.01'), rounding=rounding))


# ── Moneyline ─────────────────────────────────────────────────────────────────

def _is_draw_possible_sport(sport: str) -> bool:
    """
    True for sports where Draw is a real, separately-bettable third
    selection on the moneyline (soccer) -- mirrors the client's existing
    THREE_WAY_KEY_PREFIXES/THREE_WAY_GROUPS heuristic in
    odds-tool/client/src/utils/twoWaySports.js (sport_key startswith
    "soccer_", or bare "soccer"), since there's no shared sport-profile
    data source between this Python repo and that Node app -- the Sport
    column's stored value (the Odds API sport_key, e.g. "soccer_epl") is
    the only signal available here.
    """
    return (sport or "").strip().lower().startswith("soccer")


def _resolve_moneyline(selection, team1, team2, home_team, away_team,
                       home_score, away_score, sport: str = "") -> str:
    """
    Selection is a team name (Team 1 or Team 2 from sheet), or "Draw".
    For team bets, the selected team must win outright.
    For Draw bets, scores must be equal at full time.

    A tied score is only a PUSH for a team selection when Draw wasn't a
    real, separately-bettable option on this market (sports with no draw
    possibility at all, e.g. NBA) -- when it WAS available (soccer), a
    team bet that doesn't win is a genuine LOSS, the same as it would be
    against any other non-winning outcome. Bug fixed 2026-06-26: a real
    3-way soccer bet (Team1/Team2/Draw, one bet per side) ended in a draw,
    and the two team-selection bets were incorrectly marked PUSH instead
    of LOSS -- this function had no way to know Draw existed as its own
    selection on that market.
    """
    # Handle three-way moneyline draw selection (common in soccer)
    if selection.strip().lower() == "draw":
        return _resolve_draw(home_score, away_score)

    if home_score == away_score:
        if not _is_draw_possible_sport(sport):
            return RESULT_PUSH
        return RESULT_LOSS

    winning_team = home_team if home_score > away_score else away_team
    bet_team = _resolve_team(selection, team1, team2, home_team, away_team)

    return RESULT_WIN if _team_matches(bet_team, winning_team) else RESULT_LOSS


# ── Spread ────────────────────────────────────────────────────────────────────

def _resolve_spread(selection, team1, team2, home_team, away_team,
                    home_score, away_score) -> str:
    """
    Selection format: "Chiefs -3.5" or "Raiders +3.5" or "Chiefs -3"
    Parses the team name and line from the selection string.
    """
    team_name, line = _parse_spread_selection(selection)
    bet_team = _resolve_team(team_name, team1, team2, home_team, away_team)

    # Determine if the bet team is home or away, then calculate margin
    if _team_matches(bet_team, home_team):
        margin = home_score - away_score
    else:
        margin = away_score - home_score

    # Apply the spread: positive line = underdog, negative = favourite
    # margin + line > 0 → covered, == 0 → push, < 0 → lost
    result = margin + line

    if result > 0:
        return RESULT_WIN
    elif result < 0:
        return RESULT_LOSS
    else:
        return RESULT_PUSH


def _parse_spread_selection(selection: str) -> tuple[str, float]:
    """
    Parses "Chiefs -3.5" → ("Chiefs", -3.5)
    Parses "Raiders +7"  → ("Raiders", 7.0)
    Parses "Lakers -3"   → ("Lakers", -3.0)

    Raises ValueError if the format can't be parsed.
    """
    selection = selection.strip()

    # Find the last token — should be the line (e.g. "-3.5", "+7")
    parts = selection.rsplit(" ", 1)
    if len(parts) != 2:
        raise ValueError(f"[resolver] Can't parse spread selection: '{selection}'. "
                         f"Expected format: 'Team -3.5' or 'Team +7'")

    team_part = parts[0].strip()
    line_part = parts[1].strip()

    try:
        line = float(line_part)
    except ValueError:
        raise ValueError(f"[resolver] Can't parse line '{line_part}' from selection "
                         f"'{selection}'. Expected a number like -3.5 or +7.")

    return team_part, line


# ── Total ─────────────────────────────────────────────────────────────────────

def _resolve_total(selection: str, home_score: int, away_score: int) -> str:
    """
    Selection format: "Over 47.5" or "Under 47.5" or "Over 210" etc.
    Case-insensitive.
    """
    parts = selection.strip().split(" ", 1)
    if len(parts) != 2:
        raise ValueError(f"[resolver] Can't parse total selection: '{selection}'. "
                         f"Expected format: 'Over 47.5' or 'Under 47.5'")

    direction = parts[0].strip().lower()
    try:
        line = float(parts[1].strip())
    except ValueError:
        raise ValueError(f"[resolver] Can't parse total line from '{selection}'.")

    actual = home_score + away_score

    if actual == line:
        return RESULT_PUSH

    if direction == "over":
        return RESULT_WIN if actual > line else RESULT_LOSS
    elif direction == "under":
        return RESULT_WIN if actual < line else RESULT_LOSS
    else:
        raise ValueError(f"[resolver] Unknown total direction '{direction}' in '{selection}'. "
                         f"Expected 'Over' or 'Under'.")


# ── Draw ──────────────────────────────────────────────────────────────────────

def _resolve_draw(home_score: int, away_score: int) -> str:
    """
    Bet on the draw — wins if scores are equal at full time.
    """
    return RESULT_WIN if home_score == away_score else RESULT_LOSS


# ── Helpers ───────────────────────────────────────────────────────────────────

def _resolve_team(selection_team: str, team1: str, team2: str,
                  home_team: str, away_team: str) -> str:
    """
    Maps the team name from the Selection column back to the
    API-returned team name (home_team or away_team).

    Tries matching against team1/team2 first (sheet names),
    then directly against the API names as a fallback.
    """
    sel = selection_team.lower().strip()

    if _fuzzy_match(sel, team1.lower()):
        # team1 from sheet — now find its API counterpart
        if _fuzzy_match(team1.lower(), home_team.lower()):
            return home_team
        if _fuzzy_match(team1.lower(), away_team.lower()):
            return away_team

    if _fuzzy_match(sel, team2.lower()):
        if _fuzzy_match(team2.lower(), home_team.lower()):
            return home_team
        if _fuzzy_match(team2.lower(), away_team.lower()):
            return away_team

    # Direct match against API names as last resort
    if _fuzzy_match(sel, home_team.lower()):
        return home_team
    if _fuzzy_match(sel, away_team.lower()):
        return away_team

    raise ValueError(f"[resolver] Could not match selection team '{selection_team}' "
                     f"to either '{home_team}' or '{away_team}'.")


def _fuzzy_match(a: str, b: str) -> bool:
    """
    Returns True if two team name strings refer to the same team.
    Checks exact match, then substring in either direction.
    Handles "Chiefs" matching "Kansas City Chiefs" etc.
    """
    a = a.strip()
    b = b.strip()
    if not a or not b:
        return False
    return a == b or a in b or b in a


def _team_matches(team_a: str, team_b: str) -> bool:
    return _fuzzy_match(team_a.lower(), team_b.lower())
