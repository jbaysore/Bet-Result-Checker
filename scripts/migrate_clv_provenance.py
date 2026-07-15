"""One-time live-sheet migration for actual-start-aware CLV provenance."""

from __future__ import annotations

from datetime import datetime, timezone

import gspread

from config import BET_COL, SHEET_TAB
from sheets_reader import _get_spreadsheet


MIGRATIONS_TAB = "SchemaMigrations"
MIGRATION_KEY = "clv-actual-start-v1"
NEW_BETS_HEADERS = [
    BET_COL[key] for key in (
        "event_id", "start_status", "closing_quality", "closing_source",
        "closing_observed_at", "start_detected_at", "actual_start",
        "actual_start_source", "actual_start_confidence", "start_audit",
        "pinnacle_close", "pinnacle_clv",
    )
]


def migrate() -> dict:
    spreadsheet = _get_spreadsheet()
    bets = spreadsheet.worksheet(SHEET_TAB)
    headers = bets.row_values(1)
    missing = [header for header in NEW_BETS_HEADERS if header not in headers]
    if missing:
        headers.extend(missing)
        if bets.col_count < len(headers):
            bets.resize(cols=len(headers))
        bets.update([headers], "A1")

    rows = bets.get_all_values()
    status_col = headers.index(BET_COL["start_status"]) + 1
    cells = []
    for row_idx, row in enumerate(rows[1:], start=2):
        bet_id = row[headers.index(BET_COL["bet_id"])] if len(row) > headers.index(BET_COL["bet_id"]) else ""
        existing = row[status_col - 1] if len(row) >= status_col else ""
        if str(bet_id).strip() and not str(existing).strip():
            cells.append(gspread.Cell(row_idx, status_col, "LEGACY_UNAUDITED"))
    if cells:
        bets.update_cells(cells)

    try:
        marker = spreadsheet.worksheet(MIGRATIONS_TAB)
    except gspread.WorksheetNotFound:
        marker = spreadsheet.add_worksheet(title=MIGRATIONS_TAB, rows=100, cols=3)
        marker.update([["Migration", "Applied At", "Details"]], "A1")
    existing_keys = {row[0] for row in marker.get_all_values()[1:] if row}
    if MIGRATION_KEY not in existing_keys:
        marker.append_row([
            MIGRATION_KEY, datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            f"appended {len(missing)} headers; backfilled {len(cells)} legacy rows",
        ])
    return {"headers_appended": len(missing), "legacy_rows": len(cells)}


if __name__ == "__main__":
    print(migrate())
