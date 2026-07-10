"""Always-on proactive closing-odds capture worker for Railway."""

import os
import time
from datetime import datetime, timedelta, timezone

import gspread

from closing_odds import (
    _clv_from_decimals,
    fetch_live_closing_odds,
    needs_manual_closing_odds,
    parse_selection,
    to_decimal_odds,
)
from config import SHEET_ID, SHEET_TAB
from poller import _parse_game_datetime
from sheets_reader import _get_spreadsheet
from sheets_writer import write_closing_odds


QUEUE_TAB = os.getenv("CLOSING_CAPTURE_TAB", "ClosingCapture")
POLL_SECONDS = max(10, int(os.getenv("CLOSING_CAPTURE_POLL_SECONDS", "30")))
RECONCILE_SECONDS = max(300, int(os.getenv("CLOSING_CAPTURE_RECONCILE_SECONDS", "900")))

QUEUE_HEADERS = [
    "BetID", "Commence UTC", "Sport", "Book", "Team 1", "Team 2",
    "Selection", "Bet Type", "OddsTaken", "Market Key", "Status",
    "T-10 Price", "T-10 At", "T-10 Error",
    "T-5 Price", "T-5 At", "T-5 Error",
    "T-1 Price", "T-1 At", "T-1 Error",
    "Final Price", "Finalized At", "Last Error", "Created At", "Updated At",
]

FINAL_STATUSES = {"COMPLETED", "SKIPPED_EXISTING", "FALLBACK", "UNSUPPORTED", "INVALID"}
AUTOMATED_BET_TYPES = {"Moneyline", "Spread", "Total", "Draw"}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_utc(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).astimezone(timezone.utc)
    except (AttributeError, TypeError, ValueError):
        return None


def active_slot(now: datetime, commence: datetime) -> str | None:
    """Return the ladder window that contains now; never back-label missed samples."""
    remaining = commence - now
    if timedelta(minutes=5) < remaining <= timedelta(minutes=10):
        return "T-10"
    if timedelta(minutes=1) < remaining <= timedelta(minutes=5):
        return "T-5"
    if timedelta(0) < remaining <= timedelta(minutes=1):
        return "T-1"
    return None


def latest_sample(record: dict) -> str | None:
    for slot in ("T-1", "T-5", "T-10"):
        price = str(record.get(f"{slot} Price", "")).strip()
        if price:
            return price
    return None


def ensure_queue_tab():
    spreadsheet = _get_spreadsheet()
    try:
        tab = spreadsheet.worksheet(QUEUE_TAB)
    except gspread.WorksheetNotFound:
        tab = spreadsheet.add_worksheet(title=QUEUE_TAB, rows=1000, cols=len(QUEUE_HEADERS))
    headers = tab.row_values(1)
    if not headers:
        tab.update([QUEUE_HEADERS], "A1:Y1")
    elif headers != QUEUE_HEADERS:
        raise RuntimeError(f"{QUEUE_TAB} headers do not match the worker schema")
    return tab


def row_record(headers: list[str], row: list[str]) -> dict:
    padded = row + [""] * max(0, len(headers) - len(row))
    return dict(zip(headers, padded))


def update_queue_row(tab, row_idx: int, changes: dict):
    indices = {name: i + 1 for i, name in enumerate(QUEUE_HEADERS)}
    now_text = iso(utc_now())
    payload = dict(changes)
    payload["Updated At"] = now_text
    cells = [gspread.Cell(row_idx, indices[key], value) for key, value in payload.items()]
    tab.update_cells(cells)


def find_bet_row(bet_id: str) -> tuple[int | None, dict | None]:
    rows = _get_spreadsheet().worksheet(SHEET_TAB).get_all_values()
    if not rows or "BetID" not in rows[0]:
        return None, None
    headers = rows[0]
    idx = headers.index("BetID")
    matches = [(i, row) for i, row in enumerate(rows[1:], start=2)
               if idx < len(row) and row[idx].strip() == str(bet_id).strip()]
    if len(matches) != 1:
        return None, None
    row_idx, row = matches[0]
    return row_idx, row_record(headers, row)


def finalize(tab, queue_row_idx: int, record: dict, price: str | None) -> str:
    bet_id = record["BetID"]
    bet_row_idx, bet = find_bet_row(bet_id)
    if bet_row_idx is None:
        update_queue_row(tab, queue_row_idx, {
            "Status": "INVALID", "Last Error": "BetID missing or duplicated on Bets tab",
            "Finalized At": iso(utc_now()),
        })
        return "INVALID"

    if str(bet.get("ClosingOdds", "")).strip():
        update_queue_row(tab, queue_row_idx, {
            "Status": "SKIPPED_EXISTING", "Finalized At": iso(utc_now()),
            "Last Error": "ClosingOdds was already populated",
        })
        return "SKIPPED_EXISTING"

    if not price:
        update_queue_row(tab, queue_row_idx, {
            "Status": "FALLBACK", "Finalized At": iso(utc_now()),
            "Last Error": record.get("Last Error") or "No proactive sample succeeded",
        })
        return "FALLBACK"

    decimal_closing = to_decimal_odds(price)
    try:
        decimal_taken = float(str(bet.get("DecimalOddsTaken", "")).strip())
    except ValueError:
        decimal_taken = to_decimal_odds(bet.get("OddsTaken"))
    clv = _clv_from_decimals(decimal_taken, decimal_closing)
    wrote = write_closing_odds(
        bet_row_idx, bet_id, price, decimal_closing, clv,
        overwrite_errors=False,
    )
    status = "COMPLETED" if wrote else "SKIPPED_EXISTING"
    update_queue_row(tab, queue_row_idx, {
        "Status": status, "Final Price": price, "Finalized At": iso(utc_now()),
        "Last Error": "" if wrote else "ClosingOdds changed before final write",
    })
    return status


