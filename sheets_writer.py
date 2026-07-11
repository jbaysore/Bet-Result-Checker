import gspread
from google.oauth2.service_account import Credentials
from config import (
    SHEET_ID, SHEET_TAB, RESULT_VOID, RESULT_NEEDS_REVIEW, get_credentials_info,
    CLOSING_ODDS_ERROR_CODES,
)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]


_client_cache = None
_spreadsheet_cache = None
_sheet_cache = None
_col_idx_cache = None
_promo_sheet_cache = None
_promo_col_idx_cache = None
_book_refunds_cache = None


def _cell_at(row: list[str], col_1based: int) -> str:
    """Return stripped cell value from a row_values() result, or '' if short."""
    idx = col_1based - 1
    if idx < 0 or idx >= len(row):
        return ""
    return str(row[idx]).strip()


def _bet_id_matches(row: list[str], bet_id_col: int, bet_id: str) -> bool:
    current = _cell_at(row, bet_id_col)
    return str(current) == str(bet_id)


def _promo_id_matches(row: list[str], promo_id_col: int, promo_id: str) -> bool:
    current = _cell_at(row, promo_id_col)
    return current == str(promo_id).strip()


def _read_bet_row(sheet, row_idx: int) -> list[str]:
    """One Sheets API read for the full Bets row."""
    return sheet.row_values(row_idx)


def _read_promo_row(sheet, row_idx: int) -> list[str]:
    """One Sheets API read for the full Promotions row."""
    return sheet.row_values(row_idx)


def _get_client():
    """
    Cached for the lifetime of the current process (each scheduled run is
    a fresh process, so there's no cross-run staleness risk) -- re-running
    Credentials.from_service_account_info()/gspread.authorize() on every
    single flag/write call was pure overhead, never actually needed.
    """
    global _client_cache
    if _client_cache is None:
        creds_info = get_credentials_info()
        creds = Credentials.from_service_account_info(creds_info, scopes=SCOPES)
        _client_cache = gspread.authorize(creds)
    return _client_cache


def _get_spreadsheet():
    """Cached spreadsheet handle shared by Bets, Promotions, and Book Settings."""
    global _spreadsheet_cache
    if _spreadsheet_cache is None:
        _spreadsheet_cache = _get_client().open_by_key(SHEET_ID)
    return _spreadsheet_cache


def _get_sheet():
    """
    Cached for the lifetime of the current process. Every writer function
    in this module used to call this on every single invocation, each
    doing a full client.open_by_key() spreadsheet-metadata fetch (a real
    network "read request" against the Sheets API quota) -- across a
    handful of bets needing several flag/write calls each, this was
    enough to trip 'Quota exceeded for Read requests per minute per user'
    (confirmed 2026-06-26, a run crashed mid-batch on exactly this).
    Caching means only the FIRST call in a run pays for the fetch; every
    later call in the same run reuses the same worksheet object.
    """
    global _sheet_cache
    if _sheet_cache is None:
        _sheet_cache = _get_spreadsheet().worksheet(SHEET_TAB)
    return _sheet_cache


def _bets_col_letter_lookup():
    """
    Resolves config.BET_COL header names to actual 1-based column indices
    against the LIVE Bets sheet header row, so columns may be reordered.

    Cached for the lifetime of the current process -- the header row
    can't meaningfully change mid-run, so re-fetching it (a separate
    network read) on every single flag/write call was the other half of
    the redundant-API-call problem _get_sheet()'s docstring describes.
    """
    global _col_idx_cache
    if _col_idx_cache is not None:
        return _col_idx_cache

    from config import BET_COL

    sheet = _get_sheet()
    headers = sheet.row_values(1)

    idx = {}
    for key, header_name in BET_COL.items():
        try:
            idx[key] = headers.index(header_name) + 1  # gspread is 1-based
        except ValueError:
            idx[key] = None
    _col_idx_cache = idx
    return idx


def _get_book_refunds_fee_on_void(book: str) -> bool:
    """
    Looks up whether `book` refunds its per-bet fee when a bet resolves to
    VOID, from the "Book Settings" tab (Book | Refunds Fee On Void). This
    tab is the only storage shared between this Python GitHub Actions
    workflow and the Node Log Bet Wizard (no shared filesystem -- a
    GitHub Actions runner has no persistent disk between runs), and is
    written ONLY by the wizard's Step 2
    prompt, never hand-edited, so the TRUE/FALSE literal is reliable here.

    Returns False (conservative default: do NOT zero the fee) if the book
    isn't found in the tab at all -- this should be rare in practice since
    the wizard prompts for every new book before it can be used to log a
    bet, but a missing entry is safer treated as "don't know, leave it
    alone" than as "assume refunded."

    Cached for the process lifetime -- one Book Settings read per run.
    """
    global _book_refunds_cache
    if _book_refunds_cache is None:
        tab = _get_spreadsheet().worksheet("Book Settings")
        rows = tab.get_all_values()
        mapping: dict[str, bool] = {}
        if rows:
            headers = rows[0]
            try:
                idx_book = headers.index("Book")
                idx_refunds = headers.index("Refunds Fee On Void")
                for row in rows[1:]:
                    if len(row) <= max(idx_book, idx_refunds):
                        continue
                    name = row[idx_book].strip().lower()
                    if name:
                        mapping[name] = row[idx_refunds].strip().upper() == "TRUE"
            except ValueError:
                pass
        _book_refunds_cache = mapping

    return _book_refunds_cache.get(book.strip().lower(), False)


