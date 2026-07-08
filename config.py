import os
import json
from dotenv import load_dotenv

load_dotenv()

# ── Google Sheets ────────────────────────────────────────────────
SHEET_ID = os.getenv("SHEET_ID")
SHEET_TAB = os.getenv("SHEET_TAB", "Bets")

# Locally: GOOGLE_APPLICATION_CREDENTIALS points to a file path.
# In GitHub Actions: GOOGLE_APPLICATION_CREDENTIALS_JSON holds the raw JSON
# content, injected directly from the SERVICE_ACCOUNT_JSON repo secret.
CREDENTIALS_PATH = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
CREDENTIALS_JSON = os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON")


def get_credentials_info() -> dict:
    """
    Returns the service account credentials as a dict, regardless of
    whether they came from a local file (dev) or an injected secret (GitHub Actions).
    """
    if CREDENTIALS_JSON:
        return json.loads(CREDENTIALS_JSON)
    if CREDENTIALS_PATH:
        with open(CREDENTIALS_PATH, "r") as f:
            return json.load(f)
    raise RuntimeError(
        "No credentials found. Set GOOGLE_APPLICATION_CREDENTIALS (local file path) "
        "or GOOGLE_APPLICATION_CREDENTIALS_JSON (raw JSON, used in GitHub Actions)."
    )


# ── Odds API ─────────────────────────────────────────────────────
ODDS_API_KEY = os.getenv("ODDS_API_KEY")
ODDS_API_BASE = "https://api.the-odds-api.com/v4"

# ── Bets tab column headers ─────────────────────────────────────────
# Read by HEADER NAME, not a fixed index dict -- matches the Promotions
# tab convention (PROMO_COL). Resolves against the live sheet header row
# at read/write time so columns may be reordered freely.
#
# Schema: BetID | Date Placed | Book | Sport | Team 1 | Team 2 |
#         Game Date | Game Start Time | Selection | Bet Type |
#         OddsTaken | DecimalOddsTaken | ClosingOdds | DecimalClosingOdds |
#         CLV | Stake | Fee | Bet Category | Promo ID | Result | Payout |
#         P/L | Notes
BET_COL = {
    "bet_id":          "BetID",
    "date_placed":     "Date Placed",
    "book":            "Book",
    "sport":           "Sport",
    "team1":           "Team 1",
    "team2":           "Team 2",
    "game_date":       "Game Date",
    "game_start":      "Game Start Time",
    "selection":       "Selection",
    "bet_type":        "Bet Type",
    "odds_taken":      "OddsTaken",
    "decimal_odds":    "DecimalOddsTaken",
    "closing_odds":    "ClosingOdds",
    "decimal_closing": "DecimalClosingOdds",
    "clv":             "CLV",
    "stake":           "Stake",
    "fee":             "Fee",
    "bet_category":    "Bet Category",
    "promo_id":        "Promo ID",
    # TRUE when a Standard bet is a cash hedge placed against a promotion.
    # Written by the odds-tool Log Bet Wizard; the poller only preserves it
    # (settlement/promo logic never reads it). Blank for non-hedge bets.
    "hedge":           "Hedge",
    "result":          "Result",
    "payout":          "Payout",
    "pl":              "P/L",
    "notes":           "Notes",
    # JSON array of leg dicts for a Parlay row (written by the odds-tool Log
    # Bet Wizard). Blank for every single-selection bet. Each leg:
    #   {sport, book, team1, team2, gameDate, gameStart, betType, selection, oddsTaken}
    # The parlay row's OddsTaken is the combined American price; its Game
    # Date/Start are the LATEST leg, so the poller waits for every leg to
    # finish before settling. See parlay.py for parse/combine logic.
    "legs":            "Legs",
    # The Kalshi market ticker for a Kalshi bet (resolved + written by the
    # odds-tool server at log time). Lets the closing-odds pass pull the real
    # closing line from Kalshi's candlesticks API instead of falling back to
    # MANUAL ENTRY. Blank for non-Kalshi bets. See sources/kalshi.py.
    "kalshi_ticker":   "Kalshi Ticker",
}

