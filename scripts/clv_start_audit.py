"""Flag-only historical CLV start audit; never changes ClosingOdds or CLV."""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta, timezone

import gspread

from actual_start import resolve_actual_start
from closing_odds import _parse_game_datetime
from config import BET_COL, SHEET_TAB
from sheets_reader import _get_spreadsheet


SNAPSHOT_INTERVAL = timedelta(minutes=5)


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


def run_audit(*, write=True) -> dict:
    sheet = _get_spreadsheet().worksheet(SHEET_TAB)
    rows = sheet.get_all_values()
    if not rows:
        return {"rows": 0, "buckets": {}}
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
    for row_idx, raw in enumerate(rows[1:], start=2):
        row = raw + [""] * max(0, len(headers) - len(raw))
        if not str(row[idx.get(BET_COL["bet_id"], 0)] or "").strip():
            continue
        if str(row[idx[BET_COL["start_status"]]] or "").strip().upper() != "LEGACY_UNAUDITED":
            continue
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
    }
    return report


if __name__ == "__main__":
    print(json.dumps(run_audit(write=True), indent=2, sort_keys=True))