def write_result(row_idx: int, result: str, bet_id: str, book: str = None,
                  pl: float = None, payout: float = None) -> bool:
    """
    Writes a result value to the Result column for a specific row, and
    optionally writes P/L and Payout alongside it in the same call.

    Args:
        row_idx:  1-based row index in the sheet (as returned by sheets_reader)
        result:   One of "WIN", "LOSS", "PUSH", "VOID", "PENDING"
        bet_id:   Used only for logging — confirms we're writing to the right row
        book:     The book this bet was placed with. Required to correctly
                  zero Fee on a VOID write (see below) -- if omitted, Fee
                  is left untouched even on VOID, since the policy can't
                  be checked without knowing which book this is.
        pl:       P/L value to write (from resolver.calculate_pl_and_payout).
                  None means "don't touch the P/L column" — used for PENDING,
                  which isn't a real financial outcome.
        payout:   Payout value to write. None means "don't touch the Payout
                  column" (used for PENDING, and also for any real result
                  where no payout is due — a loss, or a promo-funded void).

    When result is VOID, the Fee column is zeroed in the same batched
    write, but ONLY if `book` is provided AND that book's "Book Settings"
    entry says it refunds the fee on void. This is genuinely per-book --
    confirmed Caesars refunds the fee on void, Polymarket does not.
    Zeroing it unconditionally for every book was an earlier, incorrect
    assumption that this revises. The P/L calculation already ignores fee
    entirely for VOID regardless of what's stored (see
    resolver.calculate_pl_and_payout) -- this Fee-column correction is a
    data-integrity step, not a money-correctness one, keeping the Fee
    column an accurate historical record so future fee reconciliation
    doesn't sum in phantom, already-refunded charges for books that
    actually refund them, while correctly leaving the Fee in place for
    books (like Polymarket) that keep it regardless.

    Returns:
        True if write succeeded, False if it failed.
    """
    idx = _bets_col_letter_lookup()
    result_col = idx.get("result")
    payout_col = idx.get("payout")
    pl_col     = idx.get("pl")
    fee_col    = idx.get("fee")
    bet_id_col = idx.get("bet_id")

    if None in (result_col, payout_col, pl_col, fee_col, bet_id_col):
        print(f"[sheets_writer] ⚠️  Bets tab is missing one or more required "
              f"columns (BetID, Result, Payout, P/L, Fee) -- cannot write "
              f"to row {row_idx}.")
        return False

    try:
        sheet = _get_sheet()
        row = _read_bet_row(sheet, row_idx)

        if not _bet_id_matches(row, bet_id_col, bet_id):
            current_bet_id = _cell_at(row, bet_id_col)
            print(f"[sheets_writer] ⚠️  Row {row_idx} BetID mismatch. "
                  f"Expected '{bet_id}', found '{current_bet_id}'. Skipping write.")
            return False

        current_result = _cell_at(row, result_col)
        # Allow writing only into a blank cell or over a prior NEEDS_REVIEW (a
        # re-scan that finally found a final score). NEVER overwrite a real
        # WIN/LOSS/PUSH/VOID or a manual PENDING -- those are settled or
        # human-owned, and clobbering a hand-entered result would be a data-loss
        # bug.
        if current_result and current_result != RESULT_NEEDS_REVIEW:
            print(f"[sheets_writer] Row {row_idx} (BetID: {bet_id}) already has "
                  f"result '{current_result}'. Skipping.")
            return False
        # No-op when re-writing the identical value (a re-poll that still found
        # nothing re-issues NEEDS_REVIEW) -- don't churn the cell every run.
        if current_result and current_result == result:
            print(f"[sheets_writer] Row {row_idx} (BetID: {bet_id}) re-scan still "
                  f"NEEDS_REVIEW -- no change, leaving as-is.")
            return False

        zero_fee = False
        if result == RESULT_VOID and book:
            zero_fee = _get_book_refunds_fee_on_void(book)

        # Build a batch update so Result, P/L, Payout, and (when applicable)
        # Fee land together in one API call rather than several separate
        # writes -- avoids a window where a row could show a Result with
        # stale/missing P/L if the process were interrupted partway.
        cell_list = [gspread.Cell(row_idx, result_col, result)]
        if pl is not None:
            cell_list.append(gspread.Cell(row_idx, pl_col, pl))
        if payout is not None:
            cell_list.append(gspread.Cell(row_idx, payout_col, payout))
        if zero_fee:
            cell_list.append(gspread.Cell(row_idx, fee_col, 0))

        sheet.update_cells(cell_list)
        print(f"[sheets_writer] ✅ Row {row_idx} (BetID: {bet_id}) → {result}"
              f"{f', P/L={pl}' if pl is not None else ''}"
              f"{f', Payout={payout}' if payout is not None else ''}"
              f"{', Fee=0 (voided, book refunds fee)' if zero_fee else ''}")
        return True

    except gspread.exceptions.APIError as e:
        print(f"[sheets_writer] ❌ Sheets API error writing to row {row_idx} "
              f"(BetID: {bet_id}): {e}")
        return False
    except Exception as e:
        print(f"[sheets_writer] ❌ Unexpected error writing to row {row_idx} "
              f"(BetID: {bet_id}): {e}")
        return False


