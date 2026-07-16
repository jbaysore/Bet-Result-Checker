"""Unit tests for onboarding_policy — the frozen Phase 0 policy (plan §P0.2).

Pure arithmetic and classification; no I/O, no sheet. A frozen clock (NOW) makes
the freshness/quarantine aging deterministic.
"""

from datetime import datetime, timedelta, timezone

import onboarding_policy as policy


NOW = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)


# ── Market family / class collapse ───────────────────────────────────────────
def test_market_family_for_market_keys():
    assert policy.market_family_for("h2h") == policy.MF_H2H
    assert policy.market_family_for("spreads") == policy.MF_FEATURED
    assert policy.market_family_for("alternate_totals") == policy.MF_FEATURED
    assert policy.market_family_for("player_points") == policy.MF_PROP
    assert policy.market_family_for("batter_home_runs") == policy.MF_PROP
    assert policy.market_family_for("outrights") == policy.MF_OUTRIGHT
    assert policy.market_family_for("totals_team_total") == policy.MF_TEAM_TOTAL


def test_market_family_falls_back_to_bet_type_then_unknown():
    assert policy.market_family_for(None, "Spread") == policy.MF_FEATURED
    assert policy.market_family_for(None, "Moneyline") == policy.MF_H2H
    assert policy.market_family_for(None, "Prop") == policy.MF_PROP
    assert policy.market_family_for("", "") == policy.MF_UNKNOWN  # seedless, fail closed
    assert policy.market_family_for("brand_new_market", "Custom") == policy.MF_UNKNOWN
    assert policy.market_family_for("brand_new_market", "Moneyline") == policy.MF_UNKNOWN


def test_market_class_for_settlement_grain():
    assert policy.market_class_for("Moneyline") == "moneyline"
    assert policy.market_class_for("Draw") == "moneyline"
    assert policy.market_class_for("Spread") == "spread"
    assert policy.market_class_for("Total") == "total"
    assert policy.market_class_for("Prop") == "prop"


# ── Family prior + evidence bar (§P0.2 #1) ───────────────────────────────────
def test_family_prior_strong_requires_siblings_and_irregular():
    assert policy.family_prior_strength(3, 1) == policy.STRONG_PRIOR
    assert policy.family_prior_strength(5, 2) == policy.STRONG_PRIOR
    assert policy.family_prior_strength(2, 1) == policy.NO_PRIOR   # too few siblings
    assert policy.family_prior_strength(3, 0) == policy.NO_PRIOR   # no irregular handled


def test_evidence_bar_strong_prior_promotes_on_one_clean_event():
    assert policy.meets_evidence_bar(clean_events=1, distinct_days=1, prior=policy.STRONG_PRIOR)


def test_evidence_bar_new_source_needs_three_events_two_days():
    # One event is enough for system correctness but not the source-correctness bar.
    assert not policy.meets_evidence_bar(clean_events=1, distinct_days=1, prior=policy.NO_PRIOR)
    assert not policy.meets_evidence_bar(clean_events=3, distinct_days=1, prior=policy.NO_PRIOR)
    assert policy.meets_evidence_bar(clean_events=3, distinct_days=2, prior=policy.NO_PRIOR)


def test_evidence_bar_blocked_by_contradiction_and_zero_events():
    assert not policy.meets_evidence_bar(clean_events=5, distinct_days=3,
                                         prior=policy.STRONG_PRIOR, has_contradiction=True)
    assert not policy.meets_evidence_bar(clean_events=0, distinct_days=0, prior=policy.STRONG_PRIOR)


# ── Negative evidence: severity + attribution (§P0.2 #7) ─────────────────────
def test_severity_immediate_for_start_and_post_start():
    assert policy.severity_for(policy.FAILURE_POST_START_AS_VERIFIED) == policy.SEV_IMMEDIATE
    assert policy.severity_for(policy.FAILURE_START_CONTRADICTION) == policy.SEV_IMMEDIATE


def test_severity_quarantine_for_ambiguous_and_transient():
    assert policy.severity_for(policy.FAILURE_AMBIGUOUS_MATCH) == policy.SEV_QUARANTINE
    assert policy.severity_for(policy.FAILURE_TRANSIENT_EMPTY) == policy.SEV_QUARANTINE


def test_missing_market_is_coverage_not_a_start_failure():
    # The concept's explicit example: a book not offering a market is a coverage
    # limitation on capture, never evidence against identity or actual-start.
    assert policy.severity_for(policy.FAILURE_MISSING_MARKET) == policy.SEV_COVERAGE
    assert policy.attribute_failure(policy.FAILURE_MISSING_MARKET) == policy.CAP_CAPTURE
    assert policy.attribute_failure(policy.FAILURE_START_CONTRADICTION) == policy.CAP_START_LIVE
    assert policy.attribute_failure(policy.FAILURE_IDENTITY_MISMATCH) == policy.CAP_IDENTITY


