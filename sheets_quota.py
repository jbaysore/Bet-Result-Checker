"""
Retry wrapper for Google Sheets API quota / rate-limit errors.

The free Sheets quota is ~60 read requests per minute per user. A full
Bet Result Checker run can burn most of that writing results (each write
re-reads the row first). The Promotion Updater then starts as a *separate*
process (fresh caches) and used to crash immediately on 429.

Callers should wrap individual network reads/writes with
`call_with_sheets_retry(...)` so a short wait recovers the minute window
instead of failing the whole GitHub Actions job.
"""

from __future__ import annotations

import time
from typing import Callable, TypeVar

import gspread

T = TypeVar("T")

# Stay under the per-minute ceiling: wait long enough for the window to
# reset, then retry a few times before giving up.
_DEFAULT_MAX_ATTEMPTS = 4
_DEFAULT_BASE_SLEEP_SEC = 35


def _is_quota_error(exc: BaseException) -> bool:
    if not isinstance(exc, gspread.exceptions.APIError):
        return False
    text = str(exc).lower()
    return (
        "429" in text
        or "quota exceeded" in text
        or "rate limit" in text
        or "resource_exhausted" in text
    )


def call_with_sheets_retry(
    label: str,
    fn: Callable[..., T],
    *args,
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    base_sleep_sec: float = _DEFAULT_BASE_SLEEP_SEC,
    **kwargs,
) -> T:
    """
    Invoke fn(*args, **kwargs), retrying on Sheets 429 / quota errors.
    Non-quota errors propagate immediately.
    """
    attempt = 1
    while True:
        try:
            return fn(*args, **kwargs)
        except gspread.exceptions.APIError as e:
            if not _is_quota_error(e) or attempt >= max_attempts:
                raise
            sleep_sec = base_sleep_sec * attempt
            print(
                f"[sheets_quota] ⏳ {label}: Sheets quota/rate limit "
                f"(attempt {attempt}/{max_attempts}). "
                f"Sleeping {sleep_sec:.0f}s then retrying…"
            )
            time.sleep(sleep_sec)
            attempt += 1
