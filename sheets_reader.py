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