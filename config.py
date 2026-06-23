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
#         P/L | Running P/L | Notes
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
    "result":          "Result",
    "payout":          "Payout",
    "pl":              "P/L",
    "running_pl":      "Running P/L",
    "notes":           "Notes",
}

# ── Bet types ─────────────────────────────────────────────────────
# Values as they appear in your Sheet's Bet Type column
BET_TYPE_SPREAD     = "Spread"
BET_TYPE_MONEYLINE  = "Moneyline"
BET_TYPE_TOTAL      = "Total"
BET_TYPE_DRAW       = "Draw"
BET_TYPE_PARLAY     = "Parlay"
BET_TYPE_PROP       = "Prop"

# Bet types this tool will resolve automatically
AUTOMATED_BET_TYPES = {BET_TYPE_SPREAD, BET_TYPE_MONEYLINE, BET_TYPE_TOTAL, BET_TYPE_DRAW}

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
                          BET_CATEGORY_PROFIT_BOOST, BET_CATEGORY_INSURANCE_BET}

# ── Result values ─────────────────────────────────────────────────
RESULT_WIN   = "WIN"
RESULT_LOSS  = "LOSS"
RESULT_PUSH  = "PUSH"
RESULT_VOID  = "VOID"        # written when a game is cancelled/postponed and never played
RESULT_NEEDS_REVIEW = "NEEDS_REVIEW"  # written when Odds API never returned a final
                                       # score well past game time -- distinct from
                                       # PENDING: this specifically means "check ESPN
                                       # for cancellation via the Stats page" before
                                       # falling back to full manual review. See the
                                       # Log Bet Wizard's Step 3 design notes -- ESPN is
                                       # only ever consulted here, on this rare path, via
                                       # a human-confirmed live lookup, never automatically
                                       # and never via a cached/stored sport->league mapping.
RESULT_PENDING = "PENDING"  # written when a NEEDS_REVIEW check finds nothing useful
                             # (or is skipped) -- fully manual review from here

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
POLL_MAX_DURATION_SECONDS = 21600  # give up after 6 hours, write PENDING

# ── ESPN API ──────────────────────────────────────────────────────
ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports"

# NOTE: this is no longer used by poll_bet() (which is Odds-API-only as of
# today) or by sheets_reader.py (load_sport_map() was removed -- it read a
# sport->ESPN-league mapping from the "Name References" sheet that nothing
# calls anymore). ESPN is still used, but only via server.js's
# /api/bet-review/check-espn endpoint for the Stats page's manual review
# flow, which does live league discovery per-request and never reads a
# stored mapping from this sheet or anywhere else.


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
# Schema (16 columns, confirmed against the live sheet 2026-06-21):
# Promo ID | Book | Promo Name | Promo Type | Boost % | Reward |
# Qualifying Cost | Bonus Amount | Status | Realized Date |
# Realized Amount | Notes | Expiration Date/Time | Expected Reward Count |
# Reward Timing | Token Usage Window (days)
#
# The last 4 columns were added specifically to support the automated
# Promotion Updater -- see PromotionWizard.jsx (the wizard that writes
# new rows) for the per-promo-type applicability rules these encode.
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

# Promo types with a multi-grant token model (qualifying window,
# Expected Reward Count, Reward Timing, per-token Usage Window) --
# Bonus Bet and Profit Boost share this entire machinery, differing only
# in how a claimed token's value is computed (see promo_resolver.py).
MULTI_GRANT_PROMO_TYPES = {PROMO_TYPE_BONUS_BET, PROMO_TYPE_PROFIT_BOOST}

# ── Reward Timing values ────────────────────────────────────────────
# Matches PromotionWizard.jsx's REWARD_TIMING_OPTIONS exactly.
REWARD_TIMING_PER_QUALIFYING_BET = "Per Qualifying Bet"
REWARD_TIMING_END_OF_WINDOW      = "End of Window"
