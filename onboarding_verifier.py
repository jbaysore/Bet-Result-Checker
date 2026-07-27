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

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

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
    observation_id: str = ""           # BetID fallback when provider event id is absent
    observed_at: datetime | None = None
    identity_matched: bool = True        # exactly one unambiguous event matched
    start_source: str = "toa_scores"     # qualifier of the start capability exercised
    start_outcome: str = START_UNRESOLVED
    capture_book: str = ""
    capture_family: str = ""
    capture_clean: bool = False          # a fresh pre-start close existed
    irregular: bool = False              # event went sideways and was handled correctly
    missing_market: bool = False         # book did not offer the market (coverage, not a failure)
    benchmark_available: bool | None = None  # exact Pinnacle market/period/point match


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


EVENT_LEDGER_KEY = "event_outcomes_v2"
EVENT_LEDGER_VERSION = 2
OUTCOME_CODES = {
    "clean": "c", "negative": "n", "contradiction": "x",
    "no_source": "s", "coverage": "v",
}


def _compact_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc) \
        .isoformat(timespec="seconds").replace("+00:00", "Z")


def _reset_legacy_evidence(record: CapabilityRecord) -> None:
    """Start the v2 ledger without carrying inflated observation counters."""
    if int(record.evidence.get("evidence_version", 0) or 0) >= EVENT_LEDGER_VERSION:
        return
    preserved = {key: record.evidence[key] for key in
                 ("seeded", "scanner_discoveries") if key in record.evidence}
    record.evidence.clear()
    record.evidence.update(preserved)
    record.evidence["evidence_version"] = EVENT_LEDGER_VERSION
    record.evidence[EVENT_LEDGER_KEY] = {}


def reset_verifier_evidence(record: CapabilityRecord) -> None:
    """Public rebuild helper: clear derived evidence while preserving discovery."""
    preserved = {key: record.evidence[key] for key in
                 ("seeded", "scanner_discoveries") if key in record.evidence}
    record.evidence.clear()
    record.evidence.update(preserved)
    record.evidence["evidence_version"] = EVENT_LEDGER_VERSION
    record.evidence[EVENT_LEDGER_KEY] = {}
    record.last_checked = None


def _ledger(record: CapabilityRecord) -> dict:
    _reset_legacy_evidence(record)
    ledger = record.evidence.setdefault(EVENT_LEDGER_KEY, {})
    if not isinstance(ledger, dict):
        ledger = {}
        record.evidence[EVENT_LEDGER_KEY] = ledger
    return ledger


def _prune_ledger(record: CapabilityRecord, reference: datetime) -> None:
    ledger = _ledger(record)
    cutoff = reference - timedelta(days=policy.EVIDENCE_LEDGER_RETENTION_DAYS)
    for token, entry in list(ledger.items()):
        at = policy.parse_utc_datetime(entry[1] if isinstance(entry, list)
                                       and len(entry) > 1 else None)
        if at is None or at < cutoff:
            ledger.pop(token, None)


def _event_outcomes(record: CapabilityRecord) -> list[tuple[datetime, str]]:
    out = []
    for entry in _ledger(record).values():
        if not isinstance(entry, list) or len(entry) < 3:
            continue
        at = policy.parse_utc_datetime(entry[1])
        outcome = entry[2]
        if at is None or outcome not in {"c", "n", "x"}:
            continue
        out.append((at, "clean" if outcome == "c" else "negative"))
    return out


def _sync_evidence(record: CapabilityRecord) -> None:
    entries = [entry for entry in _ledger(record).values()
               if isinstance(entry, list) and len(entry) >= 3]
    clean = [entry for entry in entries if entry[2] == "c"]
    negative = [entry for entry in entries if entry[2] == "n"]
    contradictions = [entry for entry in entries if entry[2] == "x"]
    no_source = [entry for entry in entries if entry[2] == "s"]
    record.evidence["clean"] = len(clean)
    record.evidence["days"] = sorted({str(entry[1])[:10] for entry in clean if entry[1]})
    record.evidence["irregular_ok"] = sum(
        1 for entry in clean if len(entry) > 3 and bool(entry[3]))
    record.evidence["neg"] = len(negative) + len(contradictions)
    record.evidence["quarantined"] = len(negative)
    record.evidence["contradictions"] = len(contradictions)
    record.evidence["no_source"] = len(no_source)
    # Retain a small causal timestamp tail for row re-flagging/diagnostics; the
    # authoritative distinct-event history is the compact ledger above.
    record.evidence["quarantine_events"] = sorted(
        (entry[1] for entry in negative), reverse=True)[:20]


