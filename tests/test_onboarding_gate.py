"""Tests for onboarding_gate — the hot-path enforcement façade (plan Phase 2).

The gate logic lives here (the worker just calls `gate_finalize_quality`), so
these tests cover the plan's worker requirements directly: unverified grain ⇒
PROVISIONAL under enforce even with perfect timing; verified grain ⇒ untouched;
profile read failure ⇒ PROVISIONAL, no crash (fail closed); shadow mode logs but
changes nothing.
"""

import pytest

import config
import onboarding_gate as gate
import onboarding_policy as policy
from capability_profile import CapabilityProfile, record_key
from closing_provenance import QUALITY_EARLY, QUALITY_PROVISIONAL, QUALITY_VERIFIED
from context_registry import ContextRegistry


def cap_row(context_id, capability, qualifier, classification=policy.VERIFIED, health=policy.FRESH):
    return {
        "Record Key": record_key(context_id, capability, qualifier),
        "Context ID": context_id, "Capability": capability, "Qualifier": qualifier,
        "Classification": classification, "Health": health, "Activity": policy.IDLE,
        "Policy Version": "1", "Evidence Summary": "", "First Seen": "",
        "Last Verified": "", "Last Checked": "", "Constraints": "", "Notes": "",
    }


def mlb_profile(**kw):
    return CapabilityProfile([
        cap_row("baseball/mlb", policy.CAP_IDENTITY, "toa", **kw),
        cap_row("baseball/mlb", policy.CAP_START_LIVE, "toa_scores", **kw),
        cap_row("baseball/mlb", policy.CAP_CAPTURE, "any|featured", **kw),
    ])


def mlb_registry():
    return ContextRegistry([{
        "Context ID": "baseball/mlb", "Alias Type": "sport_key",
        "Alias Value": "baseball_mlb", "Status": "active", "Mapping Version": "1",
        "Edition Start": "", "Edition End": "", "Notes": "",
    }])


def mlb_record():
    return {"BetID": "B1", "Sport": "baseball_mlb", "Book": "draftkings",
            "Bet Type": "Spread", "Market Key": "spreads", "Team 1": "A", "Team 2": "B",
            "Commence UTC": "2026-07-16T20:00:00Z"}


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    gate.reset_caches()
    # Keep shadow-log writes out of the repo tree during tests.
    monkeypatch.setattr(gate, "_SHADOW_LOG_PATH", str(tmp_path / "onboarding.jsonl"), raising=False)
    monkeypatch.setattr(config, "ONBOARDING_SHADOW_MODE", True, raising=False)
    monkeypatch.setattr(config, "ONBOARDING_ENFORCE", False, raising=False)
    yield
    gate.reset_caches()


# ── gate_finalize_quality ────────────────────────────────────────────────────
def test_non_verified_quality_passes_through():
    gate.set_caches(mlb_profile(), mlb_registry())
    q, marker = gate.gate_finalize_quality(mlb_record(), QUALITY_EARLY)
    assert (q, marker) == (QUALITY_EARLY, None)


def test_trusted_grain_keeps_verified_close():
    gate.set_caches(mlb_profile(), mlb_registry())
    q, marker = gate.gate_finalize_quality(mlb_record(), QUALITY_VERIFIED)
    assert q == QUALITY_VERIFIED
    assert marker is None


def test_unverified_grain_shadow_only_does_not_change(monkeypatch):
    # start_live missing → not trusted; shadow mode (enforce off) logs but keeps VERIFIED.
    profile = CapabilityProfile([
        cap_row("baseball/mlb", policy.CAP_IDENTITY, "toa"),
        cap_row("baseball/mlb", policy.CAP_CAPTURE, "any|featured"),
    ])
    gate.set_caches(profile, mlb_registry())
    q, marker = gate.gate_finalize_quality(mlb_record(), QUALITY_VERIFIED)
    assert q == QUALITY_VERIFIED  # shadow: unchanged
    assert marker is None


def test_unverified_grain_enforced_downgrades_to_provisional(monkeypatch):
    monkeypatch.setattr(config, "ONBOARDING_ENFORCE", True, raising=False)
    profile = CapabilityProfile([  # no start_live → not trusted even with perfect timing
        cap_row("baseball/mlb", policy.CAP_IDENTITY, "toa"),
        cap_row("baseball/mlb", policy.CAP_CAPTURE, "any|featured"),
    ])
    gate.set_caches(profile, mlb_registry())
    q, marker = gate.gate_finalize_quality(mlb_record(), QUALITY_VERIFIED)
    assert q == QUALITY_PROVISIONAL
    assert marker.startswith("onboarding:")
    assert "start" in marker


def test_profile_unreadable_fails_closed_under_enforce(monkeypatch):
    monkeypatch.setattr(config, "ONBOARDING_ENFORCE", True, raising=False)
    gate.set_caches(CapabilityProfile(None), mlb_registry())  # tab unreadable
    q, marker = gate.gate_finalize_quality(mlb_record(), QUALITY_VERIFIED)
    assert q == QUALITY_PROVISIONAL
    assert marker.startswith("onboarding:")


def test_new_context_enforced_downgrades(monkeypatch):
    monkeypatch.setattr(config, "ONBOARDING_ENFORCE", True, raising=False)
    gate.set_caches(mlb_profile(), ContextRegistry([]))  # empty registry → NEW
    rec = dict(mlb_record(), Sport="cricket_ipl")
    q, marker = gate.gate_finalize_quality(rec, QUALITY_VERIFIED)
    assert q == QUALITY_PROVISIONAL
    assert "NEW context" in marker


def test_flags_off_short_circuits(monkeypatch):
    monkeypatch.setattr(config, "ONBOARDING_SHADOW_MODE", False, raising=False)
    monkeypatch.setattr(config, "ONBOARDING_ENFORCE", False, raising=False)
    # No caches needed — the gate must not even evaluate.
    q, marker = gate.gate_finalize_quality(mlb_record(), QUALITY_VERIFIED)
    assert (q, marker) == (QUALITY_VERIFIED, None)


# ── discover_for_bet ─────────────────────────────────────────────────────────
def test_discover_trusted_bet_is_noop(monkeypatch):
    monkeypatch.setattr(config, "ONBOARDING_ENFORCE", True, raising=False)
    gate.set_caches(mlb_profile(), mlb_registry())
    assert gate.discover_for_bet(mlb_record())["action"] == "trusted"


def test_discover_known_gap_creates_discovered_records(monkeypatch):
    monkeypatch.setattr(config, "ONBOARDING_ENFORCE", True, raising=False)
    profile = CapabilityProfile([  # missing start_live grain
        cap_row("baseball/mlb", policy.CAP_IDENTITY, "toa"),
        cap_row("baseball/mlb", policy.CAP_CAPTURE, "any|featured"),
    ])
    gate.set_caches(profile, mlb_registry())
    summary = gate.discover_for_bet(mlb_record())
    assert summary["action"] == "known_gap"
    key = record_key("baseball/mlb", policy.CAP_START_LIVE, "toa_scores")
    rec = profile.get_record(key)
    assert rec is not None and rec.classification == policy.DISCOVERED


def test_discover_new_context_mints_records(monkeypatch):
    monkeypatch.setattr(config, "ONBOARDING_ENFORCE", True, raising=False)
    appended = {}
    monkeypatch.setattr(gate, "_append_registry_alias",
                        lambda cid, sk: appended.update({"cid": cid, "sk": sk}))
    gate.set_caches(CapabilityProfile([]), ContextRegistry([]))
    summary = gate.discover_for_bet(dict(mlb_record(), Sport="cricket_ipl"))
    assert summary["action"] == "new_context"
    assert appended["sk"] == "cricket_ipl"
    assert summary["created"]  # created at least the identity/start/capture grains


def test_discover_shadow_mode_does_not_write(monkeypatch):
    # Shadow only (enforce off): must not create any record, only log intent.
    profile = CapabilityProfile([
        cap_row("baseball/mlb", policy.CAP_IDENTITY, "toa"),
        cap_row("baseball/mlb", policy.CAP_CAPTURE, "any|featured"),
    ])
    gate.set_caches(profile, mlb_registry())
    summary = gate.discover_for_bet(mlb_record())
    assert summary["action"] == "would_discover"
    key = record_key("baseball/mlb", policy.CAP_START_LIVE, "toa_scores")
    assert profile.get_record(key) is None  # nothing written in shadow mode


def test_discover_is_idempotent(monkeypatch):
    monkeypatch.setattr(config, "ONBOARDING_ENFORCE", True, raising=False)
    profile = CapabilityProfile([
        cap_row("baseball/mlb", policy.CAP_IDENTITY, "toa"),
        cap_row("baseball/mlb", policy.CAP_CAPTURE, "any|featured"),
    ])
    gate.set_caches(profile, mlb_registry())
    first = gate.discover_for_bet(mlb_record())
    second = gate.discover_for_bet(mlb_record())
    assert first["created"]        # created the missing start_live grain
    assert not second["created"]   # second pass finds it present → creates nothing


def test_discover_never_raises(monkeypatch):
    monkeypatch.setattr(config, "ONBOARDING_ENFORCE", True, raising=False)
    # A registry that raises on resolve must not propagate out of discovery.
    class Boom:
        readable = True
        def resolve(self, *a, **k):
            raise RuntimeError("boom")
    gate.set_caches(mlb_profile(), Boom())
    assert gate.discover_for_bet(mlb_record())["action"] == "error"
