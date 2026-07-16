"""Unit tests for capability_profile — the Capabilities-tab store.

Covers record round-trip, require() fail-closed semantics, the grandfathered
capture fallback (parity bridge), Limited bounds, and transition legality.
"""

import json

import pytest

import onboarding_policy as policy
from capability_profile import (
    CLV_START, CapabilityProfile, CapabilityRecord, IllegalTransition, Requirement,
    clv_requirements, record_key,
)


def seed_row(context_id, capability, qualifier, classification=policy.VERIFIED,
             health=policy.FRESH, constraints=""):
    return {
        "Record Key": record_key(context_id, capability, qualifier),
        "Context ID": context_id, "Capability": capability, "Qualifier": qualifier,
        "Classification": classification, "Health": health, "Activity": policy.IDLE,
        "Policy Version": "1", "Evidence Summary": "", "First Seen": "",
        "Last Verified": "", "Last Checked": "", "Constraints": constraints, "Notes": "",
    }


def clv_seed(context_id, book, family):
    """The three seeded records that make a bet trusted today."""
    return [
        seed_row(context_id, policy.CAP_IDENTITY, "toa"),
        seed_row(context_id, policy.CAP_START_LIVE, "toa_scores"),
        seed_row(context_id, policy.CAP_CAPTURE, f"any|{family}"),  # grandfathered book
    ]


# ── Round-trip ───────────────────────────────────────────────────────────────
def test_record_from_row_round_trip():
    row = seed_row("baseball/mlb", policy.CAP_CAPTURE, "draftkings|featured",
                   constraints=json.dumps({"book": "draftkings"}))
    rec = CapabilityRecord.from_row(row)
    assert rec.context_id == "baseball/mlb"
    assert rec.classification == policy.VERIFIED
    assert rec.constraints == {"book": "draftkings"}
    back = rec.to_row()
    assert back["Record Key"] == row["Record Key"]
    assert back["Classification"] == policy.VERIFIED


# ── require() ────────────────────────────────────────────────────────────────
def test_require_clv_trusted_when_all_seeded():
    profile = CapabilityProfile(clv_seed("baseball/mlb", "draftkings", "featured"))
    decision = profile.require_clv("baseball/mlb", "draftkings", "featured")
    assert decision.trusted
    assert not decision.provisional
    assert decision.matrix[policy.CAP_CAPTURE]["authorized"]


def test_require_clv_provisional_when_start_live_missing():
    rows = [r for r in clv_seed("soccer/new", "fanduel", "h2h")
            if policy.CAP_START_LIVE not in r["Capability"]]
    profile = CapabilityProfile(rows)
    decision = profile.require_clv("soccer/new", "fanduel", "h2h")
    assert decision.provisional
    assert CLV_START in decision.unresolved
    assert policy.CAP_IDENTITY not in decision.unresolved  # identity still verified


def test_require_specific_book_capture_wins_over_grandfathered():
    rows = clv_seed("baseball/mlb", "draftkings", "featured")
    rows.append(seed_row("baseball/mlb", policy.CAP_CAPTURE, "draftkings|featured"))
    profile = CapabilityProfile(rows)
    decision = profile.require_clv("baseball/mlb", "draftkings", "featured")
    assert decision.trusted
    assert decision.matrix[policy.CAP_CAPTURE]["record_key"].endswith("draftkings|featured")


def test_require_fails_closed_on_unreadable_tab():
    profile = CapabilityProfile(None)  # tab could not be read
    decision = profile.require_clv("baseball/mlb", "draftkings", "featured")
    assert decision.provisional
    assert not decision.trusted
    assert set(decision.unresolved) == {policy.CAP_IDENTITY, CLV_START, policy.CAP_CAPTURE}
    assert "fail closed" in decision.reason


def test_stale_health_does_not_authorize_trust():
    rows = clv_seed("baseball/mlb", "draftkings", "featured")
    for r in rows:
        if r["Capability"] == policy.CAP_START_LIVE:
            r["Health"] = policy.STALE
    profile = CapabilityProfile(rows)
    decision = profile.require_clv("baseball/mlb", "draftkings", "featured")
    assert decision.provisional
    assert CLV_START in decision.unresolved