def _claim(record: CapabilityRecord, obs: Observation, signature: str) -> str | None:
    """Claim one state per sporting event, retained beyond the rescan window."""
    raw_id = obs.event_id or obs.observation_id
    if not raw_id and obs.observed_at is not None:
        raw_id = obs.observed_at.isoformat()
    if not raw_id:
        return None
    at = obs.observed_at or policy.now_utc()
    _prune_ledger(record, at)
    token = hashlib.sha256(str(raw_id).encode("utf-8")).hexdigest()[:16]
    state = hashlib.sha256(signature.encode("utf-8")).hexdigest()[:8]
    prior = _ledger(record).get(token)
    if isinstance(prior, list) and prior and prior[0] == state:
        return None
    prior_at = policy.parse_utc_datetime(prior[1] if isinstance(prior, list)
                                         and len(prior) > 1 else None)
    if prior_at is not None and prior_at > at:
        return None
    previous_outcome = prior[2] if isinstance(prior, list) and len(prior) > 2 else ""
    previous_irregular = prior[3] if isinstance(prior, list) and len(prior) > 3 else False
    _ledger(record)[token] = [state, _compact_iso(at), previous_outcome, previous_irregular]
    return token


def _set_event_outcome(record: CapabilityRecord, token: str, outcome: str,
                       observed_at: datetime, *, irregular: bool = False) -> None:
    entry = _ledger(record).get(token)
    if not isinstance(entry, list):
        return
    entry[1] = _compact_iso(observed_at)
    entry[2] = OUTCOME_CODES[outcome]
    entry[3] = bool(irregular)
    _sync_evidence(record)


def _record_clean(profile: CapabilityProfile, record: CapabilityRecord,
                  observed_at: datetime, event_token: str, *, irregular: bool = False) -> None:
    _set_event_outcome(record, event_token, "clean", observed_at, irregular=irregular)
    if record.classification in (policy.VERIFIED, policy.LIMITED):
        if record.health == policy.STALE \
                and policy.meets_reconfirmation_bar(_event_outcomes(record)):
            record.health = policy.FRESH
            record.notes = (record.notes + "\n" if record.notes else "") + \
                f"{observed_at.date().isoformat()} re-confirmed by 3 clean events over 2 days"
        if record.health == policy.FRESH:
            record.last_verified = max(filter(None, [record.last_verified, observed_at]))
            record.policy_version = policy.POLICY_VERSION


def reconcile_rebuilt_health(record: CapabilityRecord, previous_health: str, *,
                             now: datetime) -> None:
    """Set health from rebuilt distinct-event evidence, preserving sparse trust."""
    if record.classification not in (policy.VERIFIED, policy.LIMITED):
        return
    if (record.capability == policy.CAP_CAPTURE
            and record.qualifier.startswith("any|")
            and "grandfathered bridge superseded" in record.notes.lower()):
        record.health = policy.STALE
        return
    outcomes = _event_outcomes(record)
    if int(record.evidence.get("contradictions", 0) or 0):
        record.health = policy.CONTRADICTED
        return
    if policy.reliability_crosses_threshold(outcomes, now):
        record.health = policy.STALE
        return
    clean = int(record.evidence.get("clean", 0) or 0)
    days = len(set(record.evidence.get("days", []) or []))
    if clean >= policy.RECONFIRM_CLEAN_EVENTS and days >= policy.RECONFIRM_DISTINCT_DAYS:
        record.health = policy.FRESH
        clean_dates = [at for at, outcome in outcomes if outcome == "clean"]
        if clean_dates:
            record.last_verified = max(clean_dates)
        if previous_health != policy.FRESH:
            record.notes = (record.notes + "\n" if record.notes else "") + \
                f"{now.date().isoformat()} re-confirmed by rebuilt distinct-event evidence"
        return
    # Sparse recent evidence cannot erase an older trust decision either way.
    record.health = previous_health