def write_pl_payout(row_idx: int, bet_id: str, pl: float, payout: float | None) -> bool:
    """
    Writes P/L (and optionally Payout) to a row that already has a Result
    -- used by the P/L/Payout Completion tool to fill in rows where a
    Result was set without going through write_result() (most commonly:
    you resolved a NEEDS_REVIEW bet by hand and typed in Result yourself,
    without computing P/L/Payout).

    Unlike write_result(), this does NOT touch the Result column at all,
    and does NOT zero Fee on VOID (that already happened, if applicable,
    whenever Result was originally set -- this function only ever runs
    on rows where P/L/Payout are currently blank, which by definition
    means write_result() never ran on this row to do that zeroing; if the
    fee genuinely needs zeroing for a manually-VOIDed row, that's a
    separate, manual fix, not something this function should guess at).

    Re-verifies at write time (not just at the read time that selected
    this row) that BetID matches AND that P/L/Payout are still both
    blank -- guards against a race where you manually filled in a value
    between when this row was read and when this write actually happens.

    Args:
        row_idx:  1-based row index in the sheet
        bet_id:   Used to confirm we're writing to the right row
        pl:       P/L value to write. Required (not Optional) -- unlike
                  write_result(), this function's entire purpose is
                  writing a real P/L value, so a None here would be a
                  caller bug, not a legitimate "don't touch" signal.
        payout:   Payout value to write, or None if no payout is due
                  (a loss, or a promo-funded void) -- in that case only
                  P/L is written, Payout is correctly left blank.

    Returns:
        True if write succeeded, False if it failed or was skipped
        (BetID mismatch, or P/L/Payout were no longer both blank).
    """
    idx = _bets_col_letter_lookup()
    pl_col     = idx.get("pl")
    payout_col = idx.get("payout")
    bet_id_col = idx.get("bet_id")

    if None in (pl_col, payout_col, bet_id_col):
        print(f"[sheets_writer] ⚠️  Bets tab is missing one or more required "
              f"columns (BetID, P/L, Payout) -- cannot write to row {row_idx}.")
        return False

    try:
        sheet = _get_sheet()
        row = _read_bet_row(sheet, row_idx)

        if not _bet_id_matches(row, bet_id_col, bet_id):
            current_bet_id = _cell_at(row, bet_id_col)
            print(f"[sheets_writer] ⚠️  Row {row_idx} BetID mismatch. "
                  f"Expected '{bet_id}', found '{current_bet_id}'. Skipping write.")
            return False

        current_pl = _cell_at(row, pl_col)
        current_payout = _cell_at(row, payout_col)
        if current_pl or current_payout:
            print(f"[sheets_writer] Row {row_idx} (BetID: {bet_id}) already has "
                  f"P/L and/or Payout filled in (P/L='{current_pl}', Payout='{current_payout}'). "
                  f"Skipping -- never overwrite an existing value.")
            return False

        cell_list = [gspread.Cell(row_idx, pl_col, pl)]
        if payout is not None:
            cell_list.append(gspread.Cell(row_idx, payout_col, payout))

        sheet.update_cells(cell_list)
        print(f"[sheets_writer] ✅ Row {row_idx} (BetID: {bet_id}) → P/L={pl}"
              f"{f', Payout={payout}' if payout is not None else ''}")
        return True

    except gspread.exceptions.APIError as e:
        print(f"[sheets_writer] ❌ Sheets API error writing to row {row_idx} "
              f"(BetID: {bet_id}): {e}")
        return False
    except Exception as e:
        print(f"[sheets_writer] ❌ Unexpected error writing to row {row_idx} "
              f"(BetID: {bet_id}): {e}")
        return False


def write_pl_only(row_idx: int, bet_id: str, pl: float) -> bool:
    """
    Writes ONLY the P/L cell -- the counterpart to write_pl_payout() for
    the manual-Payout-entry flow (config.MANUAL_PAYOUT_REQUIRED_BOOKS,
    e.g. Kalshi): by the time this runs, Payout is ALREADY filled in (the
    user typed in the real value from their account, since it can't be
    reliably computed from odds), so write_pl_payout()'s "both must
    currently be blank" guard would wrongly refuse this write. This
    function instead only requires that P/L itself is still blank --
    Payout being non-blank is the expected, correct state here, not a
    conflict.

    Re-verifies at write time that BetID matches and P/L is still blank,
    same race-guard reasoning as write_pl_payout().
    """
    idx = _bets_col_letter_lookup()
    pl_col = idx.get("pl")
    bet_id_col = idx.get("bet_id")

    if None in (pl_col, bet_id_col):
        print(f"[sheets_writer] ⚠️  Bets tab is missing P/L or BetID column -- "
              f"cannot write to row {row_idx}.")
        return False

    try:
        sheet = _get_sheet()
        row = _read_bet_row(sheet, row_idx)

        if not _bet_id_matches(row, bet_id_col, bet_id):
            current_bet_id = _cell_at(row, bet_id_col)
            print(f"[sheets_writer] ⚠️  Row {row_idx} BetID mismatch. "
                  f"Expected '{bet_id}', found '{current_bet_id}'. Skipping write.")
            return False

        current_pl = _cell_at(row, pl_col)
        if current_pl:
            print(f"[sheets_writer] Row {row_idx} (BetID: {bet_id}) already has "
                  f"P/L filled in ('{current_pl}'). Skipping -- never overwrite an existing value.")
            return False

        sheet.update_cells([gspread.Cell(row_idx, pl_col, pl)])
        print(f"[sheets_writer] ✅ Row {row_idx} (BetID: {bet_id}) → P/L={pl} (derived from manually-entered Payout)")
        return True

    except gspread.exceptions.APIError as e:
        print(f"[sheets_writer] ❌ Sheets API error writing to row {row_idx} "
              f"(BetID: {bet_id}): {e}")
        return False
    except Exception as e:
        print(f"[sheets_writer] ❌ Unexpected error writing to row {row_idx} "
              f"(BetID: {bet_id}): {e}")
        return False


