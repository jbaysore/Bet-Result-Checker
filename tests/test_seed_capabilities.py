"""Unit tests for the pure planning logic of scripts/seed_capabilities.py.

The live-sheet write path is exercised only under --apply; here we test the
idempotency + non-seed protection that guards it.
"""

import onboarding_policy as policy
from capability_profile import record_key
from scripts.seed_capabilities import (
    SEED_NOTE_PREFIX, build_registry_rows, plan_capability_writes, plan_registry_writes,
)


def seed(context_id, capability, qualifier, alias):
    return {
        "Record Key": record_key(context_id, capability, qualifier),
        "Context ID": context_id, "Capability": capability, "Qualifier": qualifier,
        "Classification": policy.VERIFIED, "Health": policy.FRESH, "Activity": policy.IDLE,
        "Policy Version": 1, "Evidence Summary": "", "First Seen": "", "Last Verified": "",
        "Last Checked": "", "Constraints": "", "Notes": f"seeded: table; alias={alias}",
    }


def test_build_registry_rows_dedups_by_context_and_alias():
    rows = build_registry_rows([
        seed("baseball/mlb", policy.CAP_IDENTITY, "toa", "baseball_mlb"),
        seed("baseball/mlb", policy.CAP_DISCOVERY, "toa", "baseball_mlb"),  # same alias
        seed("soccer/usa.mls", policy.CAP_IDENTITY, "toa", "soccer_usa_mls"),
    ])
    keys = {(r["Context ID"], r["Alias Value"]) for r in rows}
    assert keys == {("baseball/mlb", "baseball_mlb"), ("soccer/usa.mls", "soccer_usa_mls")}
    assert all(r["Status"] == "active" and r["Notes"] == SEED_NOTE_PREFIX for r in rows)


def test_plan_capability_writes_appends_new_only():
    seeds = [
        seed("baseball/mlb", policy.CAP_IDENTITY, "toa", "baseball_mlb"),
        seed("soccer/new", policy.CAP_IDENTITY, "toa", "soccer_new"),
    ]
    existing = [dict(seeds[0])]  # mlb already seeded
    plan = plan_capability_writes(existing, seeds)
    assert [r["Context ID"] for r in plan["append"]] == ["soccer/new"]
    assert plan["skip"] == [seeds[0]["Record Key"]]
    assert plan["refuse"] == []


def test_plan_capability_writes_refuses_to_overwrite_real_evidence():
    seeds = [seed("baseball/mlb", policy.CAP_START_LIVE, "toa_scores", "baseball_mlb")]
    real = dict(seeds[0])
    real["Notes"] = "2026-07-20 promoted Discovered→Verified (3 clean events)"  # earned, not seeded
    plan = plan_capability_writes([real], seeds)
    assert plan["append"] == []
    assert plan["refuse"] == [seeds[0]["Record Key"]]


def test_plan_registry_writes_skips_existing_aliases():
    registry = build_registry_rows([
        seed("baseball/mlb", policy.CAP_IDENTITY, "toa", "baseball_mlb"),
        seed("soccer/new", policy.CAP_IDENTITY, "toa", "soccer_new"),
    ])
    existing = [{"Context ID": "baseball/mlb", "Alias Value": "baseball_mlb"}]
    plan = plan_registry_writes(existing, registry)
    assert [r["Context ID"] for r in plan["append"]] == ["soccer/new"]
    assert plan["skip"] == 1
