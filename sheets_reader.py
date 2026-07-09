import gspread
from google.oauth2.service_account import Credentials
from config import SHEET_ID, get_credentials_info

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]

_client_cache = None
_spreadsheet_cache = None
_tab_cache = {}
_bets_rows_cache: dict[str, list[list[str]]] = {}
_missing_col_warnings: set[str] = set()
_book_settings_rows_cache = None
_promotions_rows_cache = None


def _get_book_settings_rows() -> list:
    """Process-lifetime cache for Book Settings tab rows. Safe because each
    GitHub Actions run is a fresh process and Book Settings doesn't change
    mid-run."""
    global _book_settings_rows_cache
    if _book_settings_rows_cache is None:
        _book_settings_rows_cache = _get_tab("Book Settings").get_all_values()
    return _book_settings_rows_cache


def _get_promotions_rows() -> list:
    """Process-lifetime cache for Promotions tab rows. Safe because each
    GitHub Actions run is a fresh process and Promotions data doesn't change
    mid-run."""
    global _promotions_rows_cache
    if _promotions_rows_cache is None:
        _promotions_rows_cache = _get_tab("Promotions").get_all_values()
    return _promotions_rows_cache


def _get_client():
    """
    Cached for the lifetime of the current process (each scheduled run is
    a fresh process) -- see sheets_writer._get_client()'s docstring for
    why this matters: re-authenticating on every single read call was
    pure overhead.
    """
    global _client_cache
    if _client_cache is None:
        creds_info = get_credentials_info()
        creds = Credentials.from_service_account_info(creds_info, scopes=SCOPES)
        _client_cache = gspread.authorize(creds)
    return _client_cache


def _get_spreadsheet():
    """
    Cached for the lifetime of the current process -- client.open_by_key()
    is a real network "read request" against the Sheets API quota every
    time it's called. Every load_* function in this module used to call
    it independently on every invocation; across a single trigger.py run
    (load_pending_bets + load_unresolved_pl_bets + load_manual_payout_
    pending_pl_bets, plus several sheets_writer.py calls per bet) that
    redundancy was enough to trip 'Quota exceeded for Read requests per
    minute per user' (confirmed 2026-06-26).
    """
    global _spreadsheet_cache
    if _spreadsheet_cache is None:
        _spreadsheet_cache = _get_client().open_by_key(SHEET_ID)
    return _spreadsheet_cache


def _get_tab(tab_name: str):
    """
    Cached per tab name for the lifetime of the current process -- same
    reasoning as _get_spreadsheet(), one fewer redundant network call
    per repeat read of the same tab within a single run.
    """
    if tab_name not in _tab_cache:
        _tab_cache[tab_name] = _get_spreadsheet().worksheet(tab_name)
    return _tab_cache[tab_name]


def _resolve_promotions_headers_from_rows(rows: list[list[str]]) -> tuple[int, list[str]]:
    """
    Finds the Promotions header row by scanning for 'Promo ID'. Row 1 may be
    blank if a spacer row was inserted above headers during a manual reorder.
    Returns (0-based header row index, header cell list).
    """
    header_row_idx = 0
    for i, row in enumerate(rows[:10]):
        if "Promo ID" in [c.strip() for c in row]:
            header_row_idx = i
            break

    headers = rows[header_row_idx] if rows else []

    if header_row_idx > 0:
        print(f"[sheets_reader] Promotions header row detected on sheet row "
              f"{header_row_idx + 1} (row 1 is blank or not the header row).")

    return header_row_idx, headers


def _get_bets_rows(tab_name: str) -> list[list[str]]:
    """One Bets-tab read per process per tab — trigger.py calls four load_*
    functions per run; without this cache that was four identical get_all_values()
    calls hitting the Sheets read quota."""
    if tab_name not in _bets_rows_cache:
        _bets_rows_cache[tab_name] = _get_tab(tab_name).get_all_values()
    return _bets_rows_cache[tab_name]


