import gspread
from google.oauth2.service_account import Credentials
from config import SHEET_ID, get_credentials_info

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]

def _get_client():
    creds_info = get_credentials_info()
    creds = Credentials.from_service_account_info(creds_info, scopes=SCOPES)
    return gspread.authorize(creds)


def load_pending_bets(tab_name: str, col: dict) -> list[dict]:
    """
    Reads the Bets tab and returns all rows where:
      - Result column is blank
      - Bet type is in the automated set
      - Game date/time is in the past (checked by the poller, not here)

    Returns a list of dicts keyed by column name.
    """
    from config import AUTOMATED_BET_TYPES

    client = _get_client()
    sheet = client.open_by_key(SHEET_ID)
    tab = sheet.worksheet(tab_name)

    rows = tab.get_all_values()

    if not rows:
        return []

    pending = []
    for row_idx, row in enumerate(rows[1:], start=2):  # row 1 is header
        # Pad row in case trailing blank cells are omitted by Sheets API
        while len(row) <= max(col.values()):
            row.append("")

        result = row[col["result"]].strip()
        bet_type = row[col["bet_type"]].strip()

        if result:
            continue  # already resolved

        if bet_type not in AUTOMATED_BET_TYPES:
            continue  # parlay, prop — skip

        pending.append({
            "row_idx":     row_idx,
            "bet_id":      row[col["bet_id"]].strip(),
            "sport":       row[col["sport"]].strip(),
            "book":        row[col["book"]].strip(),
            "team1":       row[col["team1"]].strip(),
            "team2":       row[col["team2"]].strip(),
            "game_date":   row[col["game_date"]].strip(),
            "game_start":  row[col["game_start"]].strip(),
            "selection":   row[col["selection"]].strip(),
            "bet_type":    bet_type,
            "odds_taken":  row[col["odds_taken"]].strip(),
            "stake":       row[col["stake"]].strip(),
            "fee":         row[col["fee"]].strip(),
            "bet_category": row[col["bet_category"]].strip(),
            "promo_id":    row[col["promo_id"]].strip(),
        })

    return pending


def load_unresolved_pl_bets(tab_name: str, col: dict) -> list[dict]:
    """
    Reads the Bets tab and returns all rows where:
      - Result is already filled in (WIN/LOSS/PUSH/VOID -- a real outcome,
        not NEEDS_REVIEW or blank)
      - P/L AND Payout are BOTH currently blank

    This covers any row where a result was set without going through
    poll_bet() -- most commonly a manual fix (e.g. resolving a
    NEEDS_REVIEW bet by hand after checking ESPN yourself) where the
    person filled in Result but didn't compute P/L/Payout themselves.

    Deliberately does NOT restrict by bet type the way load_pending_bets()
    does for AUTOMATED_BET_TYPES -- confirmed against real Parlay data
    (BetID 11) that calculate_pl_and_payout() only needs OddsTaken/Stake/
    Fee to already be correct; it doesn't care whether those numbers
    represent one leg or a parlay's combined odds. Determining WHICH
    outcome won is a separate problem this tool never touches, since
    Result is already given by the time a row reaches this function.

    Only fills genuine gaps -- if a row already has a P/L or Payout value
    (even one that might be wrong by today's rules), it's left alone,
    never overwritten. This intentionally mirrors load_pending_bets()'s
    "don't touch what's already settled" caution.

    Returns a list of dicts keyed by column name, same shape as
    load_pending_bets()'s output, so it can be passed straight into
    poller._safe_calculate_pl_payout() without any reshaping.
    """
    client = _get_client()
    sheet = client.open_by_key(SHEET_ID)
    tab = sheet.worksheet(tab_name)

    rows = tab.get_all_values()

    if not rows:
        return []

    unresolved = []
    for row_idx, row in enumerate(rows[1:], start=2):  # row 1 is header
        while len(row) <= max(col.values()):
            row.append("")

        result = row[col["result"]].strip()
        pl = row[col["pl"]].strip()
        payout = row[col["payout"]].strip()

        if not result:
            continue  # no result yet -- this is load_pending_bets()'s job, not this one
        if result == "NEEDS_REVIEW":
            continue  # not a real outcome yet, nothing to compute
        if pl or payout:
            continue  # already has at least one value -- never overwrite

        unresolved.append({
            "row_idx":     row_idx,
            "bet_id":      row[col["bet_id"]].strip(),
            "sport":       row[col["sport"]].strip(),
            "book":        row[col["book"]].strip(),
            "team1":       row[col["team1"]].strip(),
            "team2":       row[col["team2"]].strip(),
            "game_date":   row[col["game_date"]].strip(),
            "game_start":  row[col["game_start"]].strip(),
            "selection":   row[col["selection"]].strip(),
            "bet_type":    row[col["bet_type"]].strip(),
            "odds_taken":  row[col["odds_taken"]].strip(),
            "stake":       row[col["stake"]].strip(),
            "fee":         row[col["fee"]].strip(),
            "bet_category": row[col["bet_category"]].strip(),
            "promo_id":    row[col["promo_id"]].strip(),
            "result":      result,
        })

    return unresolved