def age_stale_records(profile: CapabilityProfile, *, now: datetime) -> list[CapabilityRecord]:
    """Persist policy-version/idle freshness aging before evaluating trust."""
    aged = []
    for rec in profile.records():
        if (rec.classification in (policy.VERIFIED, policy.LIMITED)
                and rec.health == policy.FRESH
                and rec.effective_health(now) == policy.STALE):
            profile.set_health(rec.record_key, policy.STALE,
                               "stale: policy version or idle freshness window")
            aged.append(rec)
    return aged


def narrow_grandfathered_bridges(profile: CapabilityProfile) -> list[CapabilityRecord]:
    """Disable any|family trust once one exact book grain has verified.

    We preserve the seed record/history and stale its health rather than using a
    user-only Retired classification transition. Existing trusted rows remain
    historical; future unseen books fail closed from their first bet.
    """
    verified = set()
    for rec in profile.records():
        if (rec.capability == policy.CAP_CAPTURE and not rec.qualifier.startswith("any|")
                and rec.classification in (policy.VERIFIED, policy.LIMITED)
                and rec.effective_health() == policy.FRESH):
            _, _, family = rec.qualifier.partition("|")
            verified.add((rec.context_id, family))
    narrowed = []
    for rec in profile.records():
        if (rec.capability != policy.CAP_CAPTURE or not rec.qualifier.startswith("any|")):
            continue
        family = rec.qualifier.split("|", 1)[1]
        if ((rec.context_id, family) in verified and rec.health == policy.FRESH):
            profile.set_health(rec.record_key, policy.STALE,
                               "stale: grandfathered bridge superseded by exact book grain")
            narrowed.append(rec)
    return narrowed


def accumulate(profile: CapabilityProfile, obs: Observation, *, now: datetime) -> list[CapabilityRecord]:
    """Fold one observation into the evidence counters of its grain records,
    creating Discovered records as needed. Returns the records touched.

    Demotion of an already-Verified record is applied here and now (never
    shadowed); promotion is decided later by `run_verification`.
    """
    touched: list[CapabilityRecord] = []

    # identity
    ident = _ensure(profile, obs.context_id, policy.CAP_IDENTITY, "toa", now)
    event_time = obs.observed_at or now
    claim = _claim(ident, obs, f"identity:{obs.identity_matched}:{obs.irregular}")
    if claim:
        if obs.identity_matched:
            _record_clean(profile, ident, event_time, claim, irregular=obs.irregular)
        else:
            _apply_negative(profile, ident, policy.FAILURE_IDENTITY_MISMATCH,
                            event_time, event_token=claim)
        touched.append(ident)

    # start
    cap, qual = start_grain(obs.start_source or "toa_scores")
    start_rec = _ensure(profile, obs.context_id, cap, qual, now)
    claim = _claim(start_rec, obs,
                   f"start:{obs.start_source}:{obs.start_outcome}:{obs.irregular}")
    if claim:
        if obs.start_outcome in (START_AGREEMENT, START_RECOVERABLE):
            _record_clean(profile, start_rec, event_time, claim, irregular=obs.irregular)
        elif obs.start_outcome == START_CONTRADICTION:
            _apply_negative(profile, start_rec, policy.FAILURE_START_CONTRADICTION,
                            event_time, event_token=claim)
        elif obs.start_outcome == START_UNRESOLVED:
            _apply_negative(profile, start_rec, policy.FAILURE_TRANSIENT_EMPTY,
                            event_time, event_token=claim)
        elif obs.start_outcome == START_MISSING:
            _set_event_outcome(start_rec, claim, "no_source", event_time)
        touched.append(start_rec)

    # capture
    if obs.capture_family:
        book = (obs.capture_book or "any").strip().lower() or "any"
        crec = _ensure(profile, obs.context_id, policy.CAP_CAPTURE,
                       f"{book}|{obs.capture_family}", now)
        claim = _claim(crec, obs, f"capture:{obs.capture_clean}:{obs.missing_market}")
        if claim:
            if obs.missing_market:
                _set_event_outcome(crec, claim, "coverage", event_time)
            elif obs.capture_clean:
                _record_clean(profile, crec, event_time, claim)
            else:
                _apply_negative(profile, crec, policy.FAILURE_STALE_QUOTE,
                                event_time, event_token=claim)
            touched.append(crec)

        # Benchmark availability is an independent grain. A miss is a visible
        # CLV-computability limitation; it never feeds capture evidence or
        # demotes a trustworthy closing price (concept safety #12).
        bqual = f"{policy.BENCHMARK_SOURCE}|{obs.capture_family}"
        brec = _ensure(profile, obs.context_id, policy.CAP_BENCHMARK, bqual, now)
        if brec.classification == policy.BLOCKED and obs.benchmark_available:
            brec = profile.transition(brec.record_key, policy.DISCOVERED,
                                      "benchmark appeared on a later qualifying event")
        claim = _claim(brec, obs, f"benchmark:{obs.benchmark_available}")
        if claim:
            if obs.benchmark_available:
                _record_clean(profile, brec, event_time, claim)
            elif obs.benchmark_available is False:
                _set_event_outcome(brec, claim, "no_source", event_time)
            touched.append(brec)

    for rec in touched:
        profile._store(rec)  # persist counters (sink write in live mode; in-memory in tests)
    return touched


