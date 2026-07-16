"""Post-event verification, promotion, and demotion (concept §5/§7/§8, plan P4).

After an event completes the verifier turns what actually happened into evidence
against the exact capability grains a bet exercised, then decides classification
moves:

  - promote Discovered → Verified when the evidence bar is met (system
    correctness proven by ≥1 clean exact match, source correctness supported by a
    strong same-source family prior or enough events);
  - block Discovered → Blocked when the investigation reaches a stable dead end
    (no authoritative start source / no viable automatic path);
  - demote Verified/Limited health → Contradicted or Stale on attributed negative
    evidence (a routine capture doubles as a cheap re-validation).

Promotions honor ONBOARDING_PROMOTE_SHADOW (propose-only). **Demotions never
shadow** — a downgrade fails closed immediately (concept §8). Everything here is
pure over an in-memory CapabilityProfile + Observations so it is fully testable;
the live wrapper reads the tabs and flags.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import onboarding_policy as policy
from capability_profile import CapabilityProfile, CapabilityRecord, record_key
from family_priors import compute_prior

# ── Start outcomes (concept §5) ──────────────────────────────────────────────
START_AGREEMENT = "agreement"        # live detection and authoritative start agree
START_RECOVERABLE = "recoverable"    # authoritative start known, safe close selectable
START_UNRESOLVED = "unresolved"      # no authoritative start / ambiguous event
START_CONTRADICTION = "contradiction"  # sources disagree beyond tolerance
START_MISSING = "missing"            # no start signal AND no authoritative source


@dataclass
class Observation:
    """The facts of one completed event as they bear on a context's grains."""
    context_id: str
    event_id: str = ""
    observed_at: datetime | None = None
    identity_matched: bool = True        # exactly one unambiguous event matched
    start_source: str = "toa_scores"     # qualifier of the start capability exercised
    start_outcome: str = START_UNRESOLVED
    capture_book: str = ""
    capture_family: str = ""
    capture_clean: bool = False          # a fresh pre-start close existed
    irregular: bool = False              # event went sideways and was handled correctly
    missing_market: bool = False         # book did not offer the market (coverage, not a failure)


@dataclass
class Proposal:
    record_key: str
    from_classification: str
    to_classification: str
    reason: str
    kind: str                 # "promote" | "block"
    applied: bool = False     # True if enforced, False if shadow-proposed


# ── Start classification (concept §5) ────────────────────────────────────────
def classify_start(detected_at: datetime | None, authoritative_start: datetime | None) -> str:
    if authoritative_start is None:
        # No authoritative fact to confirm against.
        return START_UNRESOLVED if detected_at is not None else START_MISSING
    if detected_at is None:
        # Authoritative start known but no live detection → a safe close is still
        # recoverable (the mid-event / post-hoc case, concept §4).
        return START_RECOVERABLE
    delta = (detected_at - authoritative_start).total_seconds()
    if policy.start_disagreement_is_contradiction(delta):
        return START_CONTRADICTION
    return START_AGREEMENT


def start_grain(source: str) -> tuple[str, str]:
    """(capability, qualifier) for the start source actually exercised."""
    if source == "toa_scores":
        return policy.CAP_START_LIVE, "toa_scores"
    return policy.CAP_START_AUTHORITATIVE, source


# ── Evidence accumulation ────────────────────────────────────────────────────
def _bump(record: CapabilityRecord, key: str, n: int = 1) -> None:
    record.evidence[key] = int(record.evidence.get(key, 0) or 0) + n


def _add_day(record: CapabilityRecord, key: str, day: datetime | None) -> None:
    if day is None:
        return
    days = record.evidence.setdefault(key, [])
    iso = day.date().isoformat()
    if iso not in days:
        days.append(iso)


def _ensure(profile: CapabilityProfile, context_id: str, capability: str,
            qualifier: str, now: datetime) -> CapabilityRecord:
    key = record_key(context_id, capability, qualifier)
    rec = profile.get_record(key)
    if rec is None:
        rec = profile.transition(key, policy.DISCOVERED, "discovered by verifier",
                                 context_id=context_id, capability=capability, qualifier=qualifier)
    rec.last_checked = now
    return rec


