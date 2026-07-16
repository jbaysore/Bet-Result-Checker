"""Tests for the concept operational-state report buckets (plan Phase 2).

Invariants: every row lands in exactly one bucket (the classifier is total),
and an upcoming event is never in a failure bucket.
"""

from datetime import datetime, timedelta, timezone

from scripts.clv_start_audit import (
    BUCKET_BY_DESIGN, BUCKET_CAPTURING, BUCKET_MANUAL, BUCKET_OBSERVING,
    BUCKET_PENDING, BUCKET_RECOVERABLE, BUCKET_REPAIRED, BUCKET_TRUSTED,
    BUCKET_UNBENCHMARKABLE, REPORT_FAILURE_BUCKETS, classify_report_bucket,
)

NOW = datetime(2026, 7, 16, 18, 0, tzinfo=timezone.utc)
FUTURE = NOW + timedelta(hours=3)
PAST = NOW - timedelta(hours=3)


def row(**kw):
    base = {"result": "", "closing_quality": "", "closing_source": "", "closing_odds": "",
            "pinnacle_close": "", "bet_type": "Moneyline", "live_bet": "", "notes": "",
            "commence_dt": PAST}
    base.update(kw)
    return base


def test_upcoming_event_is_pending_not_failure():
    bucket = classify_report_bucket(row(commence_dt=FUTURE, result=""), NOW)
    assert bucket == BUCKET_PENDING
    assert bucket not in REPORT_FAILURE_BUCKETS


def test_live_and_prop_and_void_are_by_design():
    assert classify_report_bucket(row(live_bet="TRUE"), NOW) == BUCKET_BY_DESIGN
    assert classify_report_bucket(row(bet_type="Prop"), NOW) == BUCKET_BY_DESIGN
    assert classify_report_bucket(row(result="VOID"), NOW) == BUCKET_BY_DESIGN


def test_verified_close_trusted_vs_unbenchmarkable():
    assert classify_report_bucket(
        row(closing_quality="VERIFIED_CLOSE", closing_odds="-110", pinnacle_close="-105"),
        NOW) == BUCKET_TRUSTED
    assert classify_report_bucket(
        row(closing_quality="VERIFIED_CLOSE", closing_odds="-110", pinnacle_close=""),
        NOW) == BUCKET_UNBENCHMARKABLE


def test_onboarding_marker_provisional_is_observing():
    assert classify_report_bucket(
        row(closing_quality="PROVISIONAL", closing_odds="-110",
            notes="onboarding: soccer/x|h2h start=Discovered"),
        NOW) == BUCKET_OBSERVING


def test_repaired_and_manual_and_recoverable():
    assert classify_report_bucket(
        row(closing_source="recovery-onboarding", closing_quality="VERIFIED_CLOSE",
            closing_odds="-110", pinnacle_close="-105"),
        NOW) == BUCKET_REPAIRED
    assert classify_report_bucket(row(closing_odds="MANUAL ENTRY"), NOW) == BUCKET_MANUAL
    assert classify_report_bucket(row(closing_odds="GAME NOT FOUND"), NOW) == BUCKET_RECOVERABLE


def test_capturing_when_no_close_yet():
    assert classify_report_bucket(row(closing_odds="", closing_quality=""), NOW) == BUCKET_CAPTURING


def test_classifier_is_total_over_a_grid():
    # Every combination lands somewhere — the function never returns None/raises.
    seen = set()
    for q in ("", "VERIFIED_CLOSE", "PROVISIONAL", "STALE", "MANUAL", "SAFE_BUT_EARLY"):
        for odds in ("", "-110", "GAME NOT FOUND", "MANUAL ENTRY"):
            for res in ("", "WIN", "VOID"):
                b = classify_report_bucket(
                    row(closing_quality=q, closing_odds=odds, result=res), NOW)
                assert isinstance(b, str) and b
                seen.add(b)
    assert BUCKET_TRUSTED in seen or BUCKET_UNBENCHMARKABLE in seen
