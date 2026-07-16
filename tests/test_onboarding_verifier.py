"""Post-event verifier — §5 outcomes, evidence accumulation, and promotion."""

from datetime import datetime, timedelta, timezone

import onboarding_policy as policy
from capability_profile import CapabilityProfile, CapabilityRecord, record_key
import onboarding_verifier as verifier
from onboarding_verifier import Observation

NOW = datetime(2026, 7, 16, 22, 0, tzinfo=timezone.utc)


def verified(context_id, capability, qualifier, irregular=0):
    return CapabilityRecord(
        record_key=f"{context_id}|{capability}|{qualifier}", context_id=context_id,
        capability=capability, qualifier=qualifier, classification=policy.VERIFIED,
        health=policy.FRESH, evidence={"clean": 3, "irregular_ok": irregular})


def strong_prior_profile(family="soccer"):
    """Three verified same-source siblings so a new sibling has a strong prior."""
    return [
        verified(f"{family}/a", policy.CAP_START_LIVE, "toa_scores", irregular=1),
        verified(f"{family}/b", policy.CAP_START_LIVE, "toa_scores"),
        verified(f"{family}/c", policy.CAP_START_LIVE, "toa_scores"),
    ]


# ── classify_start (§5) ──────────────────────────────────────────────────────
def test_classify_start_outcomes():
    auth = NOW
    assert verifier.classify_start(NOW + timedelta(seconds=30), auth) == verifier.START_AGREEMENT
    assert verifier.classify_start(None, auth) == verifier.START_RECOVERABLE
    assert verifier.classify_start(NOW, None) == verifier.START_UNRESOLVED
    assert verifier.classify_start(None, None) == verifier.START_MISSING
    assert verifier.classify_start(NOW + timedelta(minutes=15), auth) == verifier.START_CONTRADICTION


# ── Promotion ────────────────────────────────────────────────────────────────
def test_strong_prior_promotes_after_one_clean_event():
    profile = CapabilityProfile(strong_prior_profile())
    obs = Observation(context_id="soccer/new", start_source="toa_scores",
                      start_outcome=verifier.START_AGREEMENT, observed_at=NOW,
                      capture_book="draftkings", capture_family="h2h", capture_clean=True)
    proposals = verifier.run_verification(profile, [obs], apply=True, now=NOW)
    promoted = {p.record_key for p in proposals if p.kind == "promote"}
    start_key = record_key("soccer/new", policy.CAP_START_LIVE, "toa_scores")
    assert start_key in promoted
    assert profile.get_record(start_key).classification == policy.VERIFIED


def test_new_source_needs_three_events_two_days():
    # No sibling prior → one clean event is not enough.
    profile = CapabilityProfile([])
    day1 = Observation(context_id="cricket/ipl", start_source="toa_scores",
                       start_outcome=verifier.START_AGREEMENT, observed_at=NOW,
                       capture_family="h2h", capture_clean=True)
    verifier.run_verification(profile, [day1], apply=True, now=NOW)
    start_key = record_key("cricket/ipl", policy.CAP_START_LIVE, "toa_scores")
    assert profile.get_record(start_key).classification == policy.DISCOVERED

    # Two more clean events across two additional days → meets the new-source bar.
    day2 = Observation(context_id="cricket/ipl", start_source="toa_scores",
                       start_outcome=verifier.START_AGREEMENT, observed_at=NOW + timedelta(days=1),
                       capture_family="h2h", capture_clean=True)
    day3 = Observation(context_id="cricket/ipl", start_source="toa_scores",
                       start_outcome=verifier.START_AGREEMENT, observed_at=NOW + timedelta(days=2),
                       capture_family="h2h", capture_clean=True)
    verifier.run_verification(profile, [day2, day3], apply=True, now=NOW + timedelta(days=2))
    assert profile.get_record(start_key).classification == policy.VERIFIED


def test_shadow_mode_proposes_without_applying():
    profile = CapabilityProfile(strong_prior_profile())
    obs = Observation(context_id="soccer/new", start_source="toa_scores",
                      start_outcome=verifier.START_AGREEMENT, observed_at=NOW,
                      capture_family="h2h", capture_clean=True)
    proposals = verifier.run_verification(profile, [obs], apply=False, now=NOW)
    assert any(p.kind == "promote" and not p.applied for p in proposals)
    start = profile.get_record(record_key("soccer/new", policy.CAP_START_LIVE, "toa_scores"))
    assert start.classification == policy.DISCOVERED           # not applied
    assert "proposed: Discovered→Verified" in start.notes      # recorded


# ── Mid-event first observation (concept §4) ─────────────────────────────────
def test_mid_event_first_observation_verifies_via_authoritative_start():
    # No pregame live detection, but a post-hoc authoritative start makes the
    # close recoverable → still clean system-correctness evidence.
    profile = CapabilityProfile([
        verified("mma/a", policy.CAP_START_AUTHORITATIVE, "espn_fights", irregular=1),
        verified("mma/b", policy.CAP_START_AUTHORITATIVE, "espn_fights"),
        verified("mma/c", policy.CAP_START_AUTHORITATIVE, "espn_fights"),
    ])
    obs = Observation(context_id="mma/new", start_source="espn_fights",
                      start_outcome=verifier.START_RECOVERABLE, observed_at=NOW,
                      capture_family="h2h", capture_clean=True)
    verifier.run_verification(profile, [obs], apply=True, now=NOW)
    key = record_key("mma/new", policy.CAP_START_AUTHORITATIVE, "espn_fights")
    assert profile.get_record(key).classification == policy.VERIFIED


# ── Attribution: missing market ≠ start failure (concept explicit example) ───
def test_missing_market_does_not_count_against_start_or_identity():
    profile = CapabilityProfile(strong_prior_profile())
    obs = Observation(context_id="soccer/new", start_source="toa_scores",
                      start_outcome=verifier.START_AGREEMENT, observed_at=NOW,
                      capture_book="fanduel", capture_family="featured",
                      capture_clean=False, missing_market=True)
    verifier.run_verification(profile, [obs], apply=True, now=NOW)
    start = profile.get_record(record_key("soccer/new", policy.CAP_START_LIVE, "toa_scores"))
    capture = profile.get_record(record_key("soccer/new", policy.CAP_CAPTURE, "fanduel|featured"))
    assert int(start.evidence.get("neg", 0)) == 0             # start unharmed
    assert int(capture.evidence.get("neg", 0)) == 0           # coverage, not a failure
    assert int(capture.evidence.get("clean", 0)) == 0         # but no positive evidence either


def test_no_start_source_blocks():
    profile = CapabilityProfile([])
    obs = Observation(context_id="darts/pdc", start_source="toa_scores",
                      start_outcome=verifier.START_MISSING, observed_at=NOW,
                      capture_family="h2h", capture_clean=False)
    proposals = verifier.run_verification(profile, [obs], apply=True, now=NOW)
    assert any(p.kind == "block" for p in proposals)
    key = record_key("darts/pdc", policy.CAP_START_LIVE, "toa_scores")
    assert profile.get_record(key).classification == policy.BLOCKED