def write_closing_odds(row_idx: int, bet_id: str,
                       closing_odds: str,
                       decimal_closing: float | None,
                       clv: float | None,
                       overwrite_errors: bool = True) -> bool:
    """
    Writes ClosingOdds, DecimalClosingOdds, and CLV to a bet row atomically.

    Only writes if ClosingOdds is still blank at write time — guards against
    a race where the cell was filled between the read pass and this write.
    DecimalClosingOdds and CLV are optional (written only when not None).

    Args:
        row_idx:         1-based row index
        bet_id:          Used to confirm we're writing to the right row
        closing_odds:    American odds string, e.g. "-110" or "+150"
        decimal_closing: Decimal conversion, e.g. 1.909
        clv:             Sheet-format CLV (divided by 100), e.g. 0.0543

    Returns:
        True if write succeeded, False if skipped or failed.
    """
    idx = _bets_col_letter_lookup()
    closing_col  = idx.get("closing_odds")
    decimal_col  = idx.get("decimal_closing")
    clv_col      = idx.get("clv")
    bet_id_col   = idx.get("bet_id")

    if None in (closing_col, bet_id_col):
        print(f"[sheets_writer] ⚠️  Bets tab is missing ClosingOdds or BetID column -- "
              f"cannot write closing odds to row {row_idx}.")
        return False

    try:
        sheet = _get_sheet()
        row = _read_bet_row(sheet, row_idx)

        if not _bet_id_matches(row, bet_id_col, bet_id):
            current_bet_id = _cell_at(row, bet_id_col)
            print(f"[sheets_writer] ⚠️  Row {row_idx} BetID mismatch. "
                  f"Expected '{bet_id}', found '{current_bet_id}'. Skipping closing odds write.")
            return False

        current_closing = _cell_at(row, closing_col)
        # Never overwrite a real value, "VOID", or "N/A" -- only a blank cell or
        # a prior failure code (so a re-scan can replace BOOK NOT FOUND etc.
        # with a real value, or update it to a different code).
        if current_closing and (
            not overwrite_errors or current_closing not in CLOSING_ODDS_ERROR_CODES
        ):
            print(f"[sheets_writer] Row {row_idx} (BetID: {bet_id}) already has "
                  f"ClosingOdds ('{current_closing}'). Skipping.")
            return False
        # No-op when a re-scan produces the identical failure code -- avoid
        # churning the cell with a needless Sheets write every run for a row
        # that keeps failing the same way.
        if current_closing and current_closing == closing_odds:
            print(f"[sheets_writer] Row {row_idx} (BetID: {bet_id}) re-scan still "
                  f"'{current_closing}' -- no change, leaving cell as-is.")
            return False

        cells = [gspread.Cell(row_idx, closing_col, closing_odds)]
        if decimal_closing is not None and decimal_col is not None:
            cells.append(gspread.Cell(row_idx, decimal_col, decimal_closing))
        if clv is not None and clv_col is not None:
            cells.append(gspread.Cell(row_idx, clv_col, clv))

        sheet.update_cells(cells)
        print(f"[sheets_writer] ✅ Row {row_idx} (BetID: {bet_id}) → "
              f"ClosingOdds={closing_odds}"
              f"{f', DecimalClosing={decimal_closing}' if decimal_closing is not None else ''}"
              f"{f', CLV={clv}' if clv is not None else ''}")
        return True

    except gspread.exceptions.APIError as e:
        print(f"[sheets_writer] ❌ Sheets API error writing closing odds to row {row_idx} "
              f"(BetID: {bet_id}): {e}")
        return False
    except Exception as e:
        print(f"[sheets_writer] ❌ Unexpected error writing closing odds to row {row_idx} "
              f"(BetID: {bet_id}): {e}")
        return False


def clear_closing_odds_cells(row_idx: int, bet_id: str) -> bool:
    """
    Clears ClosingOdds, DecimalClosingOdds, and CLV for a one-shot retry.

    Unlike write_closing_odds, this intentionally clears final values
    including N/A so fetch_closing_odds can run again.
    """
    idx = _bets_col_letter_lookup()
    closing_col = idx.get("closing_odds")
    decimal_col = idx.get("decimal_closing")
    clv_col = idx.get("clv")
    bet_id_col = idx.get("bet_id")

    if None in (closing_col, bet_id_col):
        print(f"[sheets_writer] ⚠️  Bets tab is missing ClosingOdds or BetID column -- "
              f"cannot clear closing odds on row {row_idx}.")
        return False

    try:
        sheet = _get_sheet()
        row = _read_bet_row(sheet, row_idx)

        if not _bet_id_matches(row, bet_id_col, bet_id):
            current_bet_id = _cell_at(row, bet_id_col)
            print(f"[sheets_writer] ⚠️  Row {row_idx} BetID mismatch. "
                  f"Expected '{bet_id}', found '{current_bet_id}'. Skipping clear.")
            return False

        cells = [gspread.Cell(row_idx, closing_col, "")]
        if decimal_col is not None:
            cells.append(gspread.Cell(row_idx, decimal_col, ""))
        if clv_col is not None:
            cells.append(gspread.Cell(row_idx, clv_col, ""))

        sheet.update_cells(cells)
        print(f"[sheets_writer] ✅ Row {row_idx} (BetID: {bet_id}) closing columns cleared.")
        return True

    except gspread.exceptions.APIError as e:
        print(f"[sheets_writer] ❌ Sheets API error clearing closing odds on row {row_idx} "
              f"(BetID: {bet_id}): {e}")
        return False
    except Exception as e:
        print(f"[sheets_writer] ❌ Unexpected error clearing closing odds on row {row_idx} "
              f"(BetID: {bet_id}): {e}")
        return False