def test_start_disagreement_threshold():
    assert not policy.start_disagreement_is_contradiction(9 * 60)
    assert policy.start_disagreement_is_contradiction(11 * 60)
    assert policy.start_disagreement_is_contradiction(-11 * 60)  # magnitude, either direction


def test_quarantine_threshold_and_decay():
    within = [NOW - timedelta(days=d) for d in (1, 5, 13)]
    assert policy.quarantine_crosses_threshold(within, NOW)          # 3 within 14 days
    spread_out = [NOW - timedelta(days=d) for d in (1, 5, 20)]
    assert not policy.quarantine_crosses_threshold(spread_out, NOW)  # only 2 in window
    assert policy.quarantine_decayed(NOW - timedelta(days=31), NOW)
    assert not policy.quarantine_decayed(NOW - timedelta(days=10), NOW)
    assert policy.quarantine_decayed(None, NOW)                      # never quarantined


# ── Freshness aging (§P0.2 #6) ───────────────────────────────────────────────
def test_idle_stale_after_window():
    assert not policy.is_idle_stale(NOW - timedelta(days=119), NOW)
    assert policy.is_idle_stale(NOW - timedelta(days=121), NOW)
    assert not policy.is_idle_stale(None, NOW)  # never verified is NotEvaluated, not Stale


def test_policy_bump_makes_older_records_stale():
    assert policy.freshness_after_policy_bump(policy.POLICY_VERSION) == policy.FRESH
    assert policy.freshness_after_policy_bump(policy.POLICY_VERSION - 1) == policy.STALE


# ── Classification transition legality (concept §2) ──────────────────────────
def test_legal_auto_promotions():
    assert policy.is_legal_classification_transition(
        policy.DISCOVERED, policy.VERIFIED, policy.AUTHORITY_AUTO)
    assert policy.is_legal_classification_transition(
        policy.DISCOVERED, policy.LIMITED, policy.AUTHORITY_AUTO)
    assert policy.is_legal_classification_transition(
        policy.BLOCKED, policy.DISCOVERED, policy.AUTHORITY_AUTO)


def test_illegal_and_user_only_transitions():
    # Verified never demotes to Discovered by classification (health handles it).
    assert not policy.is_legal_classification_transition(
        policy.VERIFIED, policy.DISCOVERED, policy.AUTHORITY_AUTO)
    # Retirement and Manual are user decisions — automation may not perform them.
    assert not policy.is_legal_classification_transition(
        policy.VERIFIED, policy.RETIRED, policy.AUTHORITY_AUTO)
    assert not policy.is_legal_classification_transition(
        policy.DISCOVERED, policy.MANUAL, policy.AUTHORITY_AUTO)
    assert policy.is_legal_classification_transition(
        policy.VERIFIED, policy.MANUAL, policy.AUTHORITY_USER)
    assert policy.is_legal_classification_transition(
        policy.RETIRED, policy.DISCOVERED, policy.AUTHORITY_USER)


def test_same_classification_is_legal_noop():
    assert policy.is_legal_classification_transition(
        policy.VERIFIED, policy.VERIFIED, policy.AUTHORITY_AUTO)


# ── Trust authorization (concept §2) ─────────────────────────────────────────
def test_authorizes_trust_only_verified_or_bounded_limited_and_fresh():
    assert policy.authorizes_trust(policy.VERIFIED, policy.FRESH)
    assert not policy.authorizes_trust(policy.VERIFIED, policy.STALE)
    assert not policy.authorizes_trust(policy.VERIFIED, policy.CONTRADICTED)
    assert not policy.authorizes_trust(policy.DISCOVERED, policy.FRESH)
    assert not policy.authorizes_trust(policy.BLOCKED, policy.FRESH)
    # Limited authorizes only inside its constraints.
    assert policy.authorizes_trust(policy.LIMITED, policy.FRESH, within_limited_bounds=True)
    assert not policy.authorizes_trust(policy.LIMITED, policy.FRESH, within_limited_bounds=False)


# ── Scanner intent key (§P0.2 #12) ───────────────────────────────────────────
def test_scanner_dedup_key():
    assert policy.scanner_dedup_key("baseball/mlb", "draftkings", policy.MF_FEATURED) \
        == "baseball/mlb|draftkings|featured"