# ── Demotion (never shadowed) ────────────────────────────────────────────────
def _apply_negative(profile: CapabilityProfile, record: CapabilityRecord,
                    failure_kind: str, now: datetime,
                    event_token: str | None = None) -> None:
    """Attribute a negative observation to `record` and demote if warranted."""
    severity = policy.severity_for(failure_kind)
    if severity == policy.SEV_COVERAGE:
        return
    if not event_token:
        # Direct policy/unit-test calls do not carry an Observation. Give each
        # such call a stable distinct slot without affecting production keys.
        event_token = hashlib.sha256(
            f"manual:{now.isoformat()}:{len(_ledger(record))}".encode("utf-8")
        ).hexdigest()[:16]
        _ledger(record)[event_token] = ["manual", _compact_iso(now), "", False]
    if severity == policy.SEV_IMMEDIATE:
        _set_event_outcome(record, event_token, "contradiction", now)
        if record.classification in (policy.VERIFIED, policy.LIMITED):
            profile.set_health(record.record_key, policy.CONTRADICTED,
                               f"contradicted: {failure_kind}")
        else:
            record.health = policy.CONTRADICTED  # Discovered stays Discovered, health noted
        return
    _set_event_outcome(record, event_token, "negative", now)
    if policy.reliability_crosses_threshold(_event_outcomes(record), now) \
            and record.classification in (policy.VERIFIED, policy.LIMITED):
        profile.set_health(record.record_key, policy.STALE,
                           "stale: distinct-event reliability threshold crossed")


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
    has_contradiction = (int(record.evidence.get("contradictions", 0) or 0) > 0
                         or record.health == policy.CONTRADICTED)
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
    if record.capability not in (policy.CAP_START_LIVE, policy.CAP_START_AUTHORITATIVE,
                                 policy.CAP_BENCHMARK):
        return None
    clean = int(record.evidence.get("clean", 0) or 0)
    no_source = int(record.evidence.get("no_source", 0) or 0)
    if clean == 0 and no_source >= 1:
        if record.capability == policy.CAP_BENCHMARK:
            return Proposal(record.record_key, record.classification, policy.BLOCKED,
                            "no exact Pinnacle benchmark for this market family", kind="block")
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


def observation_identity(bet: dict) -> tuple[str, str]:
    """Return provider event id plus a stable fixture fallback for dedupe.

    Several bets on an older row may have no provider Event ID. Falling back
    directly to BetID turns one game into many reliability votes, so use the
    fixture identity first (date/start disambiguate ordinary doubleheaders).
    """
    event_id = str(bet.get("Event ID") or "").strip()
    if event_id:
        return event_id, event_id
    parts = [str(bet.get(key) or "").strip().lower() for key in
             ("Sport", "Team 1", "Team 2", "Game Date", "Game Start Time")]
    if parts[0] and parts[3] and (parts[1] or parts[2]):
        return "", "fixture:" + "|".join(parts)
    return "", "bet:" + str(bet.get("BetID") or "").strip()