def clear_market_key_cell(row_idx: int, bet_id: str) -> bool:
    """Clear Market Key so a retry can cascade across main + alternate markets."""
    idx = _bets_col_letter_lookup()
    market_col = idx.get("market_key")
    bet_id_col = idx.get("bet_id")

    if market_col is None:
        return True
    if bet_id_col is None:
        return False

    try:
        sheet = _get_sheet()
        row = _read_bet_row(sheet, row_idx)
        if not _bet_id_matches(row, bet_id_col, bet_id):
            return False
        if not _cell_at(row, market_col):
            return True
        sheet.update_cell(row_idx, market_col, "")
        print(f"[sheets_writer] ✅ Row {row_idx} (BetID: {bet_id}) Market Key cleared.")
        return True
    except Exception as e:
        print(f"[sheets_writer] ⚠️  Could not clear Market Key on row {row_idx}: {e}")
        return False


def write_market_key_if_blank(row_idx: int, bet_id: str, market_key: str) -> bool:
    """Stamp Market Key when the column exists and the cell is blank."""
    mk = (market_key or "").strip()
    if not mk:
        return True

    idx = _bets_col_letter_lookup()
    market_col = idx.get("market_key")
    bet_id_col = idx.get("bet_id")

    if market_col is None:
        return True  # column not on sheet — optional

    if bet_id_col is None:
        print(f"[sheets_writer] ⚠️  Bets tab is missing BetID column -- "
              f"cannot write Market Key on row {row_idx}.")
        return False

    try:
        sheet = _get_sheet()
        row = _read_bet_row(sheet, row_idx)

        if not _bet_id_matches(row, bet_id_col, bet_id):
            return False

        if _cell_at(row, market_col):
            return True  # already set

        sheet.update_cell(row_idx, market_col, mk)
        print(f"[sheets_writer] ✅ Row {row_idx} (BetID: {bet_id}) → Market Key={mk}")
        return True

    except gspread.exceptions.APIError as e:
        print(f"[sheets_writer] ❌ Sheets API error writing Market Key on row {row_idx}: {e}")
        return False
    except Exception as e:
        print(f"[sheets_writer] ❌ Unexpected error writing Market Key on row {row_idx}: {e}")
        return False


def bump_closing_odds_fail_streak(row_idx: int, bet_id: str, error_code: str) -> bool:
    """
    Increment the identical-failure streak in Notes; mark exhausted at threshold.
    Called when write_closing_odds no-ops on the same error code.
    """
    from closing_odds_exhaustion import next_fail_streak_notes, parse_fail_streak

    idx = _bets_col_letter_lookup()
    notes_col = idx.get("notes")
    bet_id_col = idx.get("bet_id")

    if None in (notes_col, bet_id_col):
        return False

    try:
        sheet = _get_sheet()
        row = _read_bet_row(sheet, row_idx)
        if not _bet_id_matches(row, bet_id_col, bet_id):
            return False

        existing = _cell_at(row, notes_col)
        new_notes, exhausted = next_fail_streak_notes(existing, error_code)
        if new_notes == existing:
            return True

        sheet.update_cell(row_idx, notes_col, new_notes)
        count, _ = parse_fail_streak(new_notes)
        if exhausted:
            print(f"[sheets_writer] Row {row_idx} (BetID: {bet_id}) closing-odds retries "
                  f"exhausted after {count} identical '{error_code}' failures.")
        return True
    except Exception as e:
        print(f"[sheets_writer] ⚠️  Could not update closing-odds fail streak on row "
              f"{row_idx}: {e}")
        return False


def clear_closing_odds_fail_streak(row_idx: int, bet_id: str) -> bool:
    """Remove fail-streak markers after a successful closing-odds capture."""
    from closing_odds_exhaustion import clear_fail_streak_notes, parse_fail_streak, is_closing_odds_exhausted

    idx = _bets_col_letter_lookup()
    notes_col = idx.get("notes")
    bet_id_col = idx.get("bet_id")

    if None in (notes_col, bet_id_col):
        return False

    try:
        sheet = _get_sheet()
        row = _read_bet_row(sheet, row_idx)
        if not _bet_id_matches(row, bet_id_col, bet_id):
            return False

        existing = _cell_at(row, notes_col)
        if not parse_fail_streak(existing)[0] and not is_closing_odds_exhausted(existing):
            return True

        new_notes = clear_fail_streak_notes(existing)
        sheet.update_cell(row_idx, notes_col, new_notes)
        return True
    except Exception:
        return False


PL_BLOCKED_PREFIX = "⚠ P/L not calculated:"
DUPLICATE_BET_ID_PREFIX = "⚠ Duplicate BetID:"


def flag_duplicate_bet_id(row_idx: int, bet_id: str, detail: str) -> bool:
    """
    Appends a visible duplicate-BetID warning to Notes when the same BetID
    appears on more than one row. Automated settlement skips these rows;
    this makes the problem visible on the sheet itself, not just in CI logs.

    Idempotent: replaces an existing DUPLICATE_BET_ID_PREFIX line rather than
    stacking duplicates on every scheduled run.
    """
    idx = _bets_col_letter_lookup()
    notes_col = idx.get("notes")
    bet_id_col = idx.get("bet_id")

    if None in (notes_col, bet_id_col):
        print(f"[sheets_writer] ⚠️  Bets tab is missing Notes/BetID column -- "
              f"cannot flag row {row_idx} for duplicate BetID.")
        return False

    try:
        sheet = _get_sheet()
        row = _read_bet_row(sheet, row_idx)

        if not _bet_id_matches(row, bet_id_col, bet_id):
            current_bet_id = _cell_at(row, bet_id_col)
            print(f"[sheets_writer] ⚠️  Row {row_idx} BetID mismatch. "
                  f"Expected '{bet_id}', found '{current_bet_id}'. Skipping duplicate flag.")
            return False

        existing_notes = _cell_at(row, notes_col)
        flag_line = f"{DUPLICATE_BET_ID_PREFIX} {detail}"

        other_lines = [
            line for line in existing_notes.split("\n")
            if line.strip() and not line.strip().startswith(DUPLICATE_BET_ID_PREFIX)
        ]
        new_notes = "\n".join(other_lines + [flag_line])

        sheet.update_cell(row_idx, notes_col, new_notes)
        print(f"[sheets_writer] 🚩 Row {row_idx} (BetID: {bet_id}) flagged: {flag_line}")
        return True

    except gspread.exceptions.APIError as e:
        print(f"[sheets_writer] ❌ Sheets API error flagging duplicate BetID on row {row_idx} "
              f"(BetID: {bet_id}): {e}")
        return False
    except Exception as e:
        print(f"[sheets_writer] ❌ Unexpected error flagging duplicate BetID on row {row_idx} "
              f"(BetID: {bet_id}): {e}")
        return False