def _resolve_bet_col_indices(headers: list[str]) -> dict[str, int | None]:
    """
    Maps config.BET_COL logical keys to 0-based column indices against the
    live Bets tab header row. Missing headers are omitted (None) rather
    than failing the whole read.
    """
    from config import BET_COL

    col_idx: dict[str, int | None] = {}
    for key, header_name in BET_COL.items():
        try:
            col_idx[key] = headers.index(header_name)
        except ValueError:
            col_idx[key] = None
            if header_name not in _missing_col_warnings:
                _missing_col_warnings.add(header_name)
                print(f"[sheets_reader] ⚠️  Bets tab is missing optional "
                      f"column '{header_name}' -- continuing without it.")
    return col_idx


def _pad_bet_row(row: list, col_idx: dict[str, int | None]) -> list:
    valid = [i for i in col_idx.values() if i is not None]
    if not valid:
        return row
    while len(row) <= max(valid):
        row.append("")
    return row


def _bet_cell(row: list, col_idx: dict, key: str) -> str:
    idx = col_idx.get(key)
    if idx is None or idx >= len(row):
        return ""
    return row[idx].strip()


DUPLICATE_BET_ID_PREFIX = "⚠ Duplicate BetID:"


def _duplicate_bet_id_row_map(rows: list[list[str]], col: dict[str, int | None]) -> dict[str, list[int]]:
    """Maps BetID values that appear on more than one row to their 1-based sheet row numbers."""
    if col.get("bet_id") is None:
        return {}

    grouped: dict[str, list[int]] = {}
    for row_idx, row in enumerate(rows[1:], start=2):
        row = _pad_bet_row(row, col)
        bet_id = _bet_cell(row, col, "bet_id")
        if not bet_id:
            continue
        grouped.setdefault(bet_id, []).append(row_idx)

    return {bet_id: row_idxs for bet_id, row_idxs in grouped.items() if len(row_idxs) > 1}


def _duplicate_bet_ids(rows: list[list[str]], col: dict[str, int | None]) -> set[str]:
    return set(_duplicate_bet_id_row_map(rows, col).keys())


def find_duplicate_bet_id_rows(tab_name: str) -> list[dict]:
    """
    Returns every Bets-tab row whose BetID appears more than once on the sheet.
    Used by trigger.py to flag rows and exclude them from automated settlement.
    """
    rows = _get_bets_rows(tab_name)
    if not rows:
        return []

    headers = rows[0]
    col = _resolve_bet_col_indices(headers)
    if col.get("bet_id") is None:
        return []

    dup_map = _duplicate_bet_id_row_map(rows, col)
    if not dup_map:
        return []

    flagged = []
    for bet_id, row_idxs in dup_map.items():
        for row_idx in row_idxs:
            flagged.append({
                "row_idx": row_idx,
                "bet_id": bet_id,
                "other_row_idxs": [i for i in row_idxs if i != row_idx],
            })
    return flagged


