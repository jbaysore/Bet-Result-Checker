"""Frozen policy for New Context Onboarding (NEW_CONTEXT_ONBOARDING_PLAN.md).

This module is the single, versioned home of every tunable that governs how a
new competitive context is onboarded: the capability/market-family vocabulary,
the evidence bar for automatic promotion, the freshness windows, and the
negative-evidence severity/attribution rules (plan §P0.2 rows 1, 2, 6, 7, 11,
12, 15). It is deliberately pure — data + pure functions, no I/O and no heavy
imports — so both the checker (capability_profile, onboarding_verifier) and the
test suite can rely on identical arithmetic.

`POLICY_VERSION` stamps every capability record it classifies. A bump means
"old evidence may no longer apply": affected records go Health=Stale (never
Contradicted) and are NOT re-interpreted unless a release note names them
(concept §7, plan §P0.2 #14).

Everything here is PROPOSED until Josh ratifies the §P0.2 defaults table
(Gate P0). Ratifying changes the numbers below, not their shape.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

# ── Policy version ───────────────────────────────────────────────────────────
# Bumping this invalidates prior evidence for freshness purposes (→ Stale), and
# records carry the version they were classified under so history is never
# silently reinterpreted (concept §7).
POLICY_VERSION = 1


# ── Trust classification (concept §2) ────────────────────────────────────────
DISCOVERED = "Discovered"
VERIFIED = "Verified"
LIMITED = "Limited"
BLOCKED = "Blocked"
MANUAL = "Manual"
RETIRED = "Retired"
CLASSIFICATIONS = frozenset({DISCOVERED, VERIFIED, LIMITED, BLOCKED, MANUAL, RETIRED})

# Classifications eligible to authorize automatic trust when Health is Fresh
# (concept §2: "eligible ... only when Verified — or inside a Limited record's
# explicit constraints — and Fresh"). Limited is conditionally eligible: the
# requested row must fall inside its recorded constraints, checked by the
# caller, so it is listed here but gated separately in `authorizes_trust`.
TRUST_ELIGIBLE_CLASSIFICATIONS = frozenset({VERIFIED, LIMITED})


# ── Verification health (concept §2) ─────────────────────────────────────────
NOT_EVALUATED = "NotEvaluated"
FRESH = "Fresh"
STALE = "Stale"
CONTRADICTED = "Contradicted"
HEALTHS = frozenset({NOT_EVALUATED, FRESH, STALE, CONTRADICTED})

# Only a Fresh record authorizes automatic trust.
TRUST_ELIGIBLE_HEALTHS = frozenset({FRESH})


# ── Collection activity (concept §2) ─────────────────────────────────────────
IDLE = "Idle"
COLLECTING = "Collecting"
AWAITING_POST_EVENT = "AwaitingPostEvent"
ACTIVITIES = frozenset({IDLE, COLLECTING, AWAITING_POST_EVENT})


# ── Capability vocabulary (plan §0.3) ────────────────────────────────────────
CAP_IDENTITY = "identity"
CAP_DISCOVERY = "discovery"
CAP_START_LIVE = "start_live"
CAP_START_AUTHORITATIVE = "start_authoritative"
CAP_CAPTURE = "capture"
CAP_SETTLEMENT = "settlement"
CAP_RECOVERY = "recovery"
CAP_BENCHMARK = "benchmark"
CAPABILITIES = frozenset({
    CAP_IDENTITY, CAP_DISCOVERY, CAP_START_LIVE, CAP_START_AUTHORITATIVE,
    CAP_CAPTURE, CAP_SETTLEMENT, CAP_RECOVERY, CAP_BENCHMARK,
})

# Capabilities a bet's CLV trust depends on at finalize time (plan §0.5,
# Phase 2): a capture cannot finalize VERIFIED_CLOSE unless these resolve to
# (Verified|in-Limited-bounds) + Fresh. Settlement is deliberately absent —
# settlement runs independently of closing-price trust (concept non-goal #3).
CLV_REQUIRED_CAPABILITIES = (CAP_IDENTITY, CAP_START_LIVE, CAP_CAPTURE)


# ── Market families v1 (plan §0.3, §P0.2 #2) ─────────────────────────────────
MF_H2H = "h2h"
MF_FEATURED = "featured"        # spread / total mainlines + their alternates
MF_TEAM_TOTAL = "team_total"
MF_PROP = "prop"
MF_OUTRIGHT = "outright"
MARKET_FAMILIES = frozenset({MF_H2H, MF_FEATURED, MF_TEAM_TOTAL, MF_PROP, MF_OUTRIGHT})


def market_family_for(market_key: str | None = None, bet_type: str | None = None) -> str:
    """Collapse an Odds-API market key (or a sheet Bet Type) to a market family.

    Market key wins when present (it is more specific); Bet Type is the fallback
    for legacy rows that predate the Market Key column. Unknown inputs default to
    the moneyline family, the narrowest safe assumption for capture grain.
    """
    key = str(market_key or "").strip().lower()
    if key:
        if key in ("h2h", "h2h_3_way", "h2h_lay") or key.startswith("h2h"):
            return MF_H2H
        if "team_total" in key:
            return MF_TEAM_TOTAL
        if key.startswith("spreads") or key.startswith("alternate_spreads") \
                or key.startswith("totals") or key.startswith("alternate_totals"):
            return MF_FEATURED
        if key.startswith("outrights") or key.startswith("futures"):
            return MF_OUTRIGHT
        if key.startswith("player_") or key.startswith("batter_") \
                or key.startswith("pitcher_") or "_props" in key:
            return MF_PROP
    bt = str(bet_type or "").strip().lower()
    if bt in ("moneyline", "draw"):
        return MF_H2H
    if bt in ("spread", "total"):
        return MF_FEATURED
    if bt == "prop":
        return MF_PROP
    return MF_H2H


# ── Market class for settlement grain (plan §0.3 `settlement`) ───────────────
# Settlement varies by market CLASS (moneyline/spread/total/prop/…), a coarser
# split than capture's market family — it does not distinguish mainline from
# team-total, only how the result is computed.
def market_class_for(bet_type: str | None = None, market_key: str | None = None) -> str:
    bt = str(bet_type or "").strip().lower()
    if bt in ("moneyline", "draw"):
        return "moneyline"
    if bt == "spread":
        return "spread"
    if bt == "total":
        return "total"
    if bt == "prop":
        return "prop"
    family = market_family_for(market_key, bet_type)
    return {MF_H2H: "moneyline", MF_FEATURED: "spread",
            MF_TEAM_TOTAL: "total", MF_PROP: "prop", MF_OUTRIGHT: "outright"}.get(family, "moneyline")


# ── Evidence bar (concept §7, plan §P0.2 #1) ─────────────────────────────────
# System correctness (did OUR mapping/parsing/capture work) is proven by ≥1
# clean exactly-matched event at the exact grain and never skippable. Source
# correctness (does the source report true starts/results, including when events
# go sideways) is distributional and reduced — never removed — by a strong
# family prior on the SAME source.
FAMILY_PRIOR_MIN_SIBLINGS = 3       # verified sibling contexts on the same source
FAMILY_PRIOR_MIN_IRREGULAR = 1      # ≥1 irregular event handled correctly

STRONG_PRIOR = "STRONG"
NO_PRIOR = "NONE"

# Clean context-specific events required to promote Discovered → Verified.
EVIDENCE_EVENTS_STRONG_PRIOR = 1    # strong same-source family prior
EVIDENCE_EVENTS_NEW = 3             # new family / new source
EVIDENCE_DISTINCT_DAYS_NEW = 2      # spread across ≥2 distinct days


def family_prior_strength(sibling_verified_count: int, family_irregular_handled: int) -> str:
    """STRONG only when the same source has enough verified siblings AND has
    demonstrably handled an irregular event; otherwise NONE (concept §6)."""
    if (sibling_verified_count >= FAMILY_PRIOR_MIN_SIBLINGS
            and family_irregular_handled >= FAMILY_PRIOR_MIN_IRREGULAR):
        return STRONG_PRIOR
    return NO_PRIOR


def required_clean_events(prior: str) -> int:
    return EVIDENCE_EVENTS_STRONG_PRIOR if prior == STRONG_PRIOR else EVIDENCE_EVENTS_NEW


def required_distinct_days(prior: str) -> int:
    # A strong prior promotes on the first clean event, so one day suffices;
    # a new source must see ≥2 distinct days so a single bad slate cannot pass.
    return 1 if prior == STRONG_PRIOR else EVIDENCE_DISTINCT_DAYS_NEW


def meets_evidence_bar(*, clean_events: int, distinct_days: int, prior: str,
                       has_contradiction: bool = False) -> bool:
    """Whether accumulated positive evidence justifies Discovered → Verified.

    A single unresolved contradiction blocks promotion regardless of volume
    (concept §7: negative evidence weighs against positive). The caller is
    responsible for having already proven system correctness (≥1 clean exact
    match) — that is subsumed by `clean_events >= 1`.
    """
    if has_contradiction or clean_events < 1:
        return False
    return (clean_events >= required_clean_events(prior)
            and distinct_days >= required_distinct_days(prior))


# ── Freshness (concept §8, plan §P0.2 #6) ────────────────────────────────────
# A verified record idle past this window, or whose source route / policy
# version changed, becomes Stale (history intact, new rows fail closed until
# re-confirmation). Season boundary is approximated by the idle window in v1;
# there is no per-sport calendar (plan "out of scope").
FRESHNESS_IDLE_DAYS = 120
_FRESHNESS_IDLE = timedelta(days=FRESHNESS_IDLE_DAYS)


def is_idle_stale(last_verified: datetime | None, now: datetime,
                  *, idle: timedelta = _FRESHNESS_IDLE) -> bool:
    """True when a record has aged beyond its freshness window since it was last
    verified. A record never verified (None) is not "stale" — it is
    NotEvaluated, a distinct state the caller owns."""
    if last_verified is None:
        return False
    return (now - last_verified) > idle


def freshness_after_policy_bump(record_policy_version: int) -> str:
    """A record classified under an older policy version is Stale, not
    Contradicted — the evidence may still hold, it just needs re-confirmation
    (plan §P0.2 #14). Same-version records are unaffected here."""
    return STALE if record_policy_version < POLICY_VERSION else FRESH


# ── Negative evidence: severity, attribution, quarantine (plan §P0.2 #7) ─────
# Failures are attributed to the NARROWEST capability that can be blamed
# (concept §5/§8). A missing sportsbook market is a coverage limitation on
# capture, never evidence against identity or actual-start reliability.
FAILURE_IDENTITY_MISMATCH = "identity_mismatch"
FAILURE_AMBIGUOUS_MATCH = "ambiguous_match"
FAILURE_START_CONTRADICTION = "start_contradiction"
FAILURE_POST_START_AS_VERIFIED = "post_start_as_verified"
FAILURE_STALE_QUOTE = "stale_quote"
FAILURE_MISSING_MARKET = "missing_market"
FAILURE_TRANSIENT_EMPTY = "transient_empty"

# failure kind → capability it is attributed to.
NEGATIVE_ATTRIBUTION = {
    FAILURE_IDENTITY_MISMATCH: CAP_IDENTITY,
    FAILURE_AMBIGUOUS_MATCH: CAP_IDENTITY,
    FAILURE_START_CONTRADICTION: CAP_START_LIVE,
    FAILURE_POST_START_AS_VERIFIED: CAP_START_LIVE,
    FAILURE_STALE_QUOTE: CAP_CAPTURE,
    FAILURE_MISSING_MARKET: CAP_CAPTURE,
    FAILURE_TRANSIENT_EMPTY: CAP_DISCOVERY,
}

# Severity outcomes.
SEV_IMMEDIATE = "IMMEDIATE_CONTRADICTED"  # one observation sets Health=Contradicted
SEV_QUARANTINE = "QUARANTINE"             # counted, investigated, not an instant demote
SEV_COVERAGE = "COVERAGE"                 # a by-design coverage miss; not held against trust

# High-severity failures that immediately contradict (concept §8).
IMMEDIATE_CONTRADICTION_FAILURES = frozenset({
    FAILURE_POST_START_AS_VERIFIED, FAILURE_START_CONTRADICTION,
})
# Ambiguous / transient failures that quarantine rather than demote.
QUARANTINE_FAILURES = frozenset({FAILURE_AMBIGUOUS_MATCH, FAILURE_TRANSIENT_EMPTY})

# Authoritative-vs-live start disagreement beyond this is a contradiction, not a
# tolerable detection lag (plan §P0.2 #1).
START_DISAGREEMENT_CONTRADICTION_SECONDS = 10 * 60

# Quarantine accumulation + decay.
QUARANTINE_WINDOW_DAYS = 14
QUARANTINE_THRESHOLD = 3            # ≥3 quarantined within the window → Stale
QUARANTINE_DECAY_CLEAN_DAYS = 30   # counters reset after this many clean days


def severity_for(failure_kind: str) -> str:
    if failure_kind in IMMEDIATE_CONTRADICTION_FAILURES:
        return SEV_IMMEDIATE
    if failure_kind == FAILURE_MISSING_MARKET:
        return SEV_COVERAGE
    if failure_kind in QUARANTINE_FAILURES:
        return SEV_QUARANTINE
    return SEV_QUARANTINE


def attribute_failure(failure_kind: str) -> str | None:
    """The capability a failure counts against, or None if unknown."""
    return NEGATIVE_ATTRIBUTION.get(failure_kind)


def start_disagreement_is_contradiction(delta_seconds: float) -> bool:
    """A live-vs-authoritative start gap larger than the tolerance is a
    contradiction. Detection lag is one-directional but we compare magnitude."""
    return abs(delta_seconds) > START_DISAGREEMENT_CONTRADICTION_SECONDS


def quarantine_crosses_threshold(events: list[datetime], now: datetime) -> bool:
    """True when ≥QUARANTINE_THRESHOLD quarantine events fall inside the trailing
    window ending at `now` (plan §P0.2 #7: 3 within 14 days → Stale)."""
    window_start = now - timedelta(days=QUARANTINE_WINDOW_DAYS)
    recent = [event for event in events if event is not None and event >= window_start]
    return len(recent) >= QUARANTINE_THRESHOLD


def quarantine_decayed(last_quarantine: datetime | None, now: datetime) -> bool:
    """Whether enough clean days have elapsed to reset quarantine counters."""
    if last_quarantine is None:
        return True
    return (now - last_quarantine) >= timedelta(days=QUARANTINE_DECAY_CLEAN_DAYS)


# ── Classification transition legality (concept §2 "Transition governance") ──
# Authority for each edge; capability_profile.transition() raises on any edge
# not permitted here. Health transitions (Fresh/Stale/Contradicted) are a
# separate axis handled by the freshness/severity functions above, not here.
AUTHORITY_AUTO = "auto"
AUTHORITY_USER = "user"


def classification_transition_authority(from_c: str, to_c: str) -> str | None:
    """Return the authority required for a classification edge, or None if the
    edge is illegal. Same→same is a legal no-op requiring no special authority.

    Legal edges (concept §2):
      Unseen→Discovered (record creation, handled by the profile store), then
      Discovered→{Verified,Limited,Blocked} (auto), Blocked→Discovered (auto),
      Any→{Manual,Retired} (user), {Manual,Retired}→Discovered (user).
    """
    if from_c == to_c:
        return AUTHORITY_AUTO
    if to_c == MANUAL or to_c == RETIRED:
        return AUTHORITY_USER
    if from_c in (MANUAL, RETIRED) and to_c == DISCOVERED:
        return AUTHORITY_USER
    if from_c == DISCOVERED and to_c in (VERIFIED, LIMITED, BLOCKED):
        return AUTHORITY_AUTO
    if from_c == BLOCKED and to_c == DISCOVERED:
        return AUTHORITY_AUTO
    return None


def is_legal_classification_transition(from_c: str, to_c: str, authority: str) -> bool:
    required = classification_transition_authority(from_c, to_c)
    if required is None:
        return False
    # A user may perform an automatic edge, but automation may not perform a
    # user-only edge (Manual/Retired are user decisions, concept safety #10/#11).
    if required == AUTHORITY_USER:
        return authority == AUTHORITY_USER
    return authority in (AUTHORITY_AUTO, AUTHORITY_USER)


# ── Trust authorization (concept §2) ─────────────────────────────────────────
def authorizes_trust(classification: str, health: str, *, within_limited_bounds: bool = False) -> bool:
    """A capability record authorizes automatic row trust only when Verified (or
    Limited with the row inside its constraints) AND Fresh."""
    if health not in TRUST_ELIGIBLE_HEALTHS:
        return False
    if classification == VERIFIED:
        return True
    if classification == LIMITED:
        return within_limited_bounds
    return False


# ── Benchmark policy (concept §11, plan §P0.2 #11) ───────────────────────────
# Pinnacle only in v1; a benchmark requires an EXACT market/period/point/side
# match — no fallback source, no "close enough" substitution. A missing
# benchmark reports "unbenchmarkable" and demotes nothing (safety #12).
BENCHMARK_SOURCE = "pinnacle"
BENCHMARK_REQUIRES_EXACT_MATCH = True


# ── Scanner-intent policy (concept §3, plan §P0.2 #12) ───────────────────────
SCANNER_DEDUP_FIELDS = ("context_id", "book", "market_family")
SCANNER_INTENT_EXPIRY_DAYS = 7          # drop a surfaced-only discovery after this
SCANNER_MAX_APPENDS_PER_DAY = 20        # Discovery Queue append budget
SCANNER_USES_EXTRA_CREDITS = False      # reuse already-fetched scan data only


def scanner_dedup_key(context_id: str, book: str, market_family: str) -> str:
    return "|".join((context_id, book, market_family))


# ── Case lifecycle (concept §10, plan §P0.2 #15) ─────────────────────────────
# One parent case per context_id, one child issue per record_key. A case closes
# when every child reaches a classified outcome; a Blocked child leaves its
# limitation on the Capabilities row and closes the case. Reopen on a new source
# mapping, policy bump, or new qualifying event.
CASE_OUTCOME_VERIFIED = VERIFIED
CASE_OUTCOME_LIMITED = LIMITED
CASE_OUTCOME_BLOCKED = BLOCKED
CASE_OUTCOME_MANUAL = MANUAL
CASE_OUTCOME_RETIRED = RETIRED
CASE_CLOSING_OUTCOMES = frozenset({
    CASE_OUTCOME_VERIFIED, CASE_OUTCOME_LIMITED, CASE_OUTCOME_BLOCKED,
    CASE_OUTCOME_MANUAL, CASE_OUTCOME_RETIRED,
})
CASE_REOPEN_REASONS = frozenset({"new_source_mapping", "policy_bump", "new_qualifying_event"})


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