# ── Bet types ─────────────────────────────────────────────────────
# Values as they appear in your Sheet's Bet Type column
BET_TYPE_SPREAD     = "Spread"
BET_TYPE_MONEYLINE  = "Moneyline"
BET_TYPE_TOTAL      = "Total"
BET_TYPE_DRAW       = "Draw"
BET_TYPE_PARLAY     = "Parlay"
BET_TYPE_PROP       = "Prop"

# Bet types this tool will resolve automatically as a SINGLE selection.
# Parlay is handled separately (parlay.py) -- each of its legs must itself
# be one of these for the parlay to auto-resolve; a parlay containing a
# Prop or manually-tracked leg falls back to manual Result entry.
AUTOMATED_BET_TYPES = {BET_TYPE_SPREAD, BET_TYPE_MONEYLINE, BET_TYPE_TOTAL, BET_TYPE_DRAW}

# A leg of a parlay can only be auto-resolved / auto-priced if its own bet
# type is in this set (same as AUTOMATED_BET_TYPES today, named separately
# so the parlay path reads clearly and can diverge later if needed).
AUTOMATED_LEG_BET_TYPES = AUTOMATED_BET_TYPES

# ── Book Settings "Fee Type" values ────────────────────────────────
# Default (blank/anything else) means "flat dollar fee, entered manually
# at logging time" -- the original, still-most-common model. This is the
# only other value so far, for books like ProphetX whose fee is a
# percentage of NET PROFIT, derived at settlement time rather than known
# upfront -- see sheets_reader.get_book_fee_config() and
# resolver.calculate_pl_and_payout()'s fee_pct_on_win_only parameter.
BOOK_FEE_TYPE_PERCENT_OF_WIN_PROFIT = "Percent Of Win Profit"

# Same win-only shape as BOOK_FEE_TYPE_PERCENT_OF_WIN_PROFIT, but the fee is
# a percentage of STAKE, not profit (reserved for books that use that model;
# BetOpenly uses BOOK_FEE_TYPE_PERCENT_OF_WIN_PROFIT at 1% per their public
# "1% if you win" / commission-on-winnings terms). See
# resolver.calculate_pl_and_payout()'s fee_pct_on_win_stake parameter.
BOOK_FEE_TYPE_PERCENT_OF_STAKE_ON_WIN = "Percent Of Stake On Win"

# Books whose real-money WIN payout can't be reliably computed from
# American odds at all -- not a rounding-rule difference (like
# DraftKings' cent-truncation, handled inside resolver._american_odds_profit),
# but a fundamentally different settlement mechanism. Kalshi is a
# contracts-based exchange (you buy a whole number of $1-payout contracts
# at a cents-denominated price) rather than a fixed-odds sportsbook, and
# two real settlements (2026-06-26) came back several percent off from
# the American-odds approximation -- too large to be cent rounding, and
# in the OPPOSITE direction from what Kalshi's own published 7% taker-fee
# formula (kalshiOdds.js) would predict. Rather than guess at a contract-
# count/fee model from two data points, these books are routed to manual
# Payout entry instead: poller._safe_calculate_pl_payout() flags the row
# (PL_BLOCKED_PREFIX) asking for the real Payout from the book's own
# account, and poller.complete_manual_payout_pl() derives P/L from
# whatever Payout gets entered, once it's there.
MANUAL_PAYOUT_REQUIRED_BOOKS = {"kalshi"}

# Books that settle a winning payout by ROUNDING the fractional cent to the
# NEAREST cent, rather than truncating it in the house's favor. The default
# (any book not listed here) truncates -- confirmed against real DraftKings
# settlements ($100 @ -453 -> $22.07, $325.76 @ -255 -> $127.74; see
# resolver._american_odds_profit). FanDuel rounds the other way: confirmed
# 2026-07-01 against a real settlement, stake $284 @ -430, profit 66.0465...
# paid as $66.05 (payout $350.05), where truncation would give $350.04.
# ProphetX matches FanDuel here too: confirmed 2026-07-01, stake $297.83 @
# -470, app shows +$63.37 before the 2% fee (63.3680... truncated would be
# $63.36 -> P/L $62.09; nearest gives $63.37 -> P/L $62.10). Fanatics also
# rounds nearest: confirmed 2026-07-06, stake $1,017 @ -270, profit
# 376.6666... paid as $376.67 (payout $1,393.67); truncation gives $376.66.
# Keyed by lowercased book key, same convention as MANUAL_PAYOUT_REQUIRED_BOOKS.
PAYOUT_ROUND_NEAREST_BOOKS = {"fanduel", "prophetx", "fanatics"}

