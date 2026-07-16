"""Add the optional `Context ID` column to the Bets tab (plan Gate P0 / Phase 3).

Non-destructive: appends ONE header cell after the current last column and
touches no existing data. Idempotent — does nothing if the header already
exists. Everything reads Bets by header name, so an empty trailing column is
inert until Phase 3 wires resolution into the at-log path.

Run:  py scripts/add_bets_context_id_column.py
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

CONTEXT_ID_HEADER = "Context ID"


def main() -> int:
    from config import SHEET_TAB
    from sheets_quota import call_with_sheets_retry
    from sheets_reader import _get_spreadsheet

    tab = call_with_sheets_retry(
        f"worksheet({SHEET_TAB})", _get_spreadsheet().worksheet, SHEET_TAB)
    headers = call_with_sheets_retry("Bets row_values(1)", tab.row_values, 1)

    if CONTEXT_ID_HEADER in headers:
        print(f"SKIP — {SHEET_TAB!r} already has a {CONTEXT_ID_HEADER!r} column "
              f"(col {headers.index(CONTEXT_ID_HEADER) + 1}).")
        return 0

    target_col = len(headers) + 1
    # The grid may be sized to exactly the current column count; widen it before
    # writing past the last column.
    if tab.col_count < target_col:
        call_with_sheets_retry(
            f"{SHEET_TAB} add_cols", tab.add_cols, target_col - tab.col_count)
    call_with_sheets_retry(
        f"{SHEET_TAB} add header", tab.update_cell, 1, target_col, CONTEXT_ID_HEADER)
    print(f"Added {CONTEXT_ID_HEADER!r} to {SHEET_TAB!r} at column {target_col} "
          f"(was {len(headers)} columns). Empty and inert until Phase 3.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
