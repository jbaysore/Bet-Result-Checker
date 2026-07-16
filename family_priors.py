"""Family priors for New Context Onboarding (concept §6, plan Phase 4).

Source correctness (does a results/scores provider report true starts and
results, including when events go sideways) is distributional — it can be
supported by a *prior* built from sibling contexts already verified on the SAME
source, which lowers how much new evidence a sibling needs. It is a prior, never
inheritance: it reduces the QUANTITY of source evidence required, and never
removes the requirement to prove SYSTEM correctness (our mapping/parsing/capture)
for the exact new context with at least one clean event.

Concretely: a strong prior for `soccer/x` on `toa_scores` requires enough OTHER
verified soccer contexts on `toa_scores`, including at least one that has
handled an irregular event. A new combat promotion gets no prior from UFC
timing (different source/behavior); a special event gets none for either its
sport's schedule or its market behavior — those fall out naturally because they
have no verified same-source, same-family siblings.
"""

from __future__ import annotations

import onboarding_policy as policy


def _family(context_id: str) -> str:
    return str(context_id or "").split("/", 1)[0]


def compute_prior(profile, context_id: str, source: str, capability: str) -> str:
    """The prior strength (STRONG_PRIOR | NO_PRIOR) for a capability grain.

    A sibling counts when it is a DIFFERENT context in the same family with a
    Verified + Fresh record for the same (capability, source qualifier). The
    prior is STRONG only when there are enough such siblings AND the family has
    demonstrably handled an irregular event on that source (§P0.2 #1).
    """
    family = _family(context_id)
    if not family:
        return policy.NO_PRIOR

    siblings = [
        rec for rec in profile.records()
        if rec.capability == capability
        and rec.qualifier == source
        and rec.context_id != context_id
        and _family(rec.context_id) == family
        and rec.classification == policy.VERIFIED
        and rec.health == policy.FRESH
    ]
    irregular_handled = sum(int(rec.evidence.get("irregular_ok", 0) or 0) for rec in siblings)
    return policy.family_prior_strength(len(siblings), irregular_handled)


def prior_for_record(profile, record) -> str:
    """Convenience: the prior for an existing capability record."""
    return compute_prior(profile, record.context_id, record.qualifier, record.capability)
