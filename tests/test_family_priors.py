"""Family priors — a same-source prior lowers the evidence bar, never inherits
trust, and never transfers system correctness (concept §6)."""

import onboarding_policy as policy
from capability_profile import CapabilityProfile, CapabilityRecord
from family_priors import compute_prior


def rec(context_id, capability, qualifier, *, classification=policy.VERIFIED,
        health=policy.FRESH, irregular=0):
    return CapabilityRecord(
        record_key=f"{context_id}|{capability}|{qualifier}",
        context_id=context_id, capability=capability, qualifier=qualifier,
        classification=classification, health=health,
        evidence={"clean": 3, "irregular_ok": irregular})


def test_three_verified_siblings_with_irregular_give_strong_prior():
    profile = CapabilityProfile([
        rec("soccer/a", policy.CAP_START_LIVE, "toa_scores", irregular=1),
        rec("soccer/b", policy.CAP_START_LIVE, "toa_scores"),
        rec("soccer/c", policy.CAP_START_LIVE, "toa_scores"),
    ])
    prior = compute_prior(profile, "soccer/new", "toa_scores", policy.CAP_START_LIVE)
    assert prior == policy.STRONG_PRIOR


def test_too_few_siblings_no_prior():
    profile = CapabilityProfile([
        rec("soccer/a", policy.CAP_START_LIVE, "toa_scores", irregular=1),
        rec("soccer/b", policy.CAP_START_LIVE, "toa_scores"),
    ])
    assert compute_prior(profile, "soccer/new", "toa_scores", policy.CAP_START_LIVE) == policy.NO_PRIOR


def test_no_irregular_handled_no_prior():
    profile = CapabilityProfile([
        rec("soccer/a", policy.CAP_START_LIVE, "toa_scores"),
        rec("soccer/b", policy.CAP_START_LIVE, "toa_scores"),
        rec("soccer/c", policy.CAP_START_LIVE, "toa_scores"),
    ])
    assert compute_prior(profile, "soccer/new", "toa_scores", policy.CAP_START_LIVE) == policy.NO_PRIOR


def test_different_source_gives_no_prior():
    # Siblings verified on toa_scores lend nothing to an espn (authoritative) grain.
    profile = CapabilityProfile([
        rec("soccer/a", policy.CAP_START_LIVE, "toa_scores", irregular=1),
        rec("soccer/b", policy.CAP_START_LIVE, "toa_scores"),
        rec("soccer/c", policy.CAP_START_LIVE, "toa_scores"),
    ])
    assert compute_prior(profile, "soccer/new", "espn", policy.CAP_START_AUTHORITATIVE) == policy.NO_PRIOR


def test_combat_new_promotion_gets_no_prior():
    # Only UFC is verified in the mma family — a new promotion has too few
    # same-source siblings, so it earns its own way (the concept's UFC example).
    profile = CapabilityProfile([
        rec("mma/ufc", policy.CAP_START_AUTHORITATIVE, "espn_fights", irregular=1),
    ])
    assert compute_prior(profile, "mma/misfits", "espn_fights",
                         policy.CAP_START_AUTHORITATIVE) == policy.NO_PRIOR


def test_siblings_must_be_fresh_and_verified():
    profile = CapabilityProfile([
        rec("soccer/a", policy.CAP_START_LIVE, "toa_scores", irregular=1),
        rec("soccer/b", policy.CAP_START_LIVE, "toa_scores", health=policy.STALE),
        rec("soccer/c", policy.CAP_START_LIVE, "toa_scores", classification=policy.DISCOVERED),
    ])
    # Only soccer/a counts → 1 sibling → no prior.
    assert compute_prior(profile, "soccer/new", "toa_scores", policy.CAP_START_LIVE) == policy.NO_PRIOR
