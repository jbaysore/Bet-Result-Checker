"""Flag-only historical CLV start audit; never changes ClosingOdds or CLV."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Support the documented direct invocation from the repository root:
# `py scripts/clv_start_audit.py`.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import gspread

from actual_start import resolve_actual_start
from closing_odds import _parse_game_datetime
from config import BET_COL, CLOSING_ODDS_ERROR_CODES, SHEET_TAB
from closing_provenance import (
    QUALITY_EARLY, QUALITY_MANUAL, QUALITY_PROVISIONAL, QUALITY_STALE, QUALITY_VERIFIED,
)
from sheets_reader import _get_spreadsheet


SNAPSHOT_INTERVAL = timedelta(minutes=5)


# ── Concept operational-state report buckets (plan Phase 2 / concept "Reporting
# principles") ──────────────────────────────────────────────────────────────
# A total classification of every Bets row into ONE meaningful next-action
# state, so upcoming events never inflate the historical-failure count and each
# count maps to an action. Distinct from the LEGACY start-audit buckets above.
BUCKET_BY_DESIGN = "by_design"          # live/prop/void/cashout — no CLV by design
BUCKET_RETIRED = "retired"              # user-excluded context
BUCKET_PENDING = "pending"             # upcoming event, not yet due
BUCKET_MANUAL = "manual"               # manual/book-specific or user-attested
BUCKET_REPAIRED = "repaired"           # recovered close, provenance-marked
BUCKET_BLOCKED = "blocked"             # stable automatic limitation (no start source)
BUCKET_TRUSTED = "trusted"             # VERIFIED_CLOSE with a benchmark → pooled CLV
BUCKET_UNBENCHMARKABLE = "unbenchmarkable"  # trusted close, no compatible benchmark
BUCKET_OBSERVING = "observing"         # new-context provisional (onboarding: marker)
BUCKET_CAPTURING = "capturing"         # started, close not captured yet
BUCKET_RECOVERABLE = "recoverable"     # completed, close missing/error, recoverable
BUCKET_PROVISIONAL = "provisional"     # provisional for timing reasons (fallback)

REPORT_FAILURE_BUCKETS = frozenset({BUCKET_BLOCKED, BUCKET_RECOVERABLE})


def classify_report_bucket(row: dict, now: datetime) -> str:
    """Map one Bets row to exactly one operational bucket. `row` uses lowercase
    keys (result, closing_quality, closing_source, closing_odds, pinnacle_close,
    bet_type, live_bet, notes) plus a parsed `commence_dt` (datetime|None)."""
    result = str(row.get("result") or "").strip().upper()
    quality = str(row.get("closing_quality") or "").strip().upper()
    source = str(row.get("closing_source") or "").strip().lower()
    closing_odds = str(row.get("closing_odds") or "").strip()
    pinnacle = str(row.get("pinnacle_close") or "").strip()
    bet_type = str(row.get("bet_type") or "").strip().lower()
    live = str(row.get("live_bet") or "").strip().upper() in ("TRUE", "YES", "1")
    notes = str(row.get("notes") or "")
    commence = row.get("commence_dt")

    if live or bet_type == "prop" or result in ("VOID", "CASHOUT"):
        return BUCKET_BY_DESIGN
    if "retired:" in notes:
        return BUCKET_RETIRED
    if commence is not None and commence > now and not result:
        return BUCKET_PENDING
    if quality == QUALITY_MANUAL or closing_odds.upper() == "MANUAL ENTRY" \
            or "manual-evidence:" in notes:
        return BUCKET_MANUAL
    if source == "recovery-onboarding":
        return BUCKET_REPAIRED
    if "onboarding:" in notes and "blocked" in notes.lower():
        return BUCKET_BLOCKED
    if quality == QUALITY_VERIFIED:
        return BUCKET_TRUSTED if pinnacle else BUCKET_UNBENCHMARKABLE
    if "onboarding:" in notes:
        return BUCKET_OBSERVING
    if not closing_odds:
        return BUCKET_CAPTURING
    if closing_odds in CLOSING_ODDS_ERROR_CODES:
        return BUCKET_RECOVERABLE
    if quality in (QUALITY_PROVISIONAL, QUALITY_EARLY, QUALITY_STALE):
        return BUCKET_PROVISIONAL
    return BUCKET_PROVISIONAL


def report_buckets(rows: list[list[str]], headers: list[str], now: datetime | None = None) -> dict:
    """Tally every data row into operational buckets."""
    now = now or datetime.now(timezone.utc)
    idx = {header: headers.index(header) for header in headers}

    def cell(row, key):
        i = idx.get(BET_COL.get(key, ""))
        return row[i] if i is not None and i < len(row) else ""

    def header_cell(row, header):
        i = idx.get(header)
        return row[i] if i is not None and i < len(row) else ""

    buckets = Counter()
    for raw in rows:
        row = raw + [""] * max(0, len(headers) - len(raw))
        if not str(cell(row, "bet_id") or "").strip():
            continue
        commence = _parse_game_datetime(cell(row, "game_date"), cell(row, "game_start"))
        bucket = classify_report_bucket({
            "result": cell(row, "result"), "closing_quality": cell(row, "closing_quality"),
            "closing_source": cell(row, "closing_source"), "closing_odds": cell(row, "closing_odds"),
            "pinnacle_close": cell(row, "pinnacle_close"), "bet_type": cell(row, "bet_type"),
            "live_bet": header_cell(row, "Live Bet"), "notes": cell(row, "notes"),
            "commence_dt": commence.astimezone(timezone.utc) if commence else None,
        }, now)
        buckets[bucket] += 1
    return {"rows": sum(buckets.values()), "buckets": dict(buckets),
            "generated_at": now.isoformat().replace("+00:00", "Z")}


def classify_start_audit(actual_start: datetime | None, scheduled_start: datetime | None) -> str:
    if actual_start is None or scheduled_start is None:
        return "UNRESOLVABLE"
    requested = scheduled_start - timedelta(minutes=1)
    suspect_boundary = requested - SNAPSHOT_INTERVAL
    if actual_start > requested:
        return "SAFE"
    if actual_start < suspect_boundary:
        return "LIKELY_SUSPECT"
    return "INDETERMINATE"


def _float(value):
    try:
        return float(str(value or "").replace("%", "").strip())
    except ValueError:
        return None


def summarize_clv_buckets(clv_by_bucket: dict[str, list[float]]) -> dict:
    all_values = [value for values in clv_by_bucket.values() for value in values]

    def summary(values):
        return {
            "count": len(values),
            "average": sum(values) / len(values) if values else None,
        }

    return {
        "all": summary(all_values),
        "without_bucket": {
            bucket: summary([
                value for other, values in clv_by_bucket.items()
                if other != bucket for value in values
            ])
            for bucket in clv_by_bucket
        },
    }


def run_audit(*, write=True, bet_ids: set[str] | None = None,
              include_details: bool = False) -> dict:
    requested_ids = {str(value).strip() for value in (bet_ids or set()) if str(value).strip()}
    sheet = _get_spreadsheet().worksheet(SHEET_TAB)
    rows = sheet.get_all_values()
    if not rows:
        return {
            "rows": 0, "buckets": {}, "requested_bet_ids": sorted(requested_ids),
            "matched_bet_ids": [], "unmatched_bet_ids": sorted(requested_ids),
        }
    headers = rows[0]
    required = [
        BET_COL["start_status"],
        BET_COL["actual_start"], BET_COL["actual_start_source"],
        BET_COL["actual_start_confidence"], BET_COL["start_audit"],
    ]
    missing = [header for header in required if header not in headers]
    if missing:
        raise RuntimeError(f"Bets tab missing audit columns: {', '.join(missing)}")
    idx = {header: headers.index(header) for header in headers}
    cells = []
    buckets = Counter()
    deltas = []
    clv_by_bucket: dict[str, list[float]] = {}
    matched_ids = set()
    details = []
    for row_idx, raw in enumerate(rows[1:], start=2):
        row = raw + [""] * max(0, len(headers) - len(raw))
        row_bet_id = str(row[idx.get(BET_COL["bet_id"], 0)] or "").strip()
        if not row_bet_id:
            continue
        if requested_ids and row_bet_id not in requested_ids:
            continue
        if str(row[idx[BET_COL["start_status"]]] or "").strip().upper() != "LEGACY_UNAUDITED":
            continue
        matched_ids.add(row_bet_id)
        bet = {
            "sport": row[idx[BET_COL["sport"]]], "team1": row[idx[BET_COL["team1"]]],
            "team2": row[idx[BET_COL["team2"]]], "game_date": row[idx[BET_COL["game_date"]]],
            "game_start": row[idx[BET_COL["game_start"]]],
            "event_id": row[idx[BET_COL["event_id"]]],
            "actual_start": row[idx[BET_COL["actual_start"]]],
            "actual_start_source": row[idx[BET_COL["actual_start_source"]]],
            "actual_start_confidence": row[idx[BET_COL["actual_start_confidence"]]],
        }
        resolution = resolve_actual_start(bet)
        scheduled = _parse_game_datetime(bet["game_date"], bet["game_start"])
        actual = resolution.actual_start
        bucket = classify_start_audit(actual, scheduled)
        if include_details:
            details.append({
                "bet_id": row_bet_id,
                "sport": bet["sport"],
                "team1": bet["team1"],
                "team2": bet["team2"],
                "scheduled_start": scheduled.isoformat() if scheduled else None,
                "actual_start": actual.isoformat() if actual else None,
                "source": resolution.source,
                "confidence": resolution.confidence,
                "event_id": resolution.event_id,
                "error": resolution.error,
                "bucket": bucket,
                "delta_seconds": (
                    (actual - scheduled.astimezone(timezone.utc)).total_seconds()
                    if actual and scheduled else None
                ),
            })
        buckets[bucket] += 1
        if actual and scheduled:
            deltas.append((actual - scheduled.astimezone(timezone.utc)).total_seconds())
        clv = _float(row[idx[BET_COL["clv"]]]) if BET_COL["clv"] in idx else None
        if clv is not None:
            clv_by_bucket.setdefault(bucket, []).append(clv)
        if write:
            values = {
                BET_COL["actual_start"]: actual.isoformat().replace("+00:00", "Z") if actual else "",
                BET_COL["actual_start_source"]: resolution.source,
                BET_COL["actual_start_confidence"]: resolution.confidence,
                BET_COL["start_audit"]: bucket,
            }
            cells.extend(gspread.Cell(row_idx, idx[header] + 1, value) for header, value in values.items())
    if write and cells:
        sheet.update_cells(cells)
    sorted_deltas = sorted(deltas)
    report = {
        "rows": sum(buckets.values()), "buckets": dict(buckets),
        "delta_seconds": {
            "min": sorted_deltas[0] if sorted_deltas else None,
            "median": sorted_deltas[len(sorted_deltas) // 2] if sorted_deltas else None,
            "max": sorted_deltas[-1] if sorted_deltas else None,
        },
        "clv_by_bucket": {
            bucket: {"count": len(values), "average": sum(values) / len(values)}
            for bucket, values in clv_by_bucket.items() if values
        },
        "aggregate_clv": summarize_clv_buckets(clv_by_bucket),
        "clv_pooled_safe_only": ["SAFE"],
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "requested_bet_ids": sorted(requested_ids),
        "matched_bet_ids": sorted(matched_ids),
        "unmatched_bet_ids": sorted(requested_ids - matched_ids),
    }
    if include_details:
        report["details"] = details
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit legacy CLV start timing.")
    parser.add_argument(
        "--bet-id",
        action="append",
        help="Audit a BetID (repeat for an exact batch); default audits all legacy rows",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Resolve and report without writing audit cells",
    )
    parser.add_argument(
        "--details",
        action="store_true",
        help="Include per-row resolution details in the JSON report",
    )
    parser.add_argument(
        "--buckets",
        action="store_true",
        help="Report the concept operational-state buckets over all rows (read-only)",
    )
    args = parser.parse_args(argv)
    if args.buckets:
        rows = _get_spreadsheet().worksheet(SHEET_TAB).get_all_values()
        report = report_buckets(rows[1:], rows[0]) if rows else {"rows": 0, "buckets": {}}
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    report = run_audit(
        write=not args.dry_run,
        bet_ids=set(args.bet_id or []),
        include_details=args.details,
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if not report["unmatched_bet_ids"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
