"""
Track repeated identical closing-odds failure codes so the cron checker
stops burning API credits on hopeless rows.

Markers live in the Notes column:
  [closing-odds-fail:N:SELECTION NOT FOUND]  — N identical failures in a row
  [closing-odds-exhausted]                   — skip automatic re-scan
"""

import re

from config import CLOSING_ODDS_EXHAUSTION_THRESHOLD

_FAIL_STREAK_RE = re.compile(
    r"\[closing-odds-fail:(\d+):([^\]]+)\]",
    re.IGNORECASE,
)
_EXHAUSTED_MARKER = "[closing-odds-exhausted]"


def is_closing_odds_exhausted(notes: str) -> bool:
    return _EXHAUSTED_MARKER in (notes or "")


def parse_fail_streak(notes: str) -> tuple[int, str]:
    """Return (count, error_code) from Notes, or (0, '')."""
    m = _FAIL_STREAK_RE.search(notes or "")
    if not m:
        return 0, ""
    return int(m.group(1)), m.group(2).strip()


def _strip_fail_markers(notes: str) -> str:
    text = _FAIL_STREAK_RE.sub("", notes or "")
    text = text.replace(_EXHAUSTED_MARKER, "")
    lines = [line for line in text.split("\n") if line.strip()]
    return "\n".join(lines)


def next_fail_streak_notes(
    notes: str,
    error_code: str,
    *,
    threshold: int = CLOSING_ODDS_EXHAUSTION_THRESHOLD,
) -> tuple[str, bool]:
    """
    Increment the fail streak for error_code. When count reaches threshold,
    append the exhausted marker.

    Returns (new_notes, became_exhausted).
    """
    base = _strip_fail_markers(notes)
    prev_count, prev_code = parse_fail_streak(notes or "")
    if prev_code == error_code:
        count = prev_count + 1
    else:
        count = 1

    streak_line = f"[closing-odds-fail:{count}:{error_code}]"
    became_exhausted = count >= threshold
    parts = [base, streak_line]
    if became_exhausted:
        parts.append(_EXHAUSTED_MARKER)
    new_notes = "\n".join(p for p in parts if p)
    return new_notes, became_exhausted


def clear_fail_streak_notes(notes: str) -> str:
    """Remove fail-streak and exhausted markers (e.g. after a successful capture)."""
    return _strip_fail_markers(notes or "")