def accumulate(profile: CapabilityProfile, obs: Observation, *, now: datetime) -> list[CapabilityRecord]:
    """Fold one observation into the evidence counters of its grain records,
    creating Discovered records as needed. Returns the records touched.

    Demotion of an already-Verified record is applied here and now (never
    shadowed); promotion is decided later by `run_verification`.
    """
    touched: list[CapabilityRecord] = []

    # identity
    ident = _ensure(profile, obs.context_id, policy.CAP_IDENTITY, "toa", now)
    if obs.identity_matched:
        _bump(ident, "clean"); _add_day(ident, "days", obs.observed_at)
        if obs.irregular:
            _bump(ident, "irregular_ok")
    else:
        _apply_negative(profile, ident, policy.FAILURE_IDENTITY_MISMATCH, now)
    touched.append(ident)

    # start
    cap, qual = start_grain(obs.start_source or "toa_scores")
    start_rec = _ensure(profile, obs.context_id, cap, qual, now)
    if obs.start_outcome in (START_AGREEMENT, START_RECOVERABLE):
        _bump(start_rec, "clean"); _add_day(start_rec, "days", obs.observed_at)
        if obs.irregular:
            _bump(start_rec, "irregular_ok")
    elif obs.start_outcome == START_CONTRADICTION:
        _apply_negative(profile, start_rec, policy.FAILURE_START_CONTRADICTION, now)
    elif obs.start_outcome == START_UNRESOLVED:
        _apply_negative(profile, start_rec, policy.FAILURE_TRANSIENT_EMPTY, now)
    elif obs.start_outcome == START_MISSING:
        _bump(start_rec, "no_source")
    touched.append(start_rec)

    # capture
    if obs.capture_family:
        book = (obs.capture_book or "any").strip().lower() or "any"
        crec = _ensure(profile, obs.context_id, policy.CAP_CAPTURE,
                       f"{book}|{obs.capture_family}", now)
        if obs.missing_market:
            pass  # coverage limitation — no evidence for or against (concept §5)
        elif obs.capture_clean:
            _bump(crec, "clean"); _add_day(crec, "days", obs.observed_at)
        else:
            _apply_negative(profile, crec, policy.FAILURE_STALE_QUOTE, now)
        touched.append(crec)

    for rec in touched:
        profile._store(rec)  # persist counters (sink write in live mode; in-memory in tests)
    return touched


# ── Demotion (never shadowed) ────────────────────────────────────────────────
def _apply_negative(profile: CapabilityProfile, record: CapabilityRecord,
                    failure_kind: str, now: datetime) -> None:
    """Attribute a negative observation to `record` and demote if warranted."""
    severity = policy.severity_for(failure_kind)
    if severity == policy.SEV_COVERAGE:
        return
    _bump(record, "neg")
    if severity == policy.SEV_IMMEDIATE:
        if record.classification in (policy.VERIFIED, policy.LIMITED):
            profile.set_health(record.record_key, policy.CONTRADICTED,
                               f"contradicted: {failure_kind}")
        else:
            record.health = policy.CONTRADICTED  # Discovered stays Discovered, health noted
        return
    # quarantine — count EVENTS (not distinct days) inside the trailing window,
    # so repeated same-day ambiguities accumulate toward the threshold.
    _bump(record, "quarantined")
    record.evidence.setdefault("quarantine_events", []).append(now.isoformat())
    events = [policy.parse_utc_datetime(d) for d in record.evidence.get("quarantine_events", [])]
    events = [d for d in events if d is not None]
    if policy.quarantine_crosses_threshold(events, now) \
            and record.classification in (policy.VERIFIED, policy.LIMITED):
        profile.set_health(record.record_key, policy.STALE,
                           "stale: quarantine threshold crossed")


# ── Promotion / block decisions ──────────────────────────────────────────────
def evaluate_promotion(profile: CapabilityProfile, record: CapabilityRecord, *,
                       now: datetime, prior_fn=compute_prior) -> Proposal | None:
    if record.classification != policy.DISCOVERED:
        return None
    clean = int(record.evidence.get("clean", 0) or 0)
    if clean < 1:
        return None
    prior = prior_fn(profile, record.context_id, record.qualifier, record.capability)
    distinct_days = len(set(record.evidence.get("days", []) or []))
    has_contradiction = int(record.evidence.get("neg", 0) or 0) > 0
    if policy.meets_evidence_bar(clean_events=clean, distinct_days=distinct_days,
                                 prior=prior, has_contradiction=has_contradiction):
        return Proposal(record.record_key, record.classification, policy.VERIFIED,
                        f"{clean} clean event(s) over {distinct_days} day(s), prior={prior}",
                        kind="promote")
    return None


