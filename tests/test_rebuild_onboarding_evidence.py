from datetime import datetime, timedelta, timezone

import onboarding_policy as policy
from capability_profile import CapabilityProfile, CapabilityRecord, record_key
from onboarding_verifier import Observation
from scripts.rebuild_onboarding_evidence import rebuild_profile

NOW = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)


def capability(context, capability, qualifier, health=policy.STALE):
    return CapabilityRecord(
        record_key=record_key(context, capability, qualifier), context_id=context,
        capability=capability, qualifier=qualifier, classification=policy.VERIFIED,
        health=health, evidence={"clean": 449, "neg": 433, "seen": {"old": "state"}},
    )


def test_rebuild_removes_inflated_counters_and_reconfirms_from_distinct_events():
    rec = capability("baseball/mlb", policy.CAP_CAPTURE, "prophetx|featured")
    profile = CapabilityProfile([rec])
    observations = [Observation(
        context_id="baseball/mlb", event_id=f"event-{i}",
        observed_at=NOW - timedelta(days=i % 3), capture_book="prophetx",
        capture_family="featured", capture_clean=True,
    ) for i in range(4)]
    result = rebuild_profile(profile, observations, now=NOW)
    rebuilt = profile.get_record(rec.record_key)
    assert rebuilt.evidence["clean"] == 4
    assert rebuilt.evidence["neg"] == 0
    assert "seen" not in rebuilt.evidence
    assert rebuilt.health == policy.FRESH
    assert rec.record_key in result["changed"]


def test_rebuild_keeps_a_majority_failure_grain_stale():
    rec = capability("baseball/mlb", policy.CAP_CAPTURE, "fanduel|h2h")
    profile = CapabilityProfile([rec])
    observations = [Observation(
        context_id="baseball/mlb", event_id=f"event-{i}", observed_at=NOW,
        capture_book="fanduel", capture_family="h2h", capture_clean=i < 3,
    ) for i in range(6)]
    rebuild_profile(profile, observations, now=NOW)
    assert profile.get_record(rec.record_key).health == policy.STALE


def test_rebuild_preserves_intentionally_superseded_bridge():
    rec = capability("baseball/mlb", policy.CAP_CAPTURE, "any|h2h")
    rec.notes = "2026-07-17 stale: grandfathered bridge superseded by exact book grain"
    profile = CapabilityProfile([rec])
    rebuild_profile(profile, [], now=NOW)
    assert profile.get_record(rec.record_key).health == policy.STALE
