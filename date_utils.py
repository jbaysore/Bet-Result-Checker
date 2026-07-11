"""
Shared sheet-date parsing. The sheet stores game dates as EITHER "M/D/YYYY" OR
"YYYY-MM-DD" (odds-tool's Log Bet Wizard / backfill write both). Trap #7: one
parser, used by the poller's datetime builder AND every result source that needs
to scope a fetch by date (espn_fights' scoreboard window, mlb_statsapi's
schedule-by-date) — never a fresh per-call-site regex.
"""

from datetime import date


def parse_sheet_date(raw: str | None) -> date | None:
    """
    "7/10/2026" (M/D/YYYY) or "2026-07-10" (YYYY-MM-DD) → date. None on empty or
    unrecognized input (callers treat None as "no date scope", never a crash).
    """
    s = (raw or "").strip()
    if not s:
        return None
    try:
        if "-" in s:
            year, month, day = map(int, s.split("-"))          # ISO YYYY-MM-DD
        elif "/" in s:
            month, day, year = map(int, s.split("/"))          # US M/D/YYYY
        else:
            return None
        return date(year, month, day)
    except (ValueError, TypeError):
        return None