def load_pending_bets(tab_name: str) -> list[dict]:
    """
    Reads the Bets tab and returns all rows where:
      - Result column is blank
      - Bet type is in the automated set
      - Game date/time is in the past (checked by the poller, not here)

    Returns a list of dicts keyed by column name.
    """
    from config import AUTOMATED_BET_TYPES, BET_TYPE_PARLAY, RESULT_NEEDS_REVIEW
    from parlay import parse_legs, all_legs_automatable

    rows = _get_bets_rows(tab_name)

    if not rows:
        return []

    headers = rows[0]
    col = _resolve_bet_col_indices(headers)
    if col.get("result") is None or col.get("bet_id") is None:
        raise RuntimeError(
            "[sheets_reader] Bets tab is missing 'BetID' or 'Result' "
            "column entirely -- cannot proceed."
        )

    duplicate_ids = _duplicate_bet_ids(rows, col)

    pending = []
    for row_idx, row in enumerate(rows[1:], start=2):  # row 1 is header
        row = _pad_bet_row(row, col)

        bet_id = _bet_cell(row, col, "bet_id")
        if bet_id in duplicate_ids:
            continue  # flagged by trigger.py; never auto-settle duplicate IDs

        result = _bet_cell(row, col, "result")
        bet_type = _bet_cell(row, col, "bet_type")

        # Re-scan NEEDS_REVIEW rows -- a final score may be available now (e.g.
        # a game that reported late, or a tennis match now gradable via ESPN).
        # A real WIN/LOSS/PUSH/VOID or a manual PENDING is left untouched;
        # write_result() also refuses to overwrite anything but NEEDS_REVIEW.
        if result and result != RESULT_NEEDS_REVIEW:
            continue  # already resolved (or PENDING / manual)

        is_parlay = False
        legs = []
        if bet_type == BET_TYPE_PARLAY:
            # A parlay auto-resolves only if EVERY leg is an automatable type
            # with a lookup-able game. Otherwise leave it for manual Result
            # entry (calculate_pl_and_payout still settles it from the stored
            # combined odds once a human types the Result).
            legs = parse_legs(_bet_cell(row, col, "legs"))
            if not all_legs_automatable(legs):
                continue
            is_parlay = True
        elif bet_type not in AUTOMATED_BET_TYPES:
            continue  # prop, or a parlay with a non-automatable leg — skip

        pending.append({
            "row_idx":     row_idx,
            "bet_id":      _bet_cell(row, col, "bet_id"),
            "sport":       _bet_cell(row, col, "sport"),
            "book":        _bet_cell(row, col, "book"),
            "team1":       _bet_cell(row, col, "team1"),
            "team2":       _bet_cell(row, col, "team2"),
            "game_date":   _bet_cell(row, col, "game_date"),
            "game_start":  _bet_cell(row, col, "game_start"),
            "selection":   _bet_cell(row, col, "selection"),
            "bet_type":    bet_type,
            "odds_taken":  _bet_cell(row, col, "odds_taken"),
            "stake":       _bet_cell(row, col, "stake"),
            "fee":         _bet_cell(row, col, "fee"),
            "bet_category": _bet_cell(row, col, "bet_category"),
            "promo_id":    _bet_cell(row, col, "promo_id"),
            "is_parlay":   is_parlay,
            "legs":        legs,
            # Kalshi market ticker (blank for non-Kalshi) -- lets poll_bet settle
            # from Kalshi's own market resolution before the score-based path.
            "kalshi_ticker": _bet_cell(row, col, "kalshi_ticker"),
        })

    return pending


def load_unresolved_pl_bets(tab_name: str) -> list[dict]:
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
    rows = _get_bets_rows(tab_name)

    if not rows:
        return []

    headers = rows[0]
    col = _resolve_bet_col_indices(headers)
    if col.get("result") is None or col.get("bet_id") is None:
        raise RuntimeError(
            "[sheets_reader] Bets tab is missing 'BetID' or 'Result' "
            "column entirely -- cannot proceed."
        )

    duplicate_ids = _duplicate_bet_ids(rows, col)

    unresolved = []
    for row_idx, row in enumerate(rows[1:], start=2):  # row 1 is header
        row = _pad_bet_row(row, col)

        bet_id = _bet_cell(row, col, "bet_id")
        if bet_id in duplicate_ids:
            continue

        result = _bet_cell(row, col, "result")
        pl = _bet_cell(row, col, "pl")
        payout = _bet_cell(row, col, "payout")

        if not result:
            continue  # no result yet -- this is load_pending_bets()'s job, not this one
        if result == "NEEDS_REVIEW":
            continue  # not a real outcome yet, nothing to compute
        if pl or payout:
            continue  # already has at least one value -- never overwrite

        unresolved.append({
            "row_idx":     row_idx,
            "bet_id":      _bet_cell(row, col, "bet_id"),
            "sport":       _bet_cell(row, col, "sport"),
            "book":        _bet_cell(row, col, "book"),
            "team1":       _bet_cell(row, col, "team1"),
            "team2":       _bet_cell(row, col, "team2"),
            "game_date":   _bet_cell(row, col, "game_date"),
            "game_start":  _bet_cell(row, col, "game_start"),
            "selection":   _bet_cell(row, col, "selection"),
            "bet_type":    _bet_cell(row, col, "bet_type"),
            "odds_taken":  _bet_cell(row, col, "odds_taken"),
            # Parlays carry their exact combined decimal here; complete_pl_payout
            # settles a manual parlay WIN from it instead of the rounded American.
            "decimal_odds": _bet_cell(row, col, "decimal_odds"),
            "stake":       _bet_cell(row, col, "stake"),
            "fee":         _bet_cell(row, col, "fee"),
            "bet_category": _bet_cell(row, col, "bet_category"),
            "promo_id":    _bet_cell(row, col, "promo_id"),
            "result":      result,
        })

    return unresolved