def evaluate_block(record: CapabilityRecord) -> Proposal | None:
    """A start grain that produced no usable start and has no source path after an
    event is a stable dead end → Blocked (reopenable)."""
    if record.classification != policy.DISCOVERED:
        return None
    if record.capability not in (policy.CAP_START_LIVE, policy.CAP_START_AUTHORITATIVE):
        return None
    clean = int(record.evidence.get("clean", 0) or 0)
    no_source = int(record.evidence.get("no_source", 0) or 0)
    if clean == 0 and no_source >= 1:
        return Proposal(record.record_key, record.classification, policy.BLOCKED,
                        "no authoritative start source and no live signal", kind="block")
    return None


# ── Causal re-evaluation window (concept §8) ─────────────────────────────────
def rows_in_causal_window(rows: list[dict], since: datetime, *,
                          timestamp_key: str = "Closing Observed At") -> list[dict]:
    """Rows whose observation timestamp is at/after the last known-good check —
    the only rows a contradiction re-flags (never all of history)."""
    out = []
    for row in rows:
        ts = policy.parse_utc_datetime(row.get(timestamp_key))
        if ts is not None and ts >= since:
            out.append(row)
    return out


# ── Deriving observations from settled bet rows ──────────────────────────────
# The actual-start resolver stamps its own source strings; map them to the
# capability qualifiers the profile is keyed on.
_ACTUAL_SOURCE_TO_QUALIFIER = {
    "mlb-statsapi-firstpitch": "mlb_statsapi",
    "espn-first-play": "espn",
    "espn-tennis-competition-start": "espn_tennis",
    "espn-ufc-round-one-start": "espn_fights",
}


def observation_from_bet(bet: dict, resolution) -> Observation | None:
    """Build an Observation from a settled bet's provenance columns. `resolution`
    is a context_registry.ContextResolution (already resolved by the caller); a
    NEW/unknown context yields None (nothing to verify)."""
    if resolution is None or not getattr(resolution, "is_known", False):
        return None
    detected = policy.parse_utc_datetime(bet.get("Start Detected At"))
    actual = policy.parse_utc_datetime(bet.get("Actual Start"))
    outcome = classify_start(detected, actual)
    if detected is not None:
        start_source = "toa_scores"
    else:
        raw = str(bet.get("Actual Start Source") or "").strip().lower()
        start_source = _ACTUAL_SOURCE_TO_QUALIFIER.get(raw, raw or "toa_scores")
    family = policy.market_family_for(bet.get("Market Key"), bet.get("Bet Type"))
    quality = str(bet.get("Closing Quality") or "").strip().upper()
    return Observation(
        context_id=resolution.context_id,
        event_id=str(bet.get("Event ID") or ""),
        observed_at=policy.parse_utc_datetime(bet.get("Closing Observed At")) or policy.now_utc(),
        identity_matched=bool(str(bet.get("Event ID") or "").strip()),
        start_source=start_source, start_outcome=outcome,
        capture_book=str(bet.get("Book") or ""), capture_family=family,
        capture_clean=(quality == "VERIFIED_CLOSE"),
    )


# ── Orchestrator ─────────────────────────────────────────────────────────────
def run_verification(profile: CapabilityProfile, observations: list[Observation], *,
                     apply: bool, now: datetime | None = None,
                     prior_fn=compute_prior, log_fn=None) -> list[Proposal]:
    """Fold every observation into evidence, then propose (or apply) promotions
    and blocks. Demotions already happened during accumulation. `apply` False =
    shadow: proposals are written as `proposed:` Notes markers, not applied."""
    now = now or policy.now_utc()
    touched: dict[str, CapabilityRecord] = {}
    for obs in observations:
        for rec in accumulate(profile, obs, now=now):
            touched[rec.record_key] = rec

    proposals: list[Proposal] = []
    for rec in touched.values():
        proposal = evaluate_promotion(profile, rec, now=now, prior_fn=prior_fn) \
            or evaluate_block(rec)
        if proposal is None:
            continue
        if apply:
            profile.transition(proposal.record_key, proposal.to_classification,
                               proposal.reason, authority=policy.AUTHORITY_AUTO)
            proposal.applied = True
        else:
            profile.annotate(proposal.record_key,
                             f"proposed: {proposal.from_classification}→"
                             f"{proposal.to_classification} ({proposal.reason})")
        if log_fn:
            log_fn("promotion" if proposal.kind == "promote" else "block", {
                "record_key": proposal.record_key, "to": proposal.to_classification,
                "reason": proposal.reason, "applied": proposal.applied})
        proposals.append(proposal)
    return proposals
