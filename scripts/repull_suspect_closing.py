#!/usr/bin/env python3
"""
One-shot re-pull for audit-flagged legacy closing odds (CLV_ACCURACY_PLAN
Phase 6 follow-up).

Targets Bets rows where Start Status = LEGACY_UNAUDITED and Start Audit is
LIKELY_SUSPECT or INDETERMINATE. Each row's Actual Start was stamped by
scripts/clv_start_audit.py, so a re-fetch snapshots at actual_start − margin
(the actual-start-aware importer path) instead of the poisoned scheduled − 1
minute, and writes back VERIFIED_CLOSE + Start Status = VERIFIED provenance —
re-admitting the row to pooled CLV.

On any failure the row's original ClosingOdds/DecimalClosingOdds/CLV are
restored with its LEGACY_UNAUDITED provenance intact, so a failed run leaves
the sheet exactly as it was. Start Audit is never modified — it remains the
historical record of what the audit found.

Usage (from Bet-Result-Checker-github/):
  python scripts/repull_suspect_closing.py                  # preview only
  python scripts/repull_suspect_closing.py --write          # execute
  python scripts/repull_suspect_closing.py --write --bucket LIKELY_SUSPECT
  python scripts/repull_suspect_closing.py --write --bet-id 42
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Windows consoles default to cp1252, which can't render the dashes/arrows in
# team names and status lines. Force UTF-8 like the sheet data itself.
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv

load_dotenv()

DEFAULT_BUCKETS = ("LIKELY_SUSPECT", "INDETERMINATE")
REPORT_PATH = "repull_suspect_report.json"


def parse_sheet_number(raw) -> float | None:
    """Formatted sheet cell → float. '5.43%' → 0.0543 (CLV percent format)."""
    text = str(raw or "").strip()
    if not text:
        return None
    percent = text.endswith("%")
    if percent:
        text = text[:-1].strip()
    try:
        value = float(text.replace(",", ""))
    except ValueError:
        return None
    return value / 100 if percent else value


def restore_provenance(bet: dict) -> dict:
    """Provenance payload that puts a failed row back in its pre-run state."""
    return {
        "start_status": bet.get("start_status", "") or "LEGACY_UNAUDITED",
        "closing_quality": bet.get("closing_quality", ""),
        "closing_source": bet.get("closing_source", ""),
        "closing_observed_at": bet.get("closing_observed_at", ""),
        "start_detected_at": bet.get("start_detected_at", ""),
        "actual_start": bet.get("actual_start", ""),
        "actual_start_source": bet.get("actual_start_source", ""),
        "actual_start_confidence": bet.get("actual_start_confidence", ""),
    }


def classify_repull_row(bet: dict, buckets: tuple[str, ...] = DEFAULT_BUCKETS) -> dict:
    """
    skip/retry decision for one loaded Bets row. Pure — unit-tested.

    Rows must be legacy + in a target audit bucket. Parlays re-resolve actual
    start per leg inside fetch_parlay_closing_odds, so the row-level confidence
    only gates singles.
    """
    start_status = str(bet.get("start_status") or "").strip().upper()
    start_audit = str(bet.get("start_audit") or "").strip().upper()
    result = str(bet.get("result") or "").strip().upper()
    closing = str(bet.get("closing_odds") or "").strip()
    closing_quality = str(bet.get("closing_quality") or "").strip().upper()

    # An explicitly selected row can already have a verified actual start but
    # still be excluded because its only capture is stale. Re-pulling at the
    # retained actual start is the same guarded operation as a suspect legacy
    # row; global bucket runs never load these rows.
    if start_status == "VERIFIED" and closing_quality == "STALE":
        confidence = str(bet.get("actual_start_confidence") or "").strip().upper()
        if not str(bet.get("actual_start") or "").strip() or confidence != "CONFIDENT":
            return {"bucket": "manual", "reason": "STALE row lacks a confident actual start"}
        return {"bucket": "retry", "reason": "verified start with STALE capture"}

    if start_status != "LEGACY_UNAUDITED":
        return {"bucket": "skip", "reason": f"Start Status is '{start_status or '(blank)'}', not LEGACY_UNAUDITED"}
    if start_audit not in buckets:
        return {"bucket": "skip", "reason": f"Start Audit '{start_audit or '(blank)'}' not in target buckets"}
    if result == "VOID" or closing.upper() == "VOID":
        return {"bucket": "skip", "reason": "VOID bet — no CLV to repair"}
    if bet.get("is_parlay"):
        if not bet.get("legs"):
            return {"bucket": "manual", "reason": "parlay legs unparseable — repair by hand"}
        return {"bucket": "retry", "reason": "parlay — per-leg actual-start re-pull"}
    confidence = str(bet.get("actual_start_confidence") or "").strip().upper()
    if not str(bet.get("actual_start") or "").strip() or confidence != "CONFIDENT":
        return {"bucket": "manual",
                "reason": f"actual start missing or not CONFIDENT ({confidence or 'blank'}) — should not happen in these buckets"}
    return {"bucket": "retry", "reason": "single — snapshot at actual_start − margin"}


def load_audit_flagged_bets(buckets: tuple[str, ...], bet_id: str | None = None) -> list[dict]:
    """Bets rows in the target audit buckets, with their current closing trio."""
    from config import SHEET_TAB, BET_TYPE_PARLAY
    from parlay import parse_legs, all_legs_automatable
    from sheets_reader import (
        _get_bets_rows, _resolve_bet_col_indices, _pad_bet_row, _bet_cell,
        _duplicate_bet_ids,
    )

    rows = _get_bets_rows(SHEET_TAB)
    if not rows:
        return []
    headers = rows[0]
    col = _resolve_bet_col_indices(headers)
    for required in ("bet_id", "closing_odds", "start_status", "start_audit"):
        if col.get(required) is None:
            raise RuntimeError(f"Bets tab is missing the '{required}' column — run the migration/audit first.")

    duplicate_ids = _duplicate_bet_ids(rows, col)
    target_id = str(bet_id).strip() if bet_id is not None else None
    bets = []
    for row_idx, row in enumerate(rows[1:], start=2):
        row = _pad_bet_row(row, col)
        this_id = _bet_cell(row, col, "bet_id")
        if not this_id or this_id in duplicate_ids:
            continue
        if target_id is not None and this_id != target_id:
            continue
        start_audit = _bet_cell(row, col, "start_audit").strip().upper()
        if target_id is None and start_audit not in buckets:
            continue

        bet_type = _bet_cell(row, col, "bet_type")
        is_parlay = bet_type == BET_TYPE_PARLAY
        legs = []
        if is_parlay:
            legs = parse_legs(_bet_cell(row, col, "legs"))
            if legs and not all_legs_automatable(legs):
                legs = []

        bets.append({
            "row_idx": row_idx,
            "bet_id": this_id,
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
            "is_parlay": is_parlay,
            "legs": legs,
            "kalshi_ticker": _bet_cell(row, col, "kalshi_ticker"),
            "market_key": _bet_cell(row, col, "market_key"),
            "event_id": _bet_cell(row, col, "event_id"),
            "actual_start": _bet_cell(row, col, "actual_start"),
            "actual_start_source": _bet_cell(row, col, "actual_start_source"),
            "actual_start_confidence": _bet_cell(row, col, "actual_start_confidence"),
            "start_status": _bet_cell(row, col, "start_status"),
            "start_audit": start_audit,
            "closing_odds": _bet_cell(row, col, "closing_odds"),
            "decimal_closing": _bet_cell(row, col, "decimal_closing"),
            "clv": _bet_cell(row, col, "clv"),
            "closing_quality": _bet_cell(row, col, "closing_quality"),
            "closing_source": _bet_cell(row, col, "closing_source"),
            "closing_observed_at": _bet_cell(row, col, "closing_observed_at"),
            "start_detected_at": _bet_cell(row, col, "start_detected_at"),
        })
    return bets


def _with_retries(fn, attempts: int = 3, base_sleep: float = 30.0) -> bool:
    """Retry a boolean sheet operation across Sheets per-minute quota windows."""
    for attempt in range(attempts):
        if fn():
            return True
        if attempt < attempts - 1:
            time.sleep(base_sleep * (attempt + 1))
    return False


def repull_bet(bet: dict) -> dict:
    """Clear → actual-start-aware fetch → guarded write; restore original on failure."""
    from closing_odds import fetch_closing_odds, fetch_parlay_closing_odds
    from sheets_writer import (
        clear_closing_odds_cells, write_closing_odds, write_closing_capture_audit,
    )
    try:
        from scripts.retry_closing_odds import _retry_provenance
    except ImportError:  # direct `py scripts/...` invocation
        from retry_closing_odds import _retry_provenance

    bet_id = bet["bet_id"]
    row_idx = bet["row_idx"]
    outcome = {
        "bet_id": bet_id,
        "start_audit": bet.get("start_audit", ""),
        "old_closing": bet.get("closing_odds", ""),
        "old_clv": bet.get("clv", ""),
        "status": "failed",
        "detail": "",
    }

    original = (
        bet.get("closing_odds", ""),
        parse_sheet_number(bet.get("decimal_closing")),
        parse_sheet_number(bet.get("clv")),
    )

    def restore(reason: str) -> dict:
        restored = _with_retries(lambda: write_closing_odds(
            row_idx, bet_id, original[0], original[1], original[2],
            provenance=restore_provenance(bet),
        ))
        outcome["detail"] = f"{reason} — original value {'restored' if restored else 'RESTORE FAILED'}"
        if not restored:
            outcome["status"] = "restore_failed"
        return outcome

    if not _with_retries(lambda: clear_closing_odds_cells(row_idx, bet_id)):
        outcome["detail"] = "could not clear closing columns (row untouched)"
        return outcome

    fetch_bet = {**bet, "market_key": "", "_resolve_actual_start": True}
    try:
        result = (
            fetch_parlay_closing_odds(fetch_bet)
            if fetch_bet.get("is_parlay")
            else fetch_closing_odds(fetch_bet)
        )
    except Exception as exc:  # noqa: BLE001 — restore then surface
        return restore(f"fetch raised: {exc}")

    if result.get("closing_odds") is None:
        return restore(result.get("error") or "transient failure")

    wrote = _with_retries(lambda: write_closing_odds(
        row_idx, bet_id,
        result["closing_odds"], result.get("decimal_closing"), result.get("clv"),
        provenance=_retry_provenance(result),
    ))
    if not wrote:
        return restore("final write kept failing")
    if result.get("per_leg_audit"):
        write_closing_capture_audit(bet_id, {"legs": result["per_leg_audit"]})

    outcome["status"] = "success"
    outcome["new_closing"] = result["closing_odds"]
    outcome["new_clv"] = result.get("clv")
    outcome["closing_quality"] = result.get("closing_quality", "")
    outcome["detail"] = (
        f"{bet.get('closing_odds', '') or '(blank)'} -> {result['closing_odds']}"
        f" (quality {result.get('closing_quality', '?')})"
    )
    return outcome


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Re-pull audit-flagged legacy closing odds.")
    parser.add_argument("--write", action="store_true",
                        help="Execute the re-pull (default is preview only)")
    parser.add_argument("--bucket", action="append", choices=DEFAULT_BUCKETS,
                        help="Restrict to one audit bucket (repeatable; default both)")
    parser.add_argument("--bet-id", help="Process a single BetID regardless of bucket")
    parser.add_argument("--report", default=REPORT_PATH,
                        help=f"Report file path (default {REPORT_PATH})")
    parser.add_argument("--pace", type=float, default=5.0,
                        help="Seconds to sleep between rows (Sheets write quota is 60/min)")
    args = parser.parse_args(argv)

    buckets = tuple(args.bucket) if args.bucket else DEFAULT_BUCKETS
    bets = load_audit_flagged_bets(buckets, bet_id=args.bet_id)
    if not bets:
        print("No matching rows found.")
        return 0

    print(f"Found {len(bets)} row(s) in bucket(s): {', '.join(buckets)}\n")
    classified = [(bet, classify_repull_row(bet, buckets if not args.bet_id else
                                            ("LIKELY_SUSPECT", "INDETERMINATE", bet.get("start_audit", "").upper())))
                  for bet in bets]
    for bet, cls in classified:
        matchup = f"{bet.get('team1', '')} vs {bet.get('team2', '')}"
        print(f"  BetID {bet['bet_id']:>6}  [{cls['bucket']:>6}]  {bet.get('start_audit', ''):<14} "
              f"{bet.get('bet_type', ''):<10} {bet.get('book', ''):<12} {matchup:<30} "
              f"close={bet.get('closing_odds', '') or '(blank)'}  clv={bet.get('clv', '')}")
        print(f"           -> {cls['reason']}")

    to_process = [(bet, cls) for bet, cls in classified if cls["bucket"] == "retry"]
    skipped = len(classified) - len(to_process)
    if not args.write:
        print(f"\nPreview: {len(to_process)} row(s) would be re-pulled; {skipped} skipped/manual.")
        print("Run with --write to execute.")
        return 0
    if not to_process:
        print("\nNothing to write — no rows in the retry bucket.")
        return 0

    print(f"\nRe-pulling {len(to_process)} row(s) (pace {args.pace}s)...\n")
    outcomes = []
    for position, (bet, _) in enumerate(to_process):
        if position and args.pace > 0:
            time.sleep(args.pace)
        print(f"── BetID {bet['bet_id']} ({bet.get('start_audit', '')}) ──")
        outcome = repull_bet(bet)
        outcomes.append(outcome)
        print(f"  -> {outcome['status']}: {outcome['detail']}\n")

    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "buckets": list(buckets),
        "processed": len(outcomes),
        "success": sum(1 for item in outcomes if item["status"] == "success"),
        "failed": sum(1 for item in outcomes if item["status"] == "failed"),
        "restore_failed": sum(1 for item in outcomes if item["status"] == "restore_failed"),
        "skipped_or_manual": skipped,
        "outcomes": outcomes,
    }
    with open(args.report, "w", encoding="utf-8") as handle:
        json.dump(summary, handle, indent=2, sort_keys=True)
    print(f"Done: {summary['success']} succeeded, {summary['failed']} failed, "
          f"{summary['restore_failed']} restore-failed. Report: {args.report}")
    return 0 if summary["failed"] + summary["restore_failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
