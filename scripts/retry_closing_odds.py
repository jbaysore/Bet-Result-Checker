#!/usr/bin/env python3
"""
One-shot retry for ClosingOdds rows marked N/A (or error codes with --include-errors).

Preview buckets each row (skip / manual / retry), then optionally clears and
re-fetches once. Failures restore N/A by default so the cron checker does not
spin forever on hopeless lines.

Usage (from Bet-Result-Checker-github/):
  python scripts/retry_closing_odds.py --dry-run --all-na
  python scripts/retry_closing_odds.py --write --all-na --backfill-market-key
  python scripts/retry_closing_odds.py --write --bet-id 42 --force
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

from closing_odds import fetch_closing_odds, fetch_parlay_closing_odds
from config import SHEET_TAB
from retry_bucketing import classify_closing_retry_row
from sheets_reader import load_bets_for_closing_retry
from sheets_writer import (
    clear_closing_odds_cells,
    write_closing_odds,
    write_market_key_if_blank,
    clear_closing_odds_fail_streak,
)


def _format_preview_row(bet: dict, classification: dict) -> str:
    matchup = f"{bet.get('team1', '')} vs {bet.get('team2', '')}"
    mk = classification.get("inferred_market_key") or ""
    mk_part = f"  mk={mk}" if mk else ""
    return (
        f"  BetID {bet['bet_id']:>6}  [{classification['bucket']:>6}]  "
        f"{bet.get('bet_type', ''):<10}  {bet.get('book', ''):<12}  "
        f"{matchup:<28}  {bet.get('selection', '')[:30]}{mk_part}\n"
        f"           → {classification['reason']}"
    )


def _restore_na(bet: dict) -> bool:
    return write_closing_odds(bet["row_idx"], bet["bet_id"], "N/A", None, None)


def _process_bet(
    bet: dict,
    classification: dict,
    *,
    backfill_market_key: bool,
    leave_error: bool,
) -> dict:
    """Clear, optionally backfill Market Key, fetch once, write result."""
    bet_id = bet["bet_id"]
    row_idx = bet["row_idx"]
    outcome = {"bet_id": bet_id, "status": "failed", "detail": ""}

    if not clear_closing_odds_cells(row_idx, bet_id):
        outcome["detail"] = "could not clear closing columns"
        return outcome

    if backfill_market_key:
        inferred = classification.get("inferred_market_key") or ""
        if inferred and not (bet.get("market_key") or "").strip():
            write_market_key_if_blank(row_idx, bet_id, inferred)
            bet = dict(bet, market_key=inferred)

    try:
        result = (
            fetch_parlay_closing_odds(bet)
            if bet.get("is_parlay")
            else fetch_closing_odds(bet)
        )
    except Exception as e:
        if leave_error:
            write_closing_odds(row_idx, bet_id, "SELECTION NOT FOUND", None, None)
        else:
            _restore_na(bet)
        outcome["detail"] = str(e)
        return outcome

    if result.get("closing_odds") is not None:
        ok = write_closing_odds(
            row_idx,
            bet_id,
            result["closing_odds"],
            result.get("decimal_closing"),
            result.get("clv"),
        )
        if ok:
            clear_closing_odds_fail_streak(row_idx, bet_id)
            outcome["status"] = "success"
            outcome["detail"] = result["closing_odds"]
        else:
            outcome["detail"] = "write skipped (race or existing value)"
        return outcome

    error_code = result.get("error")
    if error_code:
        if leave_error:
            write_closing_odds(row_idx, bet_id, error_code, None, None)
            outcome["detail"] = error_code
        else:
            _restore_na(bet)
            outcome["detail"] = f"restored N/A ({error_code})"
        return outcome

    # Transient — leave blank; user can re-run script or wait for cron
    outcome["detail"] = "transient failure — left blank"
    return outcome


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Preview and one-shot retry ClosingOdds N/A rows.",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Clear and re-fetch rows in the retry bucket (default is preview only)",
    )
    parser.add_argument(
        "--all-na",
        action="store_true",
        help="Scan all rows with ClosingOdds = N/A",
    )
    parser.add_argument(
        "--include-errors",
        action="store_true",
        help="Also include error-code ClosingOdds rows",
    )
    parser.add_argument("--bet-id", help="Process a single BetID")
    parser.add_argument(
        "--backfill-market-key",
        action="store_true",
        help="Stamp inferred Market Key when blank before fetch",
    )
    parser.add_argument(
        "--leave-error",
        action="store_true",
        help="On failure write error code instead of restoring N/A",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Retry even when preview bucket is skip or manual",
    )
    args = parser.parse_args(argv)

    if not args.all_na and not args.bet_id:
        parser.error("Specify --all-na and/or --bet-id")

    include_na = args.all_na or bool(args.bet_id)
    bets = load_bets_for_closing_retry(
        SHEET_TAB,
        include_na=include_na,
        include_errors=args.include_errors,
        bet_id=args.bet_id,
    )

    if not bets:
        print("No matching rows found.")
        return 0

    print(f"Found {len(bets)} row(s) with target ClosingOdds value(s).\n")
    print(f"{'BetID':>8}  {'Bucket':>6}  {'Type':<10}  {'Book':<12}  "
          f"{'Matchup':<28}  Selection")
    print("-" * 100)

    all_classified: list[tuple[dict, dict]] = []
    for bet in bets:
        classification = classify_closing_retry_row(bet)
        all_classified.append((bet, classification))
        print(_format_preview_row(bet, classification))
        print()

    to_process = [
        (bet, cls)
        for bet, cls in all_classified
        if cls["bucket"] == "retry" or (args.force and cls["bucket"] in ("skip", "manual"))
    ]

    if not args.write:
        print(f"Summary: {len(to_process)} row(s) would be retried"
              f"{'' if not args.force else ' (with --force)'}; "
              f"{len(bets) - len(to_process)} skipped.")
        print("Run with --write to execute retries.")
        return 0

    if not to_process:
        print("Nothing to write — no rows in the retry bucket.")
        return 0

    print(f"\nWriting {len(to_process)} row(s)...\n")
    results = {"success": 0, "failed": 0}
    for bet, classification in to_process:
        print(f"── BetID {bet['bet_id']} ──")
        outcome = _process_bet(
            bet,
            classification,
            backfill_market_key=args.backfill_market_key,
            leave_error=args.leave_error,
        )
        print(f"  → {outcome['status']}: {outcome['detail']}")
        results[outcome["status"]] = results.get(outcome["status"], 0) + 1
        print()

    print(f"Done: {results.get('success', 0)} succeeded, "
          f"{results.get('failed', 0)} failed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