def load_manual_payout_pending_pl_bets(tab_name: str) -> list[dict]:
    """
    Reads the Bets tab and returns all rows where:
      - Result is already filled in (WIN/LOSS/PUSH/VOID -- a real outcome,
        not NEEDS_REVIEW or blank)
      - Payout is filled in, but P/L is currently blank

    This is the manual-entry counterpart to load_unresolved_pl_bets()
    (which only fires when BOTH P/L and Payout are blank): for books in
    config.MANUAL_PAYOUT_REQUIRED_BOOKS (e.g. Kalshi, whose contracts-
    based settlement can't be reliably derived from American odds --
    see resolver.derive_pl_from_payout's docstring), poller flags the row
    asking the user to type in the real Payout from their account. Once
    they do, THIS function is what notices Payout has shown up with P/L
    still blank, so poller.complete_manual_payout_pl() can derive P/L
    from it on the next scheduled run -- no separate "I filled it in"
    signal needed beyond the Payout cell itself being non-blank.

    Returns a dict shape compatible with poller.complete_manual_payout_pl(),
    including "payout" (unlike load_unresolved_pl_bets(), which never
    needs it since Payout is one of the two blank fields it's filling in).
    """
    rows = _get_bets_rows(tab_name)

    if not rows:
        return []

    headers = rows[0]
    col = _resolve_bet_col_indices(headers)
    if col.get("result") is None or col.get("bet_id") is None:
        raise RuntimeError(
            "[sheets_reader] Bets tab is missing 'BetID' or 'Result' "
            "column entirely -- cannot proceed."
        )

    duplicate_ids = _duplicate_bet_ids(rows, col)

    pending = []
    for row_idx, row in enumerate(rows[1:], start=2):  # row 1 is header
        row = _pad_bet_row(row, col)

        bet_id = _bet_cell(row, col, "bet_id")
        if bet_id in duplicate_ids:
            continue

        result = _bet_cell(row, col, "result")
        pl = _bet_cell(row, col, "pl")
        payout = _bet_cell(row, col, "payout")

        if not result or result == "NEEDS_REVIEW":
            continue
        if pl:
            continue  # already has a P/L value -- never overwrite
        if not payout:
            continue  # nothing manually entered yet -- still waiting

        pending.append({
            "row_idx":      row_idx,
            "bet_id":       _bet_cell(row, col, "bet_id"),
            "book":         _bet_cell(row, col, "book"),
            "odds_taken":   _bet_cell(row, col, "odds_taken"),
            "stake":        _bet_cell(row, col, "stake"),
            "fee":          _bet_cell(row, col, "fee"),
            "bet_category": _bet_cell(row, col, "bet_category"),
            "payout":       payout,
            "result":       result,
        })

    return pending


