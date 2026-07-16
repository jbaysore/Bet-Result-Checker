"""Phase 6 decision rows really change checker-owned capability state."""

import pytest

import onboarding_policy as policy
from capability_profile import CapabilityProfile, CapabilityRecord, record_key
from onboarding_decisions import apply_decision


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


def test_ephemeral_is_rejected_until_phase_7_instead_of_fake_success():
    profile = CapabilityProfile([rec("soccer/new", policy.CAP_IDENTITY, "toa")])
    with pytest.raises(ValueError, match="Phase 7"):
        apply_decision(profile, "soccer/new", {"action": "ephemeral"})