def get_book_fee_before_odds(book: str) -> bool:
    """
    Looks up whether `book` deducts its per-bet fee from the stake BEFORE
    profit/payout is calculated from odds, from the "Book Settings" tab
    (Book | Refunds Fee On Void | Fee Before Odds).

    Confirmed by reconciling two real Polymarket bets against Polymarket's
    own quoted "to win" figures on 2026-06-20: Polymarket computes profit
    using (stake - fee) as the effective wagered amount, not the full
    stake, with no separate fee subtraction afterward (unlike traditional
    sportsbooks, where the fee is a flat pass-through charge layered on
    top of normal odds math -- confirmed for Caesars via Illinois Gaming
    Board's wager-tax FAQ). This is a genuinely different payout mechanic,
    not a minor adjustment, so it's modeled as its own per-book flag
    rather than folded into the existing fee-on-void column.

    Reads fresh every call, same as get_promo_boost_percentage and the
    fee-on-void check in sheets_writer.py -- no caching, since correctness
    matters far more than the small latency cost for what should be a
    rare, deliberate lookup (once per book, effectively, since this tab
    rarely changes).

    Returns False (the traditional-sportsbook default) if the book isn't
    found in the tab, or the column doesn't exist yet -- conservative,
    since assuming the WRONG mechanic would silently corrupt P/L, and the
    wizard's Step 2 prompt is responsible for ensuring every book actually
    used gets a real answer recorded here before being usable.
    """
    client = _get_client()
    sheet = client.open_by_key(SHEET_ID)
    tab = sheet.worksheet("Book Settings")

    rows = tab.get_all_values()
    if not rows:
        return False

    headers = rows[0]
    try:
        idx_book = headers.index("Book")
        idx_flag = headers.index("Fee Before Odds")
    except ValueError:
        return False

    for row in rows[1:]:
        if len(row) <= max(idx_book, idx_flag):
            continue
        if row[idx_book].strip().lower() == book.strip().lower():
            return row[idx_flag].strip().upper() == "TRUE"

    return False


def get_promo_boost_percentage(promo_id: str) -> float | None:
    """
    Looks up the Boost % for a given Promo ID from the Promotions tab.
    Reads fresh every call -- no caching -- since this is only ever called
    for the rare case of resolving a winning Profit Boost bet, where
    correctness matters more than the small added latency of a live read.

    Reads by header name rather than a fixed column index (unlike
    load_pending_bets' Bets-tab reads), since nothing else in this project
    assumes a fixed column layout for the Promotions tab -- the Node side
    (server.js) already reads it the same way.

    Returns the boost percentage as a float (e.g. 100.0 for "100% Profit
    Boost"), or None if the promo isn't found or has no Boost % set --
    callers should treat None as "cannot resolve, do not guess."
    """
    client = _get_client()
    sheet = client.open_by_key(SHEET_ID)
    tab = sheet.worksheet("Promotions")

    rows = tab.get_all_values()
    if not rows:
        return None

    headers = rows[0]
    try:
        idx_promo_id = headers.index("Promo ID")
        idx_boost_pct = headers.index("Boost %")
    except ValueError:
        # "Boost %" column doesn't exist yet in the sheet
        return None

    for row in rows[1:]:
        if len(row) <= max(idx_promo_id, idx_boost_pct):
            continue
        if row[idx_promo_id].strip() == str(promo_id).strip():
            raw = row[idx_boost_pct].strip()
            if not raw:
                return None
            try:
                return float(raw.replace("%", "").strip())
            except ValueError:
                return None

    return None


# ════════════════════════════════════════════════════════════════════
# ── Promotion Updater reads ─────────────────────────────────────────
# ════════════════════════════════════════════════════════════════════

def load_pending_promotions() -> list[dict]:
    """
    Reads the Promotions tab and returns every row where Status is
    currently "Pending" -- the only rows the Promotion Updater needs to
    look at, since Realized/Unused are terminal and (per the established
    "never overwrite an existing value" convention used throughout this
    project) are never revisited.

    Reads by HEADER NAME via config.PROMO_COL, matching the existing
    get_promo_boost_percentage() convention -- the Promotions tab's
    column layout is not assumed fixed, unlike the Bets tab's COL dict.

    Returns a list of dicts keyed by the same logical names as
    config.PROMO_COL (promo_id, book, promo_type, expiration_date, etc.),
    plus "row_idx" (1-based, for writing back later). Numeric/date
    fields are returned as raw stripped strings -- parsing into actual
    numbers/dates is promo_resolver.py's job, kept separate so this
    function stays a thin, honest read with no business logic.
    """
    from config import PROMOTIONS_TAB, PROMO_COL, PROMO_STATUS_PENDING

    client = _get_client()
    sheet = client.open_by_key(SHEET_ID)
    tab = sheet.worksheet(PROMOTIONS_TAB)

    rows = tab.get_all_values()
    if not rows:
        return []

    headers = rows[0]
    col_idx = {}
    for key, header_name in PROMO_COL.items():
        try:
            col_idx[key] = headers.index(header_name)
        except ValueError:
            # Column doesn't exist in the live sheet yet -- leave it out
            # of col_idx rather than failing the whole read. Code further
            # down the pipeline (promo_resolver.py) treats a missing key
            # the same as a blank value for that field.
            print(f"[sheets_reader] ⚠️  Promotions tab is missing expected "
                  f"column '{header_name}' -- continuing without it.")

    if "promo_id" not in col_idx or "status" not in col_idx:
        raise RuntimeError(
            "[sheets_reader] Promotions tab is missing 'Promo ID' or 'Status' "
            "column entirely -- cannot proceed."
        )

    def cell(row, key):
        idx = col_idx.get(key)
        if idx is None or idx >= len(row):
            return ""
        return row[idx].strip()

    pending = []
    for row_idx, row in enumerate(rows[1:], start=2):  # row 1 is header
        status = cell(row, "status")
        if status != PROMO_STATUS_PENDING:
            continue

        pending.append({
            "row_idx":              row_idx,
            "promo_id":             cell(row, "promo_id"),
            "book":                 cell(row, "book"),
            "promo_name":           cell(row, "promo_name"),
            "promo_type":           cell(row, "promo_type"),
            "boost_pct":            cell(row, "boost_pct"),
            "reward":               cell(row, "reward"),
            "qualifying_cost":      cell(row, "qualifying_cost"),
            "bonus_amount":         cell(row, "bonus_amount"),
            "status":               status,
            "notes":                cell(row, "notes"),
            "expiration_date":      cell(row, "expiration_date"),
            "expected_reward_count":cell(row, "expected_reward_count"),
            "reward_timing":        cell(row, "reward_timing"),
            "token_usage_window":   cell(row, "token_usage_window"),
        })

    return pending


def load_bets_by_promo_id(tab_name: str, col: dict) -> dict[str, list[dict]]:
    """
    Reads the ENTIRE Bets tab once and groups every row that has a
    non-blank Promo ID, keyed by that Promo ID. Built for the Promotion
    Updater, which needs to evaluate potentially many Pending promos per
    run -- reading the whole tab once and grouping in memory is far
    cheaper than a separate Sheets API read per promo.

    Unlike load_pending_bets(), this is NOT filtered by Result, Bet Type,
    or anything else -- it returns every linked row in whatever state
    it's currently in (settled or not), since the Promotion Updater's
    logic (promo_resolver.py) needs to see unsettled reward bets too, to
    know it must keep waiting rather than finalize prematurely.

    Returns: {promo_id: [bet_dict, ...]}, each bet_dict containing every
    column this project's Bets tab has, keyed by the same names
    load_pending_bets() uses, plus "result"/"pl"/"payout" (always
    included here, unlike load_pending_bets() which omits them since
    they're guaranteed blank there).
    """
    client = _get_client()
    sheet = client.open_by_key(SHEET_ID)
    tab = sheet.worksheet(tab_name)

    rows = tab.get_all_values()
    if not rows:
        return {}

    grouped: dict[str, list[dict]] = {}

    for row_idx, row in enumerate(rows[1:], start=2):  # row 1 is header
        while len(row) <= max(col.values()):
            row.append("")

        promo_id = row[col["promo_id"]].strip()
        if not promo_id:
            continue

        bet = {
            "row_idx":      row_idx,
            "bet_id":       row[col["bet_id"]].strip(),
            "date_placed":  row[col["date_placed"]].strip(),
            "book":         row[col["book"]].strip(),
            "sport":        row[col["sport"]].strip(),
            "stake":        row[col["stake"]].strip(),
            "fee":          row[col["fee"]].strip(),
            "bet_category": row[col["bet_category"]].strip(),
            "promo_id":     promo_id,
            "result":       row[col["result"]].strip(),
            "payout":       row[col["payout"]].strip(),
            "pl":           row[col["pl"]].strip(),
            "odds_taken":   row[col["odds_taken"]].strip(),
        }

        grouped.setdefault(promo_id, []).append(bet)

    return grouped