def load_bets_needing_closing_odds(tab_name: str) -> list[dict]:
    """
    Reads the Bets tab and returns all rows where:
      - ClosingOdds column is blank
      - Bet type is in AUTOMATED_BET_TYPES (Spread, Moneyline, Total, Draw)
      - Result is not NEEDS_REVIEW (those need human review first)

    Covers both pending and already-resolved bets — a WIN/LOSS bet still
    needs its closing odds captured for CLV analysis. Game-time filtering
    (only fetch for games that have already started) is done in trigger.py,
    same pattern as the pending-bets pass.

    Returns a list of dicts with the fields needed by closing_odds.fetch_closing_odds().
    """
    from config import (
        AUTOMATED_BET_TYPES, RESULT_NEEDS_REVIEW, BET_TYPE_PARLAY,
        CLOSING_ODDS_ERROR_CODES,
    )
    from closing_odds_exhaustion import is_closing_odds_exhausted
    from parlay import parse_legs, all_legs_automatable

    rows = _get_bets_rows(tab_name)

    if not rows:
        return []

    headers = rows[0]
    col = _resolve_bet_col_indices(headers)
    if col.get("bet_id") is None:
        raise RuntimeError(
            "[sheets_reader] Bets tab is missing 'BetID' column -- cannot proceed."
        )

    if col.get("closing_odds") is None:
        print("[sheets_reader] ⚠️  Bets tab is missing 'ClosingOdds' column -- "
              "closing odds pass will find nothing to do.")
        return []

    duplicate_ids = _duplicate_bet_ids(rows, col)

    bets = []
    for row_idx, row in enumerate(rows[1:], start=2):
        row = _pad_bet_row(row, col)

        bet_id = _bet_cell(row, col, "bet_id")
        if bet_id in duplicate_ids:
            continue

        bet_type = _bet_cell(row, col, "bet_type")

        is_parlay = False
        legs = []
        if bet_type == BET_TYPE_PARLAY:
            # Combined closing line = product of each leg's closing line, so
            # every leg must be auto-priceable. A parlay with a Prop/manual
            # leg gets no CLV (left blank), same as a single Prop bet.
            legs = parse_legs(_bet_cell(row, col, "legs"))
            if not all_legs_automatable(legs):
                continue
            is_parlay = True
        elif bet_type not in AUTOMATED_BET_TYPES:
            continue

        result = _bet_cell(row, col, "result")
        if result == RESULT_NEEDS_REVIEW:
            continue  # needs human review before closing odds are useful

        closing_odds = _bet_cell(row, col, "closing_odds")
        # Re-scan rows whose ClosingOdds holds a failure code (BOOK NOT FOUND,
        # SELECTION NOT FOUND, etc.) -- they may resolve now if the underlying
        # cause was fixed. A real odds value, "VOID", or "N/A" is final and is
        # never re-fetched.
        if closing_odds and closing_odds not in CLOSING_ODDS_ERROR_CODES:
            continue  # already captured (real value, VOID, or N/A)

        notes = _bet_cell(row, col, "notes")
        if is_closing_odds_exhausted(notes):
            continue

        bets.append({
            "row_idx":    row_idx,
            "bet_id":     _bet_cell(row, col, "bet_id"),
            "sport":      _bet_cell(row, col, "sport"),
            "book":       _bet_cell(row, col, "book"),
            "team1":      _bet_cell(row, col, "team1"),
            "team2":      _bet_cell(row, col, "team2"),
            "game_date":  _bet_cell(row, col, "game_date"),
            "game_start": _bet_cell(row, col, "game_start"),
            "selection":  _bet_cell(row, col, "selection"),
            "bet_type":   bet_type,
            "odds_taken": _bet_cell(row, col, "odds_taken"),
            "result":     result,
            "is_parlay":  is_parlay,
            "legs":       legs,
            # Kalshi market ticker (blank for non-Kalshi) -- lets closing_odds
            # pull the real Kalshi closing line instead of MANUAL ENTRY.
            "kalshi_ticker": _bet_cell(row, col, "kalshi_ticker"),
            # Odds API market key (blank for legacy rows).
            "market_key": _bet_cell(row, col, "market_key"),
        })

    return bets