# ── Bet Categories ──────────────────────────────────────────────────
# Matches the 6 canonical values enforced by LogBetWizard.jsx's BET_CATEGORIES
# (Free Bet was removed -- unused, and its real payout behavior was never
# validated against an actual promo).
BET_CATEGORY_QUALIFYING     = "Qualifying Bet"
BET_CATEGORY_DEPOSIT_BONUS  = "Deposit Bonus"
BET_CATEGORY_BONUS_BET      = "Bonus Bet"
BET_CATEGORY_PROFIT_BOOST   = "Profit Boost"
BET_CATEGORY_STANDARD       = "Standard"
BET_CATEGORY_INSURANCE_BET  = "Insurance Bet"
# Odds Boost: a real-money bet placed at boosted odds on one book-specified
# line. The stake is your own cash and the bet is logged at the BOOSTED odds,
# so its own P/L is settled with normal real-money math (no boost % applied --
# the odds already are the boost). The promo's value is derived separately by
# promo_resolver as the P/L difference vs the original (pre-boost) odds.
BET_CATEGORY_ODDS_BOOST     = "Odds Boost"

# Categories where the stake itself is bonus/promotional credit, not real
# cash -- a loss costs nothing (P/L = 0), and a void returns no Payout
# (there was no real money to give back).
PROMO_FUNDED_CATEGORIES = {BET_CATEGORY_BONUS_BET, BET_CATEGORY_DEPOSIT_BONUS}

# Categories where real cash is at risk regardless of the promo label
# attached -- a loss costs the full stake (P/L = -stake), and a void
# returns the stake as Payout. Profit Boost and Insurance Bet both fall
# here: the promo affects odds or provides a separate refund credit, but
# the wagered stake itself was genuinely your money.
REAL_MONEY_CATEGORIES = {BET_CATEGORY_STANDARD, BET_CATEGORY_QUALIFYING,
                          BET_CATEGORY_PROFIT_BOOST, BET_CATEGORY_INSURANCE_BET,
                          BET_CATEGORY_ODDS_BOOST}

# ── Result values ─────────────────────────────────────────────────
RESULT_WIN   = "WIN"
RESULT_LOSS  = "LOSS"
RESULT_PUSH  = "PUSH"
RESULT_VOID  = "VOID"        # written when a game is cancelled/postponed and never played
RESULT_CASHOUT = "CASHOUT"   # manual early exit — payout entered by user
RESULT_NEEDS_REVIEW = "NEEDS_REVIEW"  # written when Odds API never returned a final
                                       # score well past game time -- use the odds-tool
                                       # notification bell (Check result) before full
                                       # manual review. Odds API scores first; ESPN
                                       # for cancellations; apply WIN/LOSS/PUSH/VOID
                                       # from the bell when confirmed.
RESULT_PENDING = "PENDING"  # written when a NEEDS_REVIEW check finds nothing useful
                             # (or is skipped) -- fully manual review from here

# ── Closing odds failure codes ────────────────────────────────────
# Written to the ClosingOdds column when the historical snapshot fetch
# succeeds (no transient error) but the specific bet can't be matched.
# Surfaces in the odds-tool notification bell until manually cleared
# or overwritten with a real value.
CLOSING_ODDS_GAME_NOT_FOUND      = "GAME NOT FOUND"
CLOSING_ODDS_BOOK_NOT_FOUND      = "BOOK NOT FOUND"
CLOSING_ODDS_SELECTION_NOT_FOUND = "SELECTION NOT FOUND"
# Exchange / direct-feed books (Kalshi, etc.) are not on The Odds API historical
# endpoint — closing line must be entered manually from the book's own UI.
CLOSING_ODDS_MANUAL_REQUIRED     = "MANUAL ENTRY"
# Sport key on the bet row is not in The Odds API's current in-season list
# (e.g. soccer_fifa_world_cup between tournaments).
CLOSING_ODDS_SPORT_NOT_ON_API    = "SPORT NOT ON API"

