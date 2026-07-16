"""Demotion — never shadowed. Immediate Contradicted on severe evidence,
quarantine accumulation → Stale, decay, and causal-window row re-flagging."""

from datetime import datetime, timedelta, timezone

import onboarding_policy as policy
from capability_profile import CapabilityProfile, CapabilityRecord, record_key
import onboarding_verifier as verifier
from onboarding_verifier import Observation

NOW = datetime(2026, 7, 16, 22, 0, tzinfo=timezone.utc)


def verified(context_id, capability, qualifier):
    return CapabilityRecord(
        record_key=f"{context_id}|{capability}|{qualifier}", context_id=context_id,
        capability=capability, qualifier=qualifier, classification=policy.VERIFIED,
        health=policy.FRESH, evidence={"clean": 5})


def test_start_contradiction_immediately_contradicts():
    key = record_key("soccer/x", policy.CAP_START_LIVE, "toa_scores")
    profile = CapabilityProfile([verified("soccer/x", policy.CAP_START_LIVE, "toa_scores")])
    obs = Observation(context_id="soccer/x", start_source="toa_scores",
                      start_outcome=verifier.START_CONTRADICTION, observed_at=NOW)
    verifier.run_verification(profile, [obs], apply=True, now=NOW)
    assert profile.get_record(key).health == policy.CONTRADICTED
    assert profile.get_record(key).classification == policy.VERIFIED  # classification intact


def test_post_start_as_verified_is_immediate():
    key = record_key("soccer/x", policy.CAP_START_LIVE, "toa_scores")
    profile = CapabilityProfile([verified("soccer/x", policy.CAP_START_LIVE, "toa_scores")])
    verifier._apply_negative(profile, profile.get_record(key),
                             policy.FAILURE_POST_START_AS_VERIFIED, NOW)
    assert profile.get_record(key).health == policy.CONTRADICTED


def test_quarantine_three_in_fourteen_days_goes_stale():
    key = record_key("soccer/x", policy.CAP_IDENTITY, "toa")
    profile = CapabilityProfile([verified("soccer/x", policy.CAP_IDENTITY, "toa")])
    rec = profile.get_record(key)
    for day in (0, 5, 13):
        verifier._apply_negative(profile, rec, policy.FAILURE_AMBIGUOUS_MATCH,
                                 NOW - timedelta(days=13 - day))
    assert profile.get_record(key).health == policy.STALE


def test_two_quarantines_in_window_stay_fresh():
    key = record_key("soccer/x", policy.CAP_IDENTITY, "toa")
    profile = CapabilityProfile([verified("soccer/x", policy.CAP_IDENTITY, "toa")])
    rec = profile.get_record(key)
    # Applied in increasing time order (as production would): one aged out of the
    # 14-day window, then two recent → only 2 in-window at the last check.
    verifier._apply_negative(profile, rec, policy.FAILURE_AMBIGUOUS_MATCH, NOW - timedelta(days=40))
    verifier._apply_negative(profile, rec, policy.FAILURE_AMBIGUOUS_MATCH, NOW - timedelta(days=2))
    verifier._apply_negative(profile, rec, policy.FAILURE_AMBIGUOUS_MATCH, NOW - timedelta(days=1))
    assert profile.get_record(key).health == policy.FRESH


def test_missing_market_never_demotes():
    key = record_key("soccer/x", policy.CAP_CAPTURE, "fanduel|featured")
    rec = CapabilityRecord(record_key=key, context_id="soccer/x", capability=policy.CAP_CAPTURE,
                           qualifier="fanduel|featured", classification=policy.VERIFIED,
                           health=policy.FRESH, evidence={"clean": 5})
    profile = CapabilityProfile([rec])
    verifier._apply_negative(profile, profile.get_record(key),
                             policy.FAILURE_MISSING_MARKET, NOW)
    assert profile.get_record(key).health == policy.FRESH
    assert int(profile.get_record(key).evidence.get("neg", 0)) == 0


# ── Causal window (concept §8) ───────────────────────────────────────────────
def test_causal_window_reflags_only_in_window_rows():
    since = NOW - timedelta(hours=3)
    rows = [
        {"BetID": "1", "Closing Observed At": (NOW - timedelta(hours=1)).isoformat()},   # in window
        {"BetID": "2", "Closing Observed At": (NOW - timedelta(hours=10)).isoformat()},  # before
        {"BetID": "3", "Closing Observed At": ""},                                         # unknown → excluded
    ]
    flagged = verifier.rows_in_causal_window(rows, since)
    assert [r["BetID"] for r in flagged] == ["1"]