def upsert_notes_line(row_idx: int, bet_id: str, prefix: str, line: str | None) -> bool:
    """
    Replace any existing Notes line starting with `prefix` with `line` (append if
    none exists); pass line=None to just REMOVE the prefixed line. Used by the
    prop resolver's Final+re-verify state: the `props-observed:` marker and the
    `props-proposed:` shadow-mode proposal both live as single, self-replacing
    Notes lines (same idempotent pattern as flag_pl_blocked). Never touches
    Result/P/L/Payout. BetID-guarded like every other Notes write.
    """
    idx = _bets_col_letter_lookup()
    notes_col = idx.get("notes")
    bet_id_col = idx.get("bet_id")
    if None in (notes_col, bet_id_col):
        print(f"[sheets_writer] ⚠️  Bets tab missing Notes/BetID column -- cannot upsert note row {row_idx}.")
        return False
    try:
        sheet = _get_sheet()
        row = _read_bet_row(sheet, row_idx)
        if not _bet_id_matches(row, bet_id_col, bet_id):
            print(f"[sheets_writer] ⚠️  Row {row_idx} BetID mismatch on notes upsert. Skipping.")
            return False
        existing_notes = _cell_at(row, notes_col)
        other_lines = [
            ln for ln in existing_notes.split("\n")
            if ln.strip() and not ln.strip().startswith(prefix)
        ]
        new_lines = other_lines + ([line] if line else [])
        sheet.update_cell(row_idx, notes_col, "\n".join(new_lines))
        return True
    except gspread.exceptions.APIError as e:
        print(f"[sheets_writer] ❌ Sheets API error upserting note row {row_idx} (BetID {bet_id}): {e}")
        return False
    except Exception as e:
        print(f"[sheets_writer] ❌ Unexpected error upserting note row {row_idx} (BetID {bet_id}): {e}")
        return False


def flag_pl_blocked(row_idx: int, bet_id: str, reason: str) -> bool:
    """
    Appends a visible "⚠ P/L not calculated: <reason>" line to the Notes
    column when complete_pl_payout()/poll_bet() has to skip P/L because
    something's missing (most commonly a blank Fee -- see
    poller._safe_calculate_pl_payout). Without this, a skip was only ever
    printed to the script's own console log, which nobody watches between
    scheduled runs -- the bet's Result would just look correctly settled
    with a silently-blank P/L and no visible explanation anywhere on the
    sheet itself.

    Idempotent: if Notes already contains a PL_BLOCKED_PREFIX line, it's
    replaced rather than duplicated (the reason can change between runs --
    e.g. Fee gets filled in but a different field is now the blocker --
    and re-appending every run would spam Notes on a bet that's skipped
    repeatedly over weeks).

    Does NOT touch Result, P/L, or Payout -- purely an informational note,
    safe to call every time a skip happens.
    """
    idx = _bets_col_letter_lookup()
    notes_col = idx.get("notes")
    bet_id_col = idx.get("bet_id")

    if None in (notes_col, bet_id_col):
        print(f"[sheets_writer] ⚠️  Bets tab is missing Notes/BetID column -- "
              f"cannot flag row {row_idx} as P/L-blocked.")
        return False

    try:
        sheet = _get_sheet()
        row = _read_bet_row(sheet, row_idx)

        if not _bet_id_matches(row, bet_id_col, bet_id):
            current_bet_id = _cell_at(row, bet_id_col)
            print(f"[sheets_writer] ⚠️  Row {row_idx} BetID mismatch. "
                  f"Expected '{bet_id}', found '{current_bet_id}'. Skipping Notes flag.")
            return False

        existing_notes = _cell_at(row, notes_col)
        flag_line = f"{PL_BLOCKED_PREFIX} {reason}"

        other_lines = [
            line for line in existing_notes.split("\n")
            if line.strip() and not line.strip().startswith(PL_BLOCKED_PREFIX)
        ]
        new_notes = "\n".join(other_lines + [flag_line])

        sheet.update_cell(row_idx, notes_col, new_notes)
        print(f"[sheets_writer] 🚩 Row {row_idx} (BetID: {bet_id}) flagged: {flag_line}")
        return True

    except gspread.exceptions.APIError as e:
        print(f"[sheets_writer] ❌ Sheets API error flagging row {row_idx} "
              f"(BetID: {bet_id}): {e}")
        return False
    except Exception as e:
        print(f"[sheets_writer] ❌ Unexpected error flagging row {row_idx} "
              f"(BetID: {bet_id}): {e}")
        return False


