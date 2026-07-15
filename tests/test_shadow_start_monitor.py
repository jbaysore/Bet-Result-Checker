"""Unit tests for the pure helpers in shadow_start_monitor (offline, no writes)."""

from datetime import datetime, timedelta, timezone

import shadow_start_monitor as sm
from sources.scores_live import COMPLETED, LIVE, PREGAME


COMMENCE = datetime(2026, 7, 11, 18, 0, tzinfo=timezone.utc)


# ── within_tracking_window ────────────────────────────────────────────────────
def test_tracking_window_bounds():
    assert sm.within_tracking_window(COMMENCE - timedelta(minutes=30), COMMENCE)
    assert not sm.within_tracking_window(COMMENCE - timedelta(minutes=31), COMMENCE)
    assert sm.within_tracking_window(COMMENCE + timedelta(minutes=45), COMMENCE)
    assert not sm.within_tracking_window(COMMENCE + timedelta(minutes=46), COMMENCE)


# ── classify_transition ───────────────────────────────────────────────────────
def test_classify_transition_cases():
    assert sm.classify_transition(PREGAME, LIVE) == "PREGAME_TO_LIVE"
    assert sm.classify_transition(PREGAME, COMPLETED) == "PREGAME_TO_COMPLETED"
    assert sm.classify_transition(None, LIVE) == "FIRST_SEEN_NOT_PREGAME"
    assert sm.classify_transition(None, PREGAME) is None
    assert sm.classify_transition(LIVE, LIVE) is None
    assert sm.classify_transition(LIVE, COMPLETED) is None


# ── detection_lag_seconds ─────────────────────────────────────────────────────
def test_detection_lag():
    detected = COMMENCE + timedelta(seconds=90)
    assert sm.detection_lag_seconds(detected, COMMENCE) == 90.0
    assert sm.detection_lag_seconds(detected, None) is None


# ── simulate_finalize ─────────────────────────────────────────────────────────
def test_simulate_verified_close_picks_latest_safe_sample():
    detected = COMMENCE
    polls = [detected - timedelta(seconds=s) for s in (200, 100, 30)]
    out = sm.simulate_finalize(polls, detected, margin_seconds=75, max_age_seconds=300)
    assert out["quality"] == sm.WOULD_VERIFIED_CLOSE
    assert out["sample_age_s"] == 100  # the -30s sample is inside the margin, so -100 wins


def test_simulate_stale_when_safe_sample_too_old():
    detected = COMMENCE
    polls = [detected - timedelta(seconds=s) for s in (200, 100)]
    out = sm.simulate_finalize(polls, detected, margin_seconds=75, max_age_seconds=60)
    assert out["quality"] == sm.WOULD_STALE


def test_simulate_missed_when_no_sample_older_than_margin():
    detected = COMMENCE
    polls = [detected - timedelta(seconds=30)]
    out = sm.simulate_finalize(polls, detected, margin_seconds=75)
    assert out["quality"] == sm.WOULD_MISSED
    assert out["newest_pregame_gap_s"] == 30


def test_simulate_missed_with_no_polls():
    out = sm.simulate_finalize([], COMMENCE, margin_seconds=75)
    assert out["quality"] == sm.WOULD_MISSED
    assert out["newest_pregame_gap_s"] is None


# ── mlb_candidate_dates ───────────────────────────────────────────────────────
def test_mlb_candidate_dates_tries_utc_day_and_prior():
    late = datetime(2026, 7, 11, 2, 0, tzinfo=timezone.utc)  # 10pm ET on the 10th
    assert sm.mlb_candidate_dates(late) == ["7/11/2026", "7/10/2026"]
