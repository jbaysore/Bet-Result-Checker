#!/usr/bin/env python3
"""Save a BetID-addressed JSON snapshot of live Bets rows before recovery."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

from config import BET_COL, SHEET_TAB
from sheets_reader import _get_bets_rows


def snapshot_rows(requested_ids: set[str]) -> dict:
    rows = _get_bets_rows(SHEET_TAB)
    if not rows:
        raise RuntimeError("Bets tab is empty")
    headers = rows[0]
    if BET_COL["bet_id"] not in headers:
        raise RuntimeError("Bets tab is missing BetID")
    bet_id_index = headers.index(BET_COL["bet_id"])
    matches: dict[str, list[dict]] = {bet_id: [] for bet_id in requested_ids}
    for row_index, raw in enumerate(rows[1:], start=2):
        row = raw + [""] * max(0, len(headers) - len(raw))
        bet_id = str(row[bet_id_index] or "").strip()
        if bet_id in matches:
            matches[bet_id].append({
                "row_index": row_index,
                "values": dict(zip(headers, row)),
            })
    duplicate_ids = sorted(bet_id for bet_id, values in matches.items() if len(values) > 1)
    missing_ids = sorted(bet_id for bet_id, values in matches.items() if not values)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "sheet_tab": SHEET_TAB,
        "requested_bet_ids": sorted(requested_ids),
        "missing_bet_ids": missing_ids,
        "duplicate_bet_ids": duplicate_ids,
        "rows": {bet_id: values[0] for bet_id, values in matches.items() if len(values) == 1},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Snapshot live Bets rows by BetID.")
    parser.add_argument("--bet-id", action="append", required=True,
                        help="BetID to snapshot (repeatable)")
    parser.add_argument("--output", required=True, help="JSON output path")
    args = parser.parse_args(argv)
    report = snapshot_rows({value.strip() for value in args.bet_id if value.strip()})
    with open(args.output, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, sort_keys=True)
    print(json.dumps({
        "output": args.output,
        "rows": len(report["rows"]),
        "missing_bet_ids": report["missing_bet_ids"],
        "duplicate_bet_ids": report["duplicate_bet_ids"],
    }, indent=2, sort_keys=True))
    return 0 if not report["missing_bet_ids"] and not report["duplicate_bet_ids"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
