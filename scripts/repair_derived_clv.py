#!/usr/bin/env python3
"""Fill missing decimal/CLV cells from existing American odds; never re-fetch."""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

import gspread

from closing_odds import _clv_from_decimals, to_decimal_odds
from config import BET_COL, SHEET_TAB
from sheets_reader import _get_spreadsheet


def _parse_american(value) -> float | None:
    try:
        return float(str(value or "").replace("+", "").strip())
    except ValueError:
        return None


def repair_bet(bet_id: str, *, write: bool) -> dict:
    sheet = _get_spreadsheet().worksheet(SHEET_TAB)
    rows = sheet.get_all_values()
    headers = rows[0]
    required = [
        BET_COL["bet_id"], BET_COL["odds_taken"], BET_COL["decimal_odds"],
        BET_COL["closing_odds"], BET_COL["decimal_closing"], BET_COL["clv"],
    ]
    missing = [header for header in required if header not in headers]
    if missing:
        raise RuntimeError(f"Bets tab missing columns: {', '.join(missing)}")
    col = {header: headers.index(header) for header in required}
    matches = []
    for row_index, raw in enumerate(rows[1:], start=2):
        row = raw + [""] * max(0, len(headers) - len(raw))
        if str(row[col[BET_COL["bet_id"]]] or "").strip() == bet_id:
            matches.append((row_index, row))
    if len(matches) != 1:
        return {"bet_id": bet_id, "status": "refused",
                "detail": f"expected one row, found {len(matches)}"}

    row_index, row = matches[0]
    taken = _parse_american(row[col[BET_COL["odds_taken"]]])
    closing = _parse_american(row[col[BET_COL["closing_odds"]]])
    decimal_taken = to_decimal_odds(taken)
    decimal_closing = to_decimal_odds(closing)
    clv = _clv_from_decimals(decimal_taken, decimal_closing)
    if None in (decimal_taken, decimal_closing, clv):
        return {"bet_id": bet_id, "status": "refused",
                "detail": "OddsTaken or ClosingOdds is not numeric"}

    existing = {
        "decimal_taken": row[col[BET_COL["decimal_odds"]]],
        "decimal_closing": row[col[BET_COL["decimal_closing"]]],
        "clv": row[col[BET_COL["clv"]]],
    }
    if any(str(value or "").strip() for value in existing.values()):
        return {"bet_id": bet_id, "status": "refused",
                "detail": f"derived cell already populated: {existing}"}

    proposed = {
        "decimal_taken": decimal_taken,
        "decimal_closing": decimal_closing,
        "clv": clv,
    }
    if write:
        fresh = sheet.row_values(row_index)
        fresh += [""] * max(0, len(headers) - len(fresh))
        if str(fresh[col[BET_COL["bet_id"]]] or "").strip() != bet_id:
            return {"bet_id": bet_id, "status": "refused", "detail": "BetID moved before write"}
        if any(str(fresh[col[header]] or "").strip() for header in (
            BET_COL["decimal_odds"], BET_COL["decimal_closing"], BET_COL["clv"],
        )):
            return {"bet_id": bet_id, "status": "refused", "detail": "derived cells changed before write"}
        sheet.update_cells([
            gspread.Cell(row_index, col[BET_COL["decimal_odds"]] + 1, decimal_taken),
            gspread.Cell(row_index, col[BET_COL["decimal_closing"]] + 1, decimal_closing),
            gspread.Cell(row_index, col[BET_COL["clv"]] + 1, clv),
        ])
    return {"bet_id": bet_id, "row_index": row_index,
            "status": "written" if write else "preview", "values": proposed}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Repair derived CLV cells from existing odds.")
    parser.add_argument("--bet-id", required=True)
    parser.add_argument("--write", action="store_true", help="Apply the repair")
    args = parser.parse_args(argv)
    result = repair_bet(args.bet_id.strip(), write=args.write)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] in {"preview", "written"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