# The full set of failure codes (mirrors odds-tool's CLOSING_ODDS_ERROR_CODES).
# These are the ONLY non-empty ClosingOdds values the automation will re-scan
# and overwrite. A real odds value, "VOID", or "N/A" is final -- never
# re-fetched or overwritten. Re-scanning lets a row recover automatically once
# the underlying cause is fixed (a corrected team name, a registered book, or a
# sport coming back into season) without having to clear the cell by hand.
CLOSING_ODDS_ERROR_CODES = {
    CLOSING_ODDS_GAME_NOT_FOUND,
    CLOSING_ODDS_BOOK_NOT_FOUND,
    CLOSING_ODDS_SELECTION_NOT_FOUND,
    CLOSING_ODDS_MANUAL_REQUIRED,
    CLOSING_ODDS_SPORT_NOT_ON_API,
}

# ── Game status values ────────────────────────────────────────────
# Contract that any upstream game-result source (ESPN, Odds API, etc.)
# must translate its own status fields into before passing a game dict
# to resolver.resolve(). Keeps resolver.py independent of any one
# data source's specific status vocabulary.
GAME_STATUS_FINAL     = "final"      # game completed normally, scores are official
GAME_STATUS_CANCELLED = "cancelled"  # game was cancelled or postponed and will not be played

# ── Polling settings ──────────────────────────────────────────────
POLL_INTERVAL_SECONDS = 1800       # 30 minutes between polls
POLL_START_BUFFER_SECONDS = 1800   # start polling 30 min after scheduled game start
POLL_MAX_DURATION_SECONDS = 21600  # give up after 6 hours, write NEEDS_REVIEW

# ── ESPN API ──────────────────────────────────────────────────────
ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports"

# NOTE: this is no longer used by poll_bet() (which is Odds-API-only as of
# today) or by sheets_reader.py (load_sport_map() was removed -- it read a
# sport->ESPN-league mapping from the "Name References" sheet that nothing
# calls anymore). ESPN is still used via odds-tool's
# /api/bet-review/check-status endpoint (notification bell), which tries
# Odds API scores first and falls back to ESPN for cancellations.


# ════════════════════════════════════════════════════════════════════
# ── Promotions tab (Promotion Updater) ──────────────────────────────
# ════════════════════════════════════════════════════════════════════
# Read by HEADER NAME, not a fixed COL index dict -- matches the existing
# convention already established in sheets_reader.py's
# get_promo_boost_percentage() ("nothing else in this project assumes a
# fixed column layout for the Promotions tab"). This dict exists only to
# avoid retyping the literal header strings at every call site; it is
# NOT a positional index map like Bets' BET_COL dict above.
#
# Schema (17 columns, confirmed against the live sheet 2026-06-23):
# Promo ID | Book | Promo Name | Promo Type | Boost % | Reward |
# Qualifying Cost | Bonus Amount | Status | Realized Date |
# Realized Amount | Notes | Expiration Date/Time | Expected Reward Count |
# Reward Timing | Token Usage Window (days) | Start Date
#
# Columns 13-16 were added to support the automated Promotion Updater's
# multi-grant model (Bonus Bet/Profit Boost) -- see PromotionWizard.jsx for
# the per-promo-type applicability rules these encode. Start Date was added
# later, specifically for Insurance Bet's multi-day variant ("one insured
# bet/day for N days") -- see promo_resolver.py's
# _evaluate_multi_day_insurance.
PROMO_COL = {
    "promo_id":              "Promo ID",
    "book":                  "Book",
    "promo_name":            "Promo Name",
    "promo_type":            "Promo Type",
    "boost_pct":             "Boost %",
    "reward":                "Reward",
    "qualifying_cost":       "Qualifying Cost",
    "bonus_amount":          "Bonus Amount",
    "status":                "Status",
    "realized_date":         "Realized Date",
    "realized_amount":       "Realized Amount",
    "notes":                 "Notes",
    "expiration_date":       "Expiration Date/Time",
    "expected_reward_count": "Expected Reward Count",
    "reward_timing":         "Reward Timing",
    "token_usage_window":    "Token Usage Window (days)",
    # Anchors a multi-day Insurance Bet promo (e.g. "one insured bet/day for
    # 10 days") -- Expected Reward Count holds the number of days, this
    # holds day 1's date, so day N = Start Date + (N-1). Blank/unused for
    # every other promo type, and for a single-shot Insurance Bet promo.
    "start_date":            "Start Date",
    # The original (pre-boost) American odds for an Odds Boost promo -- the
    # baseline the boosted line is measured against. Blank/unused for every
    # other promo type. See evaluate_odds_boost_promo in promo_resolver.py.
    "original_odds":         "Original Odds",
}