def observation_from_bet(bet: dict, resolution) -> Observation | None:
    """Build an Observation from a settled bet's provenance columns. `resolution`
    is a context_registry.ContextResolution (already resolved by the caller); a
    NEW/unknown context yields None (nothing to verify)."""
    if resolution is None or not getattr(resolution, "is_known", False):
        return None
    detected = policy.parse_utc_datetime(bet.get("Start Detected At"))
    actual = policy.parse_utc_datetime(bet.get("Actual Start"))
    outcome = classify_start(detected, actual)
    if (actual is None and bet.get("__actual_start_attempted")
            and not bet.get("__actual_start_route_missing")):
        # A routed provider miss is transient/unresolved, not proof that no
        # source exists. Never auto-Block a context because ESPN/MLB had a bad
        # response during one verifier pass.
        outcome = START_UNRESOLVED
    if detected is not None:
        start_source = "toa_scores"
    else:
        raw = str(bet.get("Actual Start Source") or "").strip().lower()
        start_source = _ACTUAL_SOURCE_TO_QUALIFIER.get(raw, raw or "toa_scores")
    family = policy.market_family_for(bet.get("Market Key"), bet.get("Bet Type"))
    quality = str(bet.get("Closing Quality") or "").strip().upper()
    notes = str(bet.get("Notes") or "").lower()
    gate_capped_clean = "onboarding:" in notes and "onboarding: demoted" not in notes
    event_id, observation_id = observation_identity(bet)
    return Observation(
        context_id=resolution.context_id,
        event_id=event_id,
        observation_id=observation_id,
        observed_at=policy.parse_utc_datetime(bet.get("Closing Observed At")) or policy.now_utc(),
        identity_matched=bool(str(bet.get("Event ID") or "").strip()),
        start_source=start_source, start_outcome=outcome,
        capture_book=str(bet.get("Book") or ""), capture_family=family,
        # An onboarding marker is written only when the pre-gate quality was
        # VERIFIED_CLOSE. Treat it as clean evidence or a new context's capture
        # grain could never earn promotion (the gate itself made it provisional).
        capture_clean=(quality == "VERIFIED_CLOSE" or gate_capped_clean),
        benchmark_available=bool(str(bet.get("Pinnacle Close") or "").strip()),
    )


# ── Orchestrator ─────────────────────────────────────────────────────────────
def run_verification(profile: CapabilityProfile, observations: list[Observation], *,
                     apply: bool, now: datetime | None = None,
                     prior_fn=compute_prior, log_fn=None,
                     rebuilt_health: dict[str, str] | None = None) -> list[Proposal]:
    """Fold every observation into evidence, then propose (or apply) promotions
    and blocks. Demotions already happened during accumulation. `apply` False =
    shadow: proposals are written as `proposed:` Notes markers, not applied."""
    now = now or policy.now_utc()
    age_stale_records(profile, now=now)
    touched: dict[str, CapabilityRecord] = {}
    for obs in observations:
        for rec in accumulate(profile, obs, now=now):
            touched[rec.record_key] = rec

    if rebuilt_health is not None:
        for rec in profile.records():
            reconcile_rebuilt_health(
                rec, rebuilt_health.get(rec.record_key, rec.health), now=now)
            profile._store(rec)

    proposals: list[Proposal] = []
    # Re-evaluate every Discovered record with durable evidence, not only grains
    # changed in this pass. Otherwise turning promotion shadow off later would
    # never apply already-proposed transitions because their observations were
    # correctly deduplicated.
    candidates = {rec.record_key: rec for rec in profile.records()
                  if rec.classification == policy.DISCOVERED and rec.evidence}
    candidates.update(touched)
    for rec in candidates.values():
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
    if apply:
        narrow_grandfathered_bridges(profile)
    return proposals
