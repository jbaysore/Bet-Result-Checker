import gspread
from google.oauth2.service_account import Credentials
from config import SHEET_ID, SHEET_TAB, COL, RESULT_VOID, get_credentials_info

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]


def _get_client():
    creds_info = get_credentials_info()
    creds = Credentials.from_service_account_info(creds_info, scopes=SCOPES)
    return gspread.authorize(creds)


def _get_sheet():
    client = _get_client()
    spreadsheet = client.open_by_key(SHEET_ID)
    return spreadsheet.worksheet(SHEET_TAB)


def _get_book_refunds_fee_on_void(book: str) -> bool:
    """
    Looks up whether `book` refunds its per-bet fee when a bet resolves to
    VOID, from the "Book Settings" tab (Book | Refunds Fee On Void). This
    tab is the only storage shared between this Python deployment and the
    Node Log Bet Wizard (no shared filesystem -- Cloud Run has no
    persistent local disk), and is written ONLY by the wizard's Step 2
    prompt, never hand-edited, so the TRUE/FALSE literal is reliable here.

    Returns False (conservative default: do NOT zero the fee) if the book
    isn't found in the tab at all -- this should be rare in practice since
    the wizard prompts for every new book before it can be used to log a
    bet, but a missing entry is safer treated as "don't know, leave it
    alone" than as "assume refunded."
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
        idx_refunds = headers.index("Refunds Fee On Void")
    except ValueError:
        return False

    for row in rows[1:]:
        if len(row) <= max(idx_book, idx_refunds):
            continue
        if row[idx_book].strip().lower() == book.strip().lower():
            return row[idx_refunds].strip().upper() == "TRUE"

    return False


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
    result_col = COL["result"] + 1
    payout_col = COL["payout"] + 1
    pl_col     = COL["pl"] + 1
    fee_col    = COL["fee"] + 1

    try:
        sheet = _get_sheet()

        # Safety check — verify the BetID in this row matches before writing
        bet_id_col = COL["bet_id"] + 1
        current_bet_id = sheet.cell(row_idx, bet_id_col).value

        if current_bet_id != bet_id:
            print(f"[sheets_writer] ⚠️  Row {row_idx} BetID mismatch. "
                  f"Expected '{bet_id}', found '{current_bet_id}'. Skipping write.")
            return False

        # Check result isn't already filled (safety guard against double writes)
        current_result = sheet.cell(row_idx, result_col).value
        if current_result:
            print(f"[sheets_writer] Row {row_idx} (BetID: {bet_id}) already has "
                  f"result '{current_result}'. Skipping.")
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
    pl_col     = COL["pl"] + 1
    payout_col = COL["payout"] + 1
    bet_id_col = COL["bet_id"] + 1

    try:
        sheet = _get_sheet()

        current_bet_id = sheet.cell(row_idx, bet_id_col).value
        if current_bet_id != bet_id:
            print(f"[sheets_writer] ⚠️  Row {row_idx} BetID mismatch. "
                  f"Expected '{bet_id}', found '{current_bet_id}'. Skipping write.")
            return False

        current_pl = sheet.cell(row_idx, pl_col).value
        current_payout = sheet.cell(row_idx, payout_col).value
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