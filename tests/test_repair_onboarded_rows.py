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
    monkeypatch.setattr(sheets_writer, "repair_onboarded_close",
                        lambda *a, **k: calls.append("repair") or True)
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
    assert calls == ["repair"]                              # one guarded atomic write


def test_gate_attested_close_repairs_in_place_without_api_credits(monkeypatch):
    captured = {}

    def write(*args, **kwargs):
        captured["args"] = args
        captured["provenance"] = kwargs["provenance"]
        return True

    monkeypatch.setattr(sheets_writer, "repair_onboarded_close", write)
    monkeypatch.setattr(closing_odds, "fetch_closing_odds",
                        lambda _: (_ for _ in ()).throw(AssertionError("must not fetch")))
    out = repair.repair_bet(bet(
        decimal_closing="1.91", clv="2.5%",
        closing_observed_at="2026-07-27T01:00:00Z",
        start_detected_at="2026-07-27T01:05:00Z"))
    assert out["status"] == "repaired"
    assert "zero API credits" in out["detail"]
    assert captured["args"][2:5] == (-110.0, 1.91, 0.025)
    assert captured["provenance"]["closing_observed_at"] == "2026-07-27T01:00:00Z"
    assert captured["provenance"]["start_detected_at"] == "2026-07-27T01:05:00Z"


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


def test_repair_refused_leaves_original_untouched(monkeypatch):
    calls = []
    _patch_writes(monkeypatch, calls)
    # Atomic repair refuses (e.g. row changed) → no preliminary clear occurred.
    monkeypatch.setattr(sheets_writer, "repair_onboarded_close", lambda *a, **k: False)
    monkeypatch.setattr(closing_odds, "fetch_closing_odds",
                        lambda b: {"closing_odds": "-108", "decimal_closing": 1.93,
                                   "clv": 0.03, "closing_quality": "VERIFIED_CLOSE"})
    out = repair.repair_bet(bet())
    assert out["status"] == "failed"
    assert "untouched" in out["detail"]
    assert calls == []


def test_scheduled_repair_is_bounded_and_keeps_failures_queued(monkeypatch):
    rows = [bet(bet_id=str(i)) for i in range(12)]
    monkeypatch.setattr(repair, "load_onboarding_rows", lambda: rows)
    monkeypatch.setattr(repair, "_is_trusted", lambda *args: True)

    def outcome(row):
        status = "failed" if row["bet_id"] == "2" else "repaired"
        return {"bet_id": row["bet_id"], "status": status, "detail": status}

    monkeypatch.setattr(repair, "repair_bet", outcome)
    summary = repair.repair_ready_rows(
        object(), object(), apply=True, max_rows=5, max_refetch_rows=5)
    assert summary["ready"] == 12
    assert summary["attempted"] == 5
    assert summary["repaired"] == 4
    assert summary["failed"] == 1
    assert summary["remaining"] == 8  # 7 unattempted + the retryable failure


def test_scheduled_repair_prioritizes_zero_credit_and_caps_refetches(monkeypatch):
    fast = [bet(bet_id=str(i), decimal_closing="1.91", clv="2.5%") for i in range(3)]
    slow = [bet(bet_id=str(i + 3)) for i in range(4)]
    monkeypatch.setattr(repair, "load_onboarding_rows", lambda: slow + fast)
    monkeypatch.setattr(repair, "_is_trusted", lambda *args: True)
    attempted = []
    monkeypatch.setattr(repair, "repair_bet", lambda row: (
        attempted.append(row["bet_id"]) or
        {"bet_id": row["bet_id"], "status": "repaired", "detail": "ok"}))
    summary = repair.repair_ready_rows(
        object(), object(), apply=True, max_rows=5, max_refetch_rows=1)
    assert attempted == ["0", "1", "2", "3"]
    assert summary["zero_credit_ready"] == 3
    assert summary["refetch_ready"] == 4
    assert summary["refetch_attempted"] == 1


def test_scheduled_repair_preview_never_fetches(monkeypatch):
    monkeypatch.setattr(repair, "load_onboarding_rows", lambda: [bet()])
    monkeypatch.setattr(repair, "_is_trusted", lambda *args: True)
    monkeypatch.setattr(repair, "repair_bet",
                        lambda row: (_ for _ in ()).throw(AssertionError("must not fetch")))
    summary = repair.repair_ready_rows(object(), object(), apply=False, max_rows=10)
    assert summary["ready"] == 1
    assert summary["attempted"] == 0
    assert summary["remaining"] == 1