def capture_record(tab, row_idx: int, record: dict, slot: str):
    attempt_col = f"{slot} At"
    if str(record.get(attempt_col, "")).strip():
        return
    result = fetch_live_closing_odds({
        "bet_id": record["BetID"], "sport": record["Sport"],
        "book": record["Book"], "team1": record["Team 1"],
        "team2": record["Team 2"], "selection": record["Selection"],
        "bet_type": record["Bet Type"], "market_key": record["Market Key"],
    })
    captured_at = iso(utc_now())
    price = result.get("closing_odds") or ""
    error = result.get("error") or ""
    changes = {
        f"{slot} Price": price,
        attempt_col: captured_at,
        f"{slot} Error": error,
        "Last Error": error,
    }
    update_queue_row(tab, row_idx, changes)
    record.update(changes)
    if slot == "T-1":
        finalize(tab, row_idx, record, latest_sample(record))


def process_queue(tab, now: datetime | None = None):
    now = now or utc_now()
    rows = tab.get_all_values()
    if not rows:
        return
    headers = rows[0]
    for row_idx, row in enumerate(rows[1:], start=2):
        record = row_record(headers, row)
        if not record.get("BetID") or record.get("Status") in FINAL_STATUSES:
            continue
        commence = parse_utc(record.get("Commence UTC", ""))
        if commence is None:
            update_queue_row(tab, row_idx, {"Status": "INVALID", "Last Error": "Invalid Commence UTC"})
            continue
        if now >= commence:
            finalize(tab, row_idx, record, latest_sample(record))
            continue
        slot = active_slot(now, commence)
        if slot:
            capture_record(tab, row_idx, record, slot)


def reconcile_bets(tab):
    """Bootstrap manually-added/fallback Bets rows without frequent full-sheet reads."""
    queue_rows = tab.get_all_values()
    queued_ids = {row[0].strip() for row in queue_rows[1:] if row}
    bets_rows = _get_spreadsheet().worksheet(SHEET_TAB).get_all_values()
    if not bets_rows:
        return 0
    headers = bets_rows[0]
    now = utc_now()
    appended = []
    for row in bets_rows[1:]:
        bet = row_record(headers, row)
        bet_id = bet.get("BetID", "").strip()
        if not bet_id or bet_id in queued_ids or bet.get("ClosingOdds", "").strip():
            continue
        if bet.get("Bet Type") not in AUTOMATED_BET_TYPES:
            continue
        if bet.get("Live Bet", "").strip().upper() == "TRUE":
            continue
        if needs_manual_closing_odds(bet.get("Book", "")):
            continue
        if parse_selection(bet.get("Bet Type", ""), bet.get("Selection", "")) is None:
            continue
        commence = _parse_game_datetime(bet.get("Game Date", ""), bet.get("Game Start Time", ""))
        if commence is None or commence.astimezone(timezone.utc) <= now:
            continue
        created = iso(now)
        values = {
            "BetID": bet_id, "Commence UTC": iso(commence), "Sport": bet.get("Sport", ""),
            "Book": bet.get("Book", ""), "Team 1": bet.get("Team 1", ""),
            "Team 2": bet.get("Team 2", ""), "Selection": bet.get("Selection", ""),
            "Bet Type": bet.get("Bet Type", ""), "OddsTaken": bet.get("OddsTaken", ""),
            "Market Key": bet.get("Market Key", ""), "Status": "PENDING",
            "Created At": created, "Updated At": created,
        }
        appended.append([values.get(header, "") for header in QUEUE_HEADERS])
    if appended:
        tab.append_rows(appended, value_input_option="RAW")
    return len(appended)


def main():
    if not SHEET_ID:
        raise RuntimeError("SHEET_ID is required")
    tab = ensure_queue_tab()
    added = reconcile_bets(tab)
    print(f"[closing-capture] worker started; poll={POLL_SECONDS}s; reconciled={added}")
    next_reconcile = time.monotonic() + RECONCILE_SECONDS
    while True:
        try:
            process_queue(tab)
            if time.monotonic() >= next_reconcile:
                added = reconcile_bets(tab)
                print(f"[closing-capture] reconciliation added {added} row(s)")
                next_reconcile = time.monotonic() + RECONCILE_SECONDS
        except Exception as exc:
            print(f"[closing-capture] loop error: {exc}")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