def clear_pl_blocked_flag(row_idx: int, bet_id: str) -> bool:
    """
    Removes a previously-written flag_pl_blocked() line from Notes, once
    P/L has actually been computed successfully -- called right after a
    successful write_pl_payout() so the flag doesn't sit there forever
    looking like P/L is still missing after it's been filled in.
    Leaves any other Notes content untouched.
    """
    idx = _bets_col_letter_lookup()
    notes_col = idx.get("notes")
    bet_id_col = idx.get("bet_id")

    if None in (notes_col, bet_id_col):
        return False

    try:
        sheet = _get_sheet()
        row = _read_bet_row(sheet, row_idx)

        if not _bet_id_matches(row, bet_id_col, bet_id):
            return False

        existing_notes = _cell_at(row, notes_col)
        if PL_BLOCKED_PREFIX not in existing_notes:
            return True  # nothing to clear

        other_lines = [
            line for line in existing_notes.split("\n")
            if line.strip() and not line.strip().startswith(PL_BLOCKED_PREFIX)
        ]
        sheet.update_cell(row_idx, notes_col, "\n".join(other_lines))
        print(f"[sheets_writer] ✅ Row {row_idx} (BetID: {bet_id}) P/L-blocked flag cleared.")
        return True

    except Exception as e:
        print(f"[sheets_writer] ❌ Unexpected error clearing P/L-blocked flag on row "
              f"{row_idx} (BetID: {bet_id}): {e}")
        return False


PAYOUT_CAUTION_PREFIX = "ℹ Payout check:"


def flag_payout_caution(row_idx: int, bet_id: str, message: str) -> bool:
    """
    Appends a non-blocking "ℹ Payout check: <message>" line to Notes when
    a manually-entered Payout (config.MANUAL_PAYOUT_REQUIRED_BOOKS flow,
    see poller.complete_manual_payout_pl) differs significantly from the
    rough American-odds estimate -- a typo guard (e.g. fat-fingering
    $329.50 instead of $32.95), not a correctness claim about the manual
    entry itself, which is trusted as ground truth from the real account.

    Deliberately does NOT block the P/L write the way flag_pl_blocked()
    does -- the whole point of manual entry is that the user's number IS
    the answer; this just surfaces "you might want to double-check this"
    alongside the now-filled-in P/L, same idempotent-line pattern as
    flag_pl_blocked() (one line, replaced not duplicated, on repeat runs).
    """
    idx = _bets_col_letter_lookup()
    notes_col = idx.get("notes")
    bet_id_col = idx.get("bet_id")

    if None in (notes_col, bet_id_col):
        return False

    try:
        sheet = _get_sheet()
        row = _read_bet_row(sheet, row_idx)

        if not _bet_id_matches(row, bet_id_col, bet_id):
            return False

        existing_notes = _cell_at(row, notes_col)
        flag_line = f"{PAYOUT_CAUTION_PREFIX} {message}"

        other_lines = [
            line for line in existing_notes.split("\n")
            if line.strip() and not line.strip().startswith(PAYOUT_CAUTION_PREFIX)
        ]
        new_notes = "\n".join(other_lines + [flag_line])

        sheet.update_cell(row_idx, notes_col, new_notes)
        print(f"[sheets_writer] ℹ️  Row {row_idx} (BetID: {bet_id}) caution: {flag_line}")
        return True

    except Exception as e:
        print(f"[sheets_writer] ❌ Unexpected error flagging payout caution on row "
              f"{row_idx} (BetID: {bet_id}): {e}")
        return False


# ════════════════════════════════════════════════════════════════════
# ── Promotion Updater writes ────────────────────────────────────────
# ════════════════════════════════════════════════════════════════════

def _get_promotions_sheet():
    global _promo_sheet_cache
    if _promo_sheet_cache is None:
        from config import PROMOTIONS_TAB
        _promo_sheet_cache = _get_spreadsheet().worksheet(PROMOTIONS_TAB)
    return _promo_sheet_cache


def _promo_col_letter_lookup():
    """
    Resolves config.PROMO_COL's header names to actual 1-based column
    indices against the LIVE sheet. Cached for the process lifetime --
    one Promotions tab read per run instead of one per write.
    """
    global _promo_col_idx_cache
    if _promo_col_idx_cache is not None:
        return _promo_col_idx_cache

    from config import PROMO_COL
    from sheets_reader import _resolve_promotions_headers_from_rows

    sheet = _get_promotions_sheet()
    rows = sheet.get_all_values()
    _, headers = _resolve_promotions_headers_from_rows(rows)

    idx = {}
    for key, header_name in PROMO_COL.items():
        try:
            idx[key] = headers.index(header_name) + 1  # gspread is 1-based
        except ValueError:
            idx[key] = None
    _promo_col_idx_cache = idx
    return idx