PROMOTIONS_TAB = "Promotions"

# ── Promotion Status values ─────────────────────────────────────────
PROMO_STATUS_PENDING  = "Pending"
PROMO_STATUS_REALIZED = "Realized"
# Distinct from a $0 Realized promo: Unused means the qualifying window
# expired with ZERO qualifying activity ever linked -- "I forgot this
# existed," not "I did it and it paid nothing." Confirmed as a required
# distinction during the Promotion Updater design conversation
# (2026-06-21).
PROMO_STATUS_UNUSED   = "Unused"

# ── Promo Type values ───────────────────────────────────────────────
# Deliberately the SAME string values as the corresponding
# BET_CATEGORY_* constants above (Promo Type on a Promotions row and Bet
# Category on its linked Bets rows are written identically by design --
# e.g. a "Bonus Bet" promo's reward bets are logged with
# Bet Category = "Bonus Bet"). Aliased here under PROMO_TYPE_* names
# purely for readability at Promotion Updater call sites, not because
# the values actually differ.
PROMO_TYPE_BONUS_BET     = BET_CATEGORY_BONUS_BET
PROMO_TYPE_DEPOSIT_BONUS = BET_CATEGORY_DEPOSIT_BONUS
PROMO_TYPE_PROFIT_BOOST  = BET_CATEGORY_PROFIT_BOOST
PROMO_TYPE_INSURANCE_BET = BET_CATEGORY_INSURANCE_BET
# Odds Boost promo: a single boosted line, used once, with a max stake. Its
# linked reward bet carries Bet Category = "Odds Boost" and is placed at the
# boosted odds; the promo row stores the ORIGINAL (pre-boost) odds so the
# realized value can be computed as the P/L delta. See
# evaluate_odds_boost_promo in promo_resolver.py.
PROMO_TYPE_ODDS_BOOST    = BET_CATEGORY_ODDS_BOOST

# Unlike the aliases above, this Promo Type has NO matching Bet Category of
# its own -- each day's boosted bet is still logged with
# Bet Category = "Profit Boost" (BET_CATEGORY_PROFIT_BOOST). This Promo Type
# only distinguishes the promo's lifecycle shape (open-ended daily reissue
# on every loss, terminating on the first win, no fixed count or
# expiration) from regular "Profit Boost" promos (a bounded multi-grant
# token pool). See _evaluate_daily_profit_boost_until_win in
# promo_resolver.py.
PROMO_TYPE_PROFIT_BOOST_DAILY = "Profit Boost (Daily Until Win)"

# Promo types with a multi-grant token model (qualifying window,
# Expected Reward Count, Reward Timing, per-token Usage Window) --
# Bonus Bet and Profit Boost share this entire machinery, differing only
# in how a claimed token's value is computed (see promo_resolver.py).
#
# Insurance Bet also supports a multi-day variant (Expected Reward Count +
# Start Date = "one insured bet/day for N days") but deliberately does NOT
# belong in this set -- it's day-anchored, not a FIFO pool of qualifying
# bets, and each grant can branch into a Leg 2 refund claim depending on
# Leg 1's own outcome. It has its own dedicated evaluator in
# promo_resolver.py (_evaluate_multi_day_insurance), not
# _evaluate_multi_grant_promo.
MULTI_GRANT_PROMO_TYPES = {PROMO_TYPE_BONUS_BET, PROMO_TYPE_PROFIT_BOOST}

# ── Reward Timing values ────────────────────────────────────────────
# Matches PromotionWizard.jsx's REWARD_TIMING_OPTIONS exactly.
REWARD_TIMING_PER_QUALIFYING_BET = "Per Qualifying Bet"
REWARD_TIMING_END_OF_WINDOW      = "End of Window"
REWARD_TIMING_GRANTED            = "Granted"