# ── Limited bounds ───────────────────────────────────────────────────────────
def test_limited_authorizes_only_within_bounds():
    rec = CapabilityRecord.from_row(
        seed_row("soccer/x", policy.CAP_CAPTURE, "any|featured",
                 classification=policy.LIMITED, health=policy.FRESH))
    profile = CapabilityProfile([
        seed_row("soccer/x", policy.CAP_IDENTITY, "toa"),
        seed_row("soccer/x", policy.CAP_START_LIVE, "toa_scores"),
    ] + [rec.to_row()])

    inside = profile.require([Requirement(policy.CAP_CAPTURE,
                                          (record_key("soccer/x", policy.CAP_CAPTURE, "any|featured"),),
                                          within_bounds=True)])
    outside = profile.require([Requirement(policy.CAP_CAPTURE,
                                           (record_key("soccer/x", policy.CAP_CAPTURE, "any|featured"),),
                                           within_bounds=False)])
    assert inside.trusted
    assert outside.provisional


# ── transition() legality (concept §2) ───────────────────────────────────────
def test_transition_discovered_to_verified_sets_fresh():
    key = record_key("soccer/new", policy.CAP_START_LIVE, "toa_scores")
    profile = CapabilityProfile([
        seed_row("soccer/new", policy.CAP_START_LIVE, "toa_scores",
                 classification=policy.DISCOVERED, health=policy.NOT_EVALUATED),
    ])
    rec = profile.transition(key, policy.VERIFIED, "3 clean events on toa_scores")
    assert rec.classification == policy.VERIFIED
    assert rec.health == policy.FRESH
    assert rec.last_verified is not None


def test_illegal_transition_raises():
    key = record_key("soccer/new", policy.CAP_START_LIVE, "toa_scores")
    profile = CapabilityProfile([
        seed_row("soccer/new", policy.CAP_START_LIVE, "toa_scores"),  # Verified
    ])
    with pytest.raises(IllegalTransition):
        profile.transition(key, policy.DISCOVERED, "demote", authority=policy.AUTHORITY_AUTO)
    # Automation may not retire (user-only).
    with pytest.raises(IllegalTransition):
        profile.transition(key, policy.RETIRED, "auto retire", authority=policy.AUTHORITY_AUTO)


def test_transition_missing_record_creates_discovered():
    key = record_key("mma/new_promo", policy.CAP_START_LIVE, "toa_scores")
    profile = CapabilityProfile([])
    rec = profile.transition(key, policy.DISCOVERED, "first bet in a new promotion",
                             context_id="mma/new_promo", capability=policy.CAP_START_LIVE,
                             qualifier="toa_scores")
    assert rec.classification == policy.DISCOVERED
    assert profile.get_record(key) is not None
    # Creating a non-Discovered record from Unseen is illegal.
    with pytest.raises(IllegalTransition):
        CapabilityProfile([]).transition(key, policy.VERIFIED, "skip discovery")


def test_set_health_keeps_classification():
    key = record_key("baseball/mlb", policy.CAP_START_LIVE, "toa_scores")
    profile = CapabilityProfile([seed_row("baseball/mlb", policy.CAP_START_LIVE, "toa_scores")])
    rec = profile.set_health(key, policy.CONTRADICTED, "post-start capture observed")
    assert rec.classification == policy.VERIFIED  # unchanged
    assert rec.health == policy.CONTRADICTED


def test_clv_requirements_shape():
    reqs = clv_requirements("baseball/mlb", "DraftKings", "featured")
    caps = {r.capability for r in reqs}
    assert caps == {policy.CAP_IDENTITY, CLV_START, policy.CAP_CAPTURE}
    capture = next(r for r in reqs if r.capability == policy.CAP_CAPTURE)
    # specific book first, grandfathered fallback second
    assert capture.candidate_keys[0].endswith("draftkings|featured")
    assert capture.candidate_keys[1].endswith("any|featured")
    # start is a disjunction over live-flip and authoritative start
    start = next(r for r in reqs if r.capability == CLV_START)
    assert start.any_of == (("baseball/mlb", policy.CAP_START_LIVE),
                            ("baseball/mlb", policy.CAP_START_AUTHORITATIVE))
