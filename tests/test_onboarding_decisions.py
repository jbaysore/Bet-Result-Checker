"""Phase 6 decision rows really change checker-owned capability state."""

import pytest
from datetime import datetime, timezone

import onboarding_policy as policy
from capability_profile import CapabilityProfile, CapabilityRecord, record_key
import onboarding_decisions
from onboarding_decisions import apply_decision, apply_discovery


def rec(context, capability, qualifier, classification=policy.DISCOVERED):
    return CapabilityRecord(
        record_key=record_key(context, capability, qualifier), context_id=context,
        capability=capability, qualifier=qualifier, classification=classification,
        health=policy.NOT_EVALUATED,
    )


def test_context_wide_manual_and_reopen_are_applied_with_user_authority():
    profile = CapabilityProfile([
        rec("soccer/new", policy.CAP_IDENTITY, "toa"),
        rec("soccer/new", policy.CAP_CAPTURE, "draftkings|h2h"),
    ])
    assert apply_decision(profile, "soccer/new", {"action": "manual"}) == 2
    assert {r.classification for r in profile.records()} == {policy.MANUAL}
    assert apply_decision(profile, "soccer/new", {"action": "reopen"}) == 2
    assert {r.classification for r in profile.records()} == {policy.DISCOVERED}


def test_record_key_cannot_cross_context_boundary():
    profile = CapabilityProfile([rec("soccer/a", policy.CAP_IDENTITY, "toa")])
    with pytest.raises(ValueError, match="does not belong"):
        apply_decision(profile, "soccer/b", {
            "action": "retire", "recordKey": "soccer/a|identity|toa"})


def test_ephemeral_creates_only_retired_event_scoped_evidence(monkeypatch):
    profile = CapabilityProfile([
        rec("soccer/new", policy.CAP_IDENTITY, "toa"),
        rec("soccer/new", policy.CAP_START_LIVE, "toa_scores"),
        rec("soccer/new", policy.CAP_CAPTURE, "draftkings|h2h"),
    ])
    scoped = []
    monkeypatch.setattr(onboarding_decisions, "_scope_ephemeral_registry",
                        lambda *args: scoped.append(args))
    assert apply_decision(profile, "soccer/new", {
        "action": "ephemeral", "eventId": "evt-1", "sportKey": "soccer_new",
    }) == 6
    assert scoped and scoped[0][0] == "soccer/new"
    assert all(record.classification == policy.RETIRED for record in profile.records())
    event_records = [r for r in profile.records() if r.context_id != "soccer/new"]
    assert len(event_records) == 3
    assert all(r.constraints["ephemeral"] and r.constraints["event_id"] == "evt-1"
               for r in event_records)
    assert not any(r.classification == policy.VERIFIED for r in event_records)


def test_durable_scanner_discovery_starts_collecting_before_bet(monkeypatch):
    profile = CapabilityProfile([])
    monkeypatch.setattr(onboarding_decisions, "_ensure_registry_alias", lambda *_: True)
    row = {"Context ID": "soccer/new", "Sport Key": "soccer_new",
           "Book": "DraftKings", "Market Family": "h2h"}
    payload = {"intent": "durable", "sightings": 2,
               "firstSeenAt": "2026-07-15T12:00:00Z",
               "expiresAt": "2026-07-20T12:00:00Z"}
    assert apply_discovery(profile, row, payload,
                           now=datetime(2026, 7, 16, tzinfo=timezone.utc)) == 4
    assert {r.capability for r in profile.records()} == {
        policy.CAP_IDENTITY, policy.CAP_START_LIVE,
        policy.CAP_CAPTURE, policy.CAP_BENCHMARK,
    }
    assert all(r.activity == policy.COLLECTING for r in profile.records())
