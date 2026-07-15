"""Shared closing-line provenance and sample-selection policy.

The constants are deliberately environment-tunable: Phase 0 supplied safe
launch defaults, while the shadow monitor can tighten them without changing
the sheet contract or the selection algorithm.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


START_VERIFIED = "VERIFIED"
START_UNVERIFIED = "UNVERIFIED"
START_UNKNOWN = "UNKNOWN"
START_LEGACY = "LEGACY_UNAUDITED"

QUALITY_VERIFIED = "VERIFIED_CLOSE"
QUALITY_EARLY = "SAFE_BUT_EARLY"
QUALITY_STALE = "STALE"
QUALITY_PROVISIONAL = "PROVISIONAL"
QUALITY_MANUAL = "MANUAL"

SAFETY_MARGIN_SECONDS = max(1, int(os.getenv("CLOSING_SAFETY_MARGIN_SECONDS", "90")))
MAX_SAMPLE_AGE_SECONDS = max(1, int(os.getenv("CLOSING_MAX_SAMPLE_AGE_SECONDS", "300")))
BOOK_STALE_SECONDS = max(1, int(os.getenv("CLOSING_BOOK_STALE_SECONDS", "600")))
SAMPLE_CADENCE_SECONDS = max(10, int(os.getenv("CLOSING_SAMPLE_CADENCE_SECONDS", "90")))
SAMPLE_SLOTS = max(2, int(os.getenv("CLOSING_SAMPLE_SLOTS", "4")))
CAP_AFTER_COMMENCE_SECONDS = max(0, int(os.getenv("CLOSING_CAP_AFTER_COMMENCE_SECONDS", "2700")))

TRUSTED_FLIP_SPORTS = frozenset(
    item.strip() for item in os.getenv(
        "CLOSING_TRUSTED_FLIP_SPORTS",
        "baseball_mlb,basketball_wnba,soccer_usa_mls,soccer_fifa_world_cup",
    ).split(",") if item.strip()
)


def parse_utc(value) -> datetime | None:
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value or "").strip().replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


@dataclass(frozen=True)
class ClosingSample:
    price: str
    fetched_at: datetime
    book_last_update: datetime | None = None
    start_state: str = "UNKNOWN"
    slot: int | None = None


def quote_is_fresh(sample: ClosingSample, *, max_stale_seconds: int = BOOK_STALE_SECONDS) -> bool:
    if sample.book_last_update is None:
        return False
    age = (sample.fetched_at - sample.book_last_update).total_seconds()
    return -5 <= age <= max_stale_seconds


def select_pre_margin_sample(
    samples: list[ClosingSample],
    detected_at: datetime,
    *,
    margin_seconds: int = SAFETY_MARGIN_SECONDS,
) -> ClosingSample | None:
    """Newest VERIFIED_PREGAME sample safely before the detected-live instant."""
    cutoff = detected_at - timedelta(seconds=margin_seconds)
    eligible = [
        sample for sample in samples
        if sample.price and sample.start_state == "VERIFIED_PREGAME"
        and sample.fetched_at <= cutoff
    ]
    return max(eligible, key=lambda sample: sample.fetched_at, default=None)


def quality_for_detected_live(
    sample: ClosingSample | None,
    detected_at: datetime,
    *,
    max_age_seconds: int = MAX_SAMPLE_AGE_SECONDS,
    max_book_stale_seconds: int = BOOK_STALE_SECONDS,
) -> str | None:
    if sample is None:
        return None
    if (detected_at - sample.fetched_at).total_seconds() > max_age_seconds:
        return QUALITY_STALE
    if not quote_is_fresh(sample, max_stale_seconds=max_book_stale_seconds):
        return QUALITY_STALE
    return QUALITY_VERIFIED


def legacy_row_is_pooled(start_status: str, closing_quality: str, start_audit: str) -> bool:
    """Canonical Phase 1/4 aggregate-CLV inclusion contract."""
    status = str(start_status or "").strip().upper()
    quality = str(closing_quality or "").strip().upper()
    audit = str(start_audit or "").strip().upper()
    if quality == QUALITY_VERIFIED:
        return True
    if status != START_LEGACY:
        return False
    if not audit:
        return True
    return audit == "SAFE"
