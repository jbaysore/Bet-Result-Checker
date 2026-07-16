"""Cross-repo parity: the checker's trust decision must match the odds-tool's
capabilityStatus.js over a SHARED fixture (plan Phase 3, betReviewPl precedent).

`tests/fixtures/onboarding_parity.json` is byte-identical to
`odds-tool/tests/fixtures/onboarding_parity.json`; it is asserted here (pytest)
and by `tests/capabilityStatus.test.js` (node). Drift in either implementation —
or between the two JSON copies — fails a suite.

The reference decision (what both `statusForBet` and this test compute):
  resolve the sport key → context; a NEW context is provisional with all three
  CLV capabilities unresolved; a KNOWN context defers to require_clv.
"""

import json
from pathlib import Path

import onboarding_policy as policy
from capability_profile import CLV_START, CapabilityProfile, clv_requirements
from context_registry import ContextRegistry

FIXTURE = Path(__file__).parent / "fixtures" / "onboarding_parity.json"


def _rows_to_dicts(rows):
    headers = rows[0]
    return [dict(zip(headers, row)) for row in rows[1:]]


def _reference_decision(profile, registry, bet):
    """Mirror of capabilityStatus.statusForBet — the shared reference."""
    resolution = registry.resolve(bet["sportKey"], game_date=bet.get("gameDate"))
    if not resolution.is_known:
        return {"contextId": None, "provisional": True,
                "unresolved": [policy.CAP_IDENTITY, CLV_START, policy.CAP_CAPTURE]}
    family = policy.market_family_for(bet.get("marketKey"), bet.get("betType"))
    decision = profile.require_clv(resolution.context_id, bet.get("book", ""), family)
    return {"contextId": resolution.context_id, "provisional": decision.provisional,
            "unresolved": list(decision.unresolved)}


def test_shared_parity_fixture_matches_checker():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    profile = CapabilityProfile(_rows_to_dicts(fixture["capabilitiesRows"]))
    registry = ContextRegistry(_rows_to_dicts(fixture["registryRows"]))

    for case in fixture["cases"]:
        decision = _reference_decision(profile, registry, case["bet"])
        expected = case["expected"]
        assert decision["provisional"] == expected["provisional"], f"provisional: {case['name']}"
        if "contextId" in expected:
            assert decision["contextId"] == expected["contextId"], f"contextId: {case['name']}"
        assert sorted(decision["unresolved"]) == sorted(expected["unresolved"]), \
            f"unresolved: {case['name']}"


def test_clv_requirement_labels_match_fixture_vocabulary():
    # The fixture's unresolved labels must be exactly the require_clv labels.
    labels = {r.capability for r in clv_requirements("x", "y", "h2h")}
    assert labels == {policy.CAP_IDENTITY, CLV_START, policy.CAP_CAPTURE}
