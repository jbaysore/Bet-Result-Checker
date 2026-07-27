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


def test_three_negative_of_six_distinct_events_goes_stale():
    key = record_key("soccer/x", policy.CAP_IDENTITY, "toa")
    profile = CapabilityProfile([verified("soccer/x", policy.CAP_IDENTITY, "toa")])
    observations = [
        Observation(context_id="soccer/x", event_id=f"clean-{i}", observed_at=NOW,
                    identity_matched=True) for i in range(3)
    ] + [
        Observation(context_id="soccer/x", event_id=f"bad-{i}", observed_at=NOW,
                    identity_matched=False) for i in range(3)
    ]
    verifier.run_verification(profile, observations, apply=True, now=NOW)
    assert profile.get_record(key).health == policy.STALE


def test_three_negative_among_ten_distinct_events_stays_fresh():
    key = record_key("soccer/x", policy.CAP_IDENTITY, "toa")
    profile = CapabilityProfile([verified("soccer/x", policy.CAP_IDENTITY, "toa")])
    observations = [
        Observation(context_id="soccer/x", event_id=f"clean-{i}", observed_at=NOW,
                    identity_matched=True) for i in range(7)
    ] + [
        Observation(context_id="soccer/x", event_id=f"bad-{i}", observed_at=NOW,
                    identity_matched=False) for i in range(3)
    ]
    verifier.run_verification(profile, observations, apply=True, now=NOW)
    assert profile.get_record(key).health == policy.FRESH


def test_stale_record_reconfirms_after_three_clean_events_over_two_days():
    key = record_key("soccer/x", policy.CAP_IDENTITY, "toa")
    profile = CapabilityProfile([verified("soccer/x", policy.CAP_IDENTITY, "toa")])
    initial = [
        Observation(context_id="soccer/x", event_id=f"clean-{i}", observed_at=NOW,
                    identity_matched=True) for i in range(3)
    ] + [
        Observation(context_id="soccer/x", event_id=f"bad-{i}", observed_at=NOW,
                    identity_matched=False) for i in range(3)
    ]
    verifier.run_verification(profile, initial, apply=True, now=NOW)
    assert profile.get_record(key).health == policy.STALE

    recovery = [
        Observation(context_id="soccer/x", event_id="recovery-1",
                    observed_at=NOW + timedelta(days=1), identity_matched=True),
        Observation(context_id="soccer/x", event_id="recovery-2",
                    observed_at=NOW + timedelta(days=2), identity_matched=True),
        Observation(context_id="soccer/x", event_id="recovery-3",
                    observed_at=NOW + timedelta(days=2, hours=1), identity_matched=True),
    ]
    verifier.run_verification(profile, recovery, apply=True,
                              now=NOW + timedelta(days=2, hours=1))
    assert profile.get_record(key).health == policy.FRESH
    assert "re-confirmed by 3 clean events over 2 days" in profile.get_record(key).notes


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