def write_promo_qualifying_cost(row_idx: int, promo_id: str, qualifying_cost: float) -> bool:
    """
    Backfills the Qualifying Cost cell for a Promotions row, ONLY if it's
    currently blank -- never overwrites an existing value, same
    "never overwrite" convention as write_pl_payout().

    This is intentionally a separate, narrower write than
    write_promo_resolution() below: Qualifying Cost can become knowable
    (the qualifying window has closed, so the set of linked Qualifying
    Bet rows is final and stable) well before the REST of the promo can
    be finalized (reward tokens might still be unclaimed or unsettled).
    Filling it in as soon as it's stable, rather than waiting for full
    resolution, means Bonus Net Value figures elsewhere in the project
    aren't needlessly stuck at $0 for weeks while a slow-resolving token
    bet is pending.

    Re-verifies BetID-equivalent identity (Promo ID) and blankness at
    write time, not just read time, guarding against the same kind of
    race write_pl_payout() guards against.

    Returns:
        True if written, False if skipped (Promo ID mismatch, cell
        already had a value, or the column doesn't exist in the sheet).
    """
    idx = _promo_col_letter_lookup()
    promo_id_col = idx.get("promo_id")
    qc_col = idx.get("qualifying_cost")

    if promo_id_col is None or qc_col is None:
        print(f"[sheets_writer] ⚠️  Promotions tab is missing 'Promo ID' or "
              f"'Qualifying Cost' column -- cannot write to row {row_idx}.")
        return False

    try:
        sheet = _get_promotions_sheet()
        row = _read_promo_row(sheet, row_idx)

        if not _promo_id_matches(row, promo_id_col, promo_id):
            current_promo_id = _cell_at(row, promo_id_col)
            print(f"[sheets_writer] ⚠️  Promotions row {row_idx} Promo ID mismatch. "
                  f"Expected '{promo_id}', found '{current_promo_id}'. Skipping write.")
            return False

        current_qc = _cell_at(row, qc_col)
        if current_qc:
            print(f"[sheets_writer] Promotions row {row_idx} (Promo ID: {promo_id}) "
                  f"already has Qualifying Cost '{current_qc}'. Skipping -- "
                  f"never overwrite an existing value.")
            return False

        sheet.update_cell(row_idx, qc_col, qualifying_cost)
        print(f"[sheets_writer] ✅ Promotions row {row_idx} (Promo ID: {promo_id}) "
              f"→ Qualifying Cost={qualifying_cost}")
        return True

    except gspread.exceptions.APIError as e:
        print(f"[sheets_writer] ❌ Sheets API error writing Qualifying Cost to "
              f"Promotions row {row_idx} (Promo ID: {promo_id}): {e}")
        return False
    except Exception as e:
        print(f"[sheets_writer] ❌ Unexpected error writing Qualifying Cost to "
              f"Promotions row {row_idx} (Promo ID: {promo_id}): {e}")
        return False


def write_promo_resolution(row_idx: int, promo_id: str, status: str,
                            realized_date: str, realized_amount: float) -> bool:
    """
    Finalizes a Promotions row -- writes Status, Realized Date, and
    Realized Amount together in one batched call. Used for BOTH terminal
    outcomes: a real Realized close-out (status="Realized", whatever the
    computed dollar amount is, including a legitimate $0), and the
    Unused close-out (status="Unused", realized_amount=0, for a
    qualifying window that expired with zero linked activity).

    Guards against double-writes the same way write_result() does for
    Bets rows: re-verifies Promo ID matches AND that Status is still
    "Pending" at write time, not just at whatever earlier point this
    row was read -- since this script could in principle run again
    before a previous run's write is reflected back, or someone could
    have hand-edited the row in between.

    Args:
        row_idx:         1-based row index in the Promotions sheet
        promo_id:        Used to confirm we're writing to the right row
        status:          config.PROMO_STATUS_REALIZED or
                          config.PROMO_STATUS_UNUSED -- never PENDING,
                          since this function's entire purpose is closing
                          a promo out.
        realized_date:   ISO date string (YYYY-MM-DD) for when this was
                          finalized.
        realized_amount: The final dollar value. 0 is a fully legitimate
                          value here (a $0 Realized promo is different
                          from Unused -- see config.PROMO_STATUS_UNUSED's
                          docstring) and must NOT be treated as "no
                          value provided."

    Returns:
        True if written, False if skipped (Promo ID mismatch, Status was
        no longer "Pending", or a required column is missing).
    """
    from config import PROMO_STATUS_PENDING

    idx = _promo_col_letter_lookup()
    promo_id_col = idx.get("promo_id")
    status_col = idx.get("status")
    realized_date_col = idx.get("realized_date")
    realized_amount_col = idx.get("realized_amount")

    missing = [name for name, col in [
        ("Promo ID", promo_id_col), ("Status", status_col),
        ("Realized Date", realized_date_col), ("Realized Amount", realized_amount_col)
    ] if col is None]
    if missing:
        print(f"[sheets_writer] ⚠️  Promotions tab is missing column(s) "
              f"{missing} -- cannot write resolution to row {row_idx}.")
        return False

    try:
        sheet = _get_promotions_sheet()
        row = _read_promo_row(sheet, row_idx)

        if not _promo_id_matches(row, promo_id_col, promo_id):
            current_promo_id = _cell_at(row, promo_id_col)
            print(f"[sheets_writer] ⚠️  Promotions row {row_idx} Promo ID mismatch. "
                  f"Expected '{promo_id}', found '{current_promo_id}'. Skipping write.")
            return False

        current_status = _cell_at(row, status_col)
        if current_status != PROMO_STATUS_PENDING:
            print(f"[sheets_writer] Promotions row {row_idx} (Promo ID: {promo_id}) "
                  f"Status is already '{current_status}', not Pending. Skipping -- "
                  f"never overwrite a terminal state.")
            return False

        cell_list = [
            gspread.Cell(row_idx, status_col, status),
            gspread.Cell(row_idx, realized_date_col, realized_date),
            gspread.Cell(row_idx, realized_amount_col, realized_amount),
        ]
        sheet.update_cells(cell_list)
        print(f"[sheets_writer] ✅ Promotions row {row_idx} (Promo ID: {promo_id}) "
              f"→ Status={status}, Realized Date={realized_date}, "
              f"Realized Amount={realized_amount}")
        return True

    except gspread.exceptions.APIError as e:
        print(f"[sheets_writer] ❌ Sheets API error writing resolution to "
              f"Promotions row {row_idx} (Promo ID: {promo_id}): {e}")
        return False
    except Exception as e:
        print(f"[sheets_writer] ❌ Unexpected error writing resolution to "
              f"Promotions row {row_idx} (Promo ID: {promo_id}): {e}")
        return False
