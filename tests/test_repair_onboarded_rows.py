"""Repair pipeline (plan Phase 5): classification + the guarded repair flow.

The row is upgraded only when the grain is trusted AND the re-derivation yields
VERIFIED_CLOSE; anything else leaves the row exactly as it was.
"""

import closing_odds
import sheets_writer
from scripts import repair_onboarded_rows as repair


def bet(**kw):
    base = {"row_idx": 2, "bet_id": "1", "sport": "soccer_new", "book": "draftkings",
            "bet_type": "Moneyline", "market_key": "h2h", "result": "WIN",
            "notes": "onboarding: soccer/new|h2h start=Discovered",
            "closing_odds": "-110", "clv": "0.02", "closing_quality": "PROVISIONAL",
            "closing_source": "worker-live"}
    base.update(kw)
    return base


# ── classify_repair_row (pure) ───────────────────────────────────────────────
def test_trusted_promoted_grain_is_retry():
    assert repair.classify_repair_row(bet(), is_trusted=True)["bucket"] == "retry"


def test_untrusted_grain_is_skipped():
    cls = repair.classify_repair_row(bet(), is_trusted=False)
    assert cls["bucket"] == "skip"
    assert "not yet promoted" in cls["reason"]


def test_no_onboarding_marker_skipped():
    assert repair.classify_repair_row(bet(notes="just a note"), is_trusted=True)["bucket"] == "skip"


def test_already_verified_skipped():
    assert repair.classify_repair_row(
        bet(closing_quality="VERIFIED_CLOSE"), is_trusted=True)["bucket"] == "skip"


def test_already_repaired_is_never_touched_again():
    # Double-repair guard: recovery-onboarding source OR a pre-repair marker.
    assert repair.classify_repair_row(
        bet(closing_source="recovery-onboarding", closing_quality="VERIFIED_CLOSE"),
        is_trusted=True)["bucket"] == "skip"
    assert repair.classify_repair_row(
        bet(notes="onboarding: x\npre-repair: close=-110 clv=0.02 quality=PROVISIONAL"),
        is_trusted=True)["bucket"] == "skip"


def test_void_skipped():
    assert repair.classify_repair_row(bet(result="VOID"), is_trusted=True)["bucket"] == "skip"


# ── repair_bet orchestration (mocked — no live writes) ───────────────────────
def _patch_writes(monkeypatch, calls):
    monkeypatch.setattr(sheets_writer, "clear_closing_odds_cells",
                        lambda *a, **k: calls.append("clear") or True)
    monkeypatch.setattr(sheets_writer, "repair_onboarded_close",
                        lambda *a, **k: calls.append("repair") or True)
    monkeypatch.setattr(sheets_writer, "write_closing_odds",
                        lambda *a, **k: calls.append("write") or True)
    import scripts.retry_closing_odds as rc
    monkeypatch.setattr(rc, "_retry_provenance", lambda r: {})


def test_repair_applies_only_on_verified_close(monkeypatch):
    calls = []
    _patch_writes(monkeypatch, calls)
    monkeypatch.setattr(closing_odds, "fetch_closing_odds",
                        lambda b: {"closing_odds": "-108", "decimal_closing": 1.93,
                                   "clv": 0.03, "closing_quality": "VERIFIED_CLOSE"})
    out = repair.repair_bet(bet())
    assert out["status"] == "repaired"
    assert "clear" in calls and "repair" in calls          # cleared then wrote the repair
    assert calls.index("clear") < calls.index("repair")    # clear precedes write


def test_non_verified_derivation_leaves_row_untouched(monkeypatch):
    calls = []
    _patch_writes(monkeypatch, calls)
    monkeypatch.setattr(closing_odds, "fetch_closing_odds",
                        lambda b: {"closing_odds": "-110", "closing_quality": "SAFE_BUT_EARLY"})
    out = repair.repair_bet(bet())
    assert out["status"] == "still_provisional"
    assert calls == []                                     # nothing cleared or written


def test_fetch_failure_leaves_row_untouched(monkeypatch):
    calls = []
    _patch_writes(monkeypatch, calls)
    def boom(_):
        raise RuntimeError("api down")
    monkeypatch.setattr(closing_odds, "fetch_closing_odds", boom)
    out = repair.repair_bet(bet())
    assert out["status"] == "failed"
    assert calls == []


def test_repair_refused_restores_original(monkeypatch):
    calls = []
    _patch_writes(monkeypatch, calls)
    # repair_onboarded_close refuses (e.g. already repaired) → original restored.
    monkeypatch.setattr(sheets_writer, "repair_onboarded_close", lambda *a, **k: False)
    monkeypatch.setattr(closing_odds, "fetch_closing_odds",
                        lambda b: {"closing_odds": "-108", "decimal_closing": 1.93,
                                   "clv": 0.03, "closing_quality": "VERIFIED_CLOSE"})
    out = repair.repair_bet(bet())
    assert out["status"] == "failed"
    assert "restored" in out["detail"]
    assert "write" in calls                                # restore write happened