def load_bets_for_closing_retry(
    tab_name: str,
    *,
    include_na: bool = True,
    include_errors: bool = False,
    bet_id: str | None = None,
) -> list[dict]:
    """
    Load bet rows eligible for the retry_closing_odds one-shot script.

    By default returns rows whose ClosingOdds is N/A. With include_errors,
    also includes rows holding a CLOSING_ODDS_ERROR_CODES value.
    """
    from config import CLOSING_ODDS_ERROR_CODES, BET_TYPE_PARLAY
    from parlay import parse_legs

    rows = _get_bets_rows(tab_name)
    if not rows:
        return []

    headers = rows[0]
    col = _resolve_bet_col_indices(headers)
    if col.get("bet_id") is None:
        raise RuntimeError(
            "[sheets_reader] Bets tab is missing 'BetID' column -- cannot proceed."
        )

    allowed_closing = set()
    if include_na:
        allowed_closing.add("N/A")
    if include_errors:
        allowed_closing |= CLOSING_ODDS_ERROR_CODES

    if not allowed_closing:
        return []

    target_id = str(bet_id).strip() if bet_id is not None else None
    duplicate_ids = _duplicate_bet_ids(rows, col)
    bets = []

    for row_idx, row in enumerate(rows[1:], start=2):
        row = _pad_bet_row(row, col)
        row_bet_id = _bet_cell(row, col, "bet_id")
        if row_bet_id in duplicate_ids:
            continue
        if target_id is not None and row_bet_id != target_id:
            continue

        closing_odds = _bet_cell(row, col, "closing_odds")
        if closing_odds not in allowed_closing:
            continue

        bet_type = _bet_cell(row, col, "bet_type")
        legs = []
        is_parlay = bet_type == BET_TYPE_PARLAY
        if is_parlay:
            legs = parse_legs(_bet_cell(row, col, "legs"))

        bets.append({
            "row_idx": row_idx,
            "bet_id": row_bet_id,
            "sport": _bet_cell(row, col, "sport"),
            "book": _bet_cell(row, col, "book"),
            "team1": _bet_cell(row, col, "team1"),
            "team2": _bet_cell(row, col, "team2"),
            "game_date": _bet_cell(row, col, "game_date"),
            "game_start": _bet_cell(row, col, "game_start"),
            "selection": _bet_cell(row, col, "selection"),
            "bet_type": bet_type,
            "odds_taken": _bet_cell(row, col, "odds_taken"),
            "result": _bet_cell(row, col, "result"),
            "notes": _bet_cell(row, col, "notes"),
            "closing_odds": closing_odds,
            "is_parlay": is_parlay,
            "legs": legs,
            "kalshi_ticker": _bet_cell(row, col, "kalshi_ticker"),
            "market_key": _bet_cell(row, col, "market_key"),
        })

    return bets


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

    Uses a process-lifetime cache for Book Settings rows -- safe because
    each GitHub Actions run is a fresh process and Book Settings doesn't
    change mid-run.

    Returns False (the traditional-sportsbook default) if the book isn't
    found in the tab, or the column doesn't exist yet -- conservative,
    since assuming the WRONG mechanic would silently corrupt P/L, and the
    wizard's Step 2 prompt is responsible for ensuring every book actually
    used gets a real answer recorded here before being usable.
    """
    rows = _get_book_settings_rows()
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


def get_book_fee_config(book: str) -> dict:
    """
    Looks up a book's fee TYPE and rate from the "Book Settings" tab
    (Book | ... | Fee Type | Fee Percent), added for ProphetX-style books
    whose fee is a percentage of NET PROFIT charged only on a win (never a
    loss, never a parlay -- see resolver.calculate_pl_and_payout's
    fee_pct_on_win_only parameter), rather than a flat dollar amount
    entered/known at logging time like every other book.     Also covers stake-on-win books (Fee Type =
    "Percent Of Stake On Win", config.BOOK_FEE_TYPE_PERCENT_OF_STAKE_ON_WIN)
    whose fee is a percentage of STAKE instead of profit. BetOpenly uses
    Percent Of Win Profit (1%) like ProphetX. This function returns whichever
    fee_type string is in the sheet,
    agnostic to which formula it maps to; poller.py decides that.

    Deliberately a SEPARATE function from get_book_fee_before_odds() above
    rather than folding this into it or replacing it -- those other call
    sites (promo_trigger.py, promo_resolver.py) only ever need the
    Fee Before Odds boolean, and giving them a dict they'd have to unpack
    for one field would be pure churn for no benefit.

    Returns {"fee_type": "", "fee_percent": None} (i.e. "use the normal
    flat-fee path") if the book isn't found, or either column doesn't
    exist yet -- same conservative-default reasoning as
    get_book_fee_before_odds(): an unrecognised fee type should never be
    silently treated as automated, since that would skip the "Fee is
    required" safety check in poller.py for a book that actually needs a
    manually-entered fee.
    """
    rows = _get_book_settings_rows()
    if not rows:
        return {"fee_type": "", "fee_percent": None}

    headers = rows[0]
    try:
        idx_book = headers.index("Book")
        idx_type = headers.index("Fee Type")
        idx_pct = headers.index("Fee Percent")
    except ValueError:
        return {"fee_type": "", "fee_percent": None}

    for row in rows[1:]:
        if len(row) <= max(idx_book, idx_type, idx_pct):
            continue
        if row[idx_book].strip().lower() == book.strip().lower():
            fee_type = row[idx_type].strip()
            try:
                fee_percent = float(row[idx_pct]) if row[idx_pct].strip() else None
            except ValueError:
                fee_percent = None
            return {"fee_type": fee_type, "fee_percent": fee_percent}

    return {"fee_type": "", "fee_percent": None}


def get_promo_boost_percentage(promo_id: str) -> float | None:
    """
    Looks up the Boost % for a given Promo ID from the Promotions tab.
    Uses a process-lifetime cache for Promotions rows -- safe because each
    GitHub Actions run is a fresh process and Promotions data doesn't
    change mid-run.

    Reads by header name rather than a fixed column index (unlike
    load_pending_bets' Bets-tab reads), since nothing else in this project
    assumes a fixed column layout for the Promotions tab -- the Node side
    (server.js) already reads it the same way.

    Returns the boost percentage as a float (e.g. 100.0 for "100% Profit
    Boost"), or None if the promo isn't found or has no Boost % set --
    callers should treat None as "cannot resolve, do not guess."
    """
    rows = _get_promotions_rows()
    if not rows:
        print("[sheets_reader] get_promo_boost_percentage: Promotions tab returned zero rows.")
        return None

    header_row_idx, headers = _resolve_promotions_headers_from_rows(rows)
    try:
        idx_promo_id = headers.index("Promo ID")
        idx_boost_pct = headers.index("Boost %")
    except ValueError:
        # "Boost %" column doesn't exist yet in the sheet -- print the
        # ACTUAL header row so a header-name mismatch (typo, extra
        # space, different casing) is immediately visible in the log
        # instead of silently looking identical to "promo not found".
        print(f"[sheets_reader] get_promo_boost_percentage: 'Promo ID' or 'Boost %' "
              f"header not found in Promotions tab. Actual headers: {headers!r}")
        return None

    for row in rows[header_row_idx + 1:]:
        if len(row) <= max(idx_promo_id, idx_boost_pct):
            # Google Sheets trims trailing blank cells from get_all_values()
            # -- a row this short means every cell from here to the end
            # (including, possibly, Promo ID or Boost % itself) was blank
            # on the actual sheet. Logged so this doesn't look identical
            # to "Promo ID just isn't on this row at all".
            if len(row) > idx_promo_id and row[idx_promo_id].strip() == str(promo_id).strip():
                print(f"[sheets_reader] get_promo_boost_percentage: row for Promo ID "
                      f"'{promo_id}' is too short to contain a Boost % cell ({len(row)} "
                      f"cells, Boost % is column index {idx_boost_pct}) -- the cell is "
                      f"either genuinely blank or trimmed by the Sheets API. Treating as blank.")
            continue
        if row[idx_promo_id].strip() == str(promo_id).strip():
            raw = row[idx_boost_pct].strip()
            if not raw:
                print(f"[sheets_reader] get_promo_boost_percentage: found Promo ID "
                      f"'{promo_id}' but its Boost % cell is blank.")
                return None
            try:
                return float(raw.replace("%", "").strip())
            except ValueError:
                print(f"[sheets_reader] get_promo_boost_percentage: found Promo ID "
                      f"'{promo_id}' but could not parse Boost % value {raw!r} as a number.")
                return None

    # Loop completed with no row matching promo_id -- print every Promo ID
    # actually seen in the sheet, so a string-format mismatch (whitespace,
    # type coercion, wrong ID entirely) is immediately diagnosable.
    seen_ids = [row[idx_promo_id] for row in rows[header_row_idx + 1:] if len(row) > idx_promo_id]
    print(f"[sheets_reader] get_promo_boost_percentage: Promo ID {promo_id!r} not found "
          f"in Promotions tab. Promo IDs present: {seen_ids!r}")
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
    column layout is not assumed fixed; reads by header name via BET_COL.

    Returns a list of dicts keyed by the same logical names as
    config.PROMO_COL (promo_id, book, promo_type, expiration_date, etc.),
    plus "row_idx" (1-based, for writing back later). Numeric/date
    fields are returned as raw stripped strings -- parsing into actual
    numbers/dates is promo_resolver.py's job, kept separate so this
    function stays a thin, honest read with no business logic.
    """
    from config import PROMOTIONS_TAB, PROMO_COL, PROMO_STATUS_PENDING

    tab = _get_tab(PROMOTIONS_TAB)

    rows = tab.get_all_values()
    if not rows:
        return []

    header_row_idx, headers = _resolve_promotions_headers_from_rows(rows)
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
    data_start_row = header_row_idx + 2  # 1-based sheet row of first data row
    for row_idx, row in enumerate(rows[header_row_idx + 1:], start=data_start_row):
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
            "start_date":           cell(row, "start_date"),
            "original_odds":        cell(row, "original_odds"),
        })

    return pending


def load_bets_by_promo_id(tab_name: str) -> dict[str, list[dict]]:
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
    rows = _get_bets_rows(tab_name)
    if not rows:
        return {}

    headers = rows[0]
    col = _resolve_bet_col_indices(headers)
    if col.get("promo_id") is None:
        raise RuntimeError(
            "[sheets_reader] Bets tab is missing 'Promo ID' column entirely "
            "-- cannot proceed."
        )

    grouped: dict[str, list[dict]] = {}

    for row_idx, row in enumerate(rows[1:], start=2):  # row 1 is header
        row = _pad_bet_row(row, col)

        promo_id = _bet_cell(row, col, "promo_id")
        if not promo_id:
            continue

        bet = {
            "row_idx":      row_idx,
            "bet_id":       _bet_cell(row, col, "bet_id"),
            "date_placed":  _bet_cell(row, col, "date_placed"),
            "book":         _bet_cell(row, col, "book"),
            "sport":        _bet_cell(row, col, "sport"),
            "stake":        _bet_cell(row, col, "stake"),
            "fee":          _bet_cell(row, col, "fee"),
            "bet_category": _bet_cell(row, col, "bet_category"),
            "promo_id":     promo_id,
            "result":       _bet_cell(row, col, "result"),
            "payout":       _bet_cell(row, col, "payout"),
            "pl":           _bet_cell(row, col, "pl"),
            "odds_taken":   _bet_cell(row, col, "odds_taken"),
        }

        grouped.setdefault(promo_id, []).append(bet)

    return grouped
