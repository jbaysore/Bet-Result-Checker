"""Seed-parity gate (plan Phase 1, "the gate test").

Grandfathering must reproduce today's behavior EXACTLY: every bet row that
already finalized `VERIFIED_CLOSE` must still be trusted by `require()` over the
seeded profile. A single downgrade here means enforcement (Phase 2) would
silently drop a real row from pooled CLV — the exact failure the whole design
exists to prevent.

This is the offline equivalent of the plan's one-week shadow observation: run
the enforcement decision over a snapshot of real Bets rows and require zero
would-have-changed decisions on already-supported contexts. The fixture holds
only non-sensitive routing columns (sport/book/market/quality) — no BetIDs,
teams, stakes, or outcomes.

Regenerate the fixture after a real behavioral change (not just to make a red
test green — a red test here is a real seed gap):
    py - <<'PY'
    ... dump Bets sport/book/bet_type/market_key/closing_quality ...
    PY
"""

import json
from pathlib import Path

import onboarding_policy as policy
from capability_profile import CapabilityProfile
from context_registry import ContextRegistry
from scripts.onboarding_inventory import build_seed_rows
from scripts.seed_capabilities import build_registry_rows

FIXTURE = Path(__file__).parent / "fixtures" / "onboarding_bets_parity.json"


def _load_fixture() -> list[dict]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _profile_and_registry():
    seeds = build_seed_rows()
    return CapabilityProfile(seeds), ContextRegistry(build_registry_rows(seeds))


def _decision(profile, registry, row):
    res = registry.resolve(row["sport"])
    family = policy.market_family_for(row.get("market_key"), row.get("bet_type"))
    if not res.is_known:
        return False, f"context NEW ({res.reason})"
    decision = profile.require_clv(res.context_id, row.get("book", ""), family)
    return decision.trusted, decision.reason


def test_fixture_present_and_nonempty():
    rows = _load_fixture()
    assert rows, "parity fixture is empty — regenerate from the live Bets tab"
    assert any(r["closing_quality"] == "VERIFIED_CLOSE" for r in rows)


def test_no_verified_close_row_is_downgraded():
    profile, registry = _profile_and_registry()
    downgraded = []
    for row in _load_fixture():
        if row["closing_quality"] != "VERIFIED_CLOSE":
            continue
        trusted, reason = _decision(profile, registry, row)
        if not trusted:
            downgraded.append((row["ref"], row["sport"], row.get("book"),
                               row.get("market_key") or row.get("bet_type"), reason))
    assert not downgraded, (
        f"{len(downgraded)} VERIFIED_CLOSE rows would be downgraded by require(): "
        + "; ".join(f"ref{r} {s}/{b}/{m} [{why}]" for r, s, b, m, why in downgraded[:15])
    )


def test_unsupported_contexts_are_not_falsely_blessed():
    """Rows that are NOT VERIFIED_CLOSE for a capability reason (no start source:
    boxing, unrouted one-offs) must resolve provisional — require() must never
    upgrade an unsupported capability (fail-open guard)."""
    profile, registry = _profile_and_registry()
    for row in _load_fixture():
        # Boxing has no live-flip trust and is deliberately excluded from combat
        # actual-start routes, so its start capability cannot authorize trust.
        if row["sport"] == "boxing_boxing":
            trusted, _ = _decision(profile, registry, row)
            assert not trusted, f"boxing row ref{row['ref']} was falsely blessed"
