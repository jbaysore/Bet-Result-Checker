import pytest

from scripts.repull_suspect_closing import (
    classify_repull_row,
    parse_sheet_number,
    restore_provenance,
)


def _legacy_bet(**overrides):
    bet = {
        "start_status": "LEGACY_UNAUDITED",
        "start_audit": "LIKELY_SUSPECT",
        "result": "WIN",
        "closing_odds": "-115",
        "is_parlay": False,
        "legs": [],
        "actual_start": "2026-07-10T00:12:41Z",
        "actual_start_confidence": "CONFIDENT",
    }
    bet.update(overrides)
    return bet


def test_confident_single_in_bucket_is_retryable():
    assert classify_repull_row(_legacy_bet())["bucket"] == "retry"
    assert classify_repull_row(_legacy_bet(start_audit="INDETERMINATE"))["bucket"] == "retry"


def test_non_legacy_and_wrong_bucket_rows_are_skipped():
    assert classify_repull_row(_legacy_bet(
        start_status="VERIFIED", closing_quality="VERIFIED_CLOSE",
    ))["bucket"] == "skip"
    assert classify_repull_row(_legacy_bet(start_audit="SAFE"))["bucket"] == "skip"
    assert classify_repull_row(_legacy_bet(start_audit="UNRESOLVABLE"))["bucket"] == "skip"


def test_verified_stale_row_with_confident_actual_start_is_retryable():
    result = classify_repull_row(_legacy_bet(
        start_status="VERIFIED", closing_quality="STALE",
    ))
    assert result["bucket"] == "retry"
    assert "STALE" in result["reason"]


def test_void_rows_have_no_clv_to_repair():
    assert classify_repull_row(_legacy_bet(result="VOID"))["bucket"] == "skip"
    assert classify_repull_row(_legacy_bet(closing_odds="VOID"))["bucket"] == "skip"


def test_single_without_confident_actual_start_needs_manual_review():
    assert classify_repull_row(_legacy_bet(actual_start=""))["bucket"] == "manual"
    assert classify_repull_row(_legacy_bet(actual_start_confidence="UNRESOLVED"))["bucket"] == "manual"


def test_parlay_gates_on_parseable_legs_not_row_confidence():
    parlay = _legacy_bet(is_parlay=True, legs=[{"sport": "baseball_mlb"}],
                         actual_start="", actual_start_confidence="")
    assert classify_repull_row(parlay)["bucket"] == "retry"
    assert classify_repull_row(_legacy_bet(is_parlay=True, legs=[]))["bucket"] == "manual"


def test_parse_sheet_number_handles_percent_and_blank():
    assert parse_sheet_number("5.43%") == pytest.approx(0.0543)
    assert parse_sheet_number("1.909") == 1.909
    assert parse_sheet_number("-2.5%") == pytest.approx(-0.025)
    assert parse_sheet_number("") is None
    assert parse_sheet_number("N/A") is None


def test_restore_provenance_preserves_legacy_status_and_audit_stamp():
    provenance = restore_provenance(_legacy_bet(
        actual_start_source="mlb-statsapi-firstPitch", closing_quality="", closing_source="",
    ))
    assert provenance["start_status"] == "LEGACY_UNAUDITED"
    assert provenance["actual_start"] == "2026-07-10T00:12:41Z"
    assert provenance["actual_start_source"] == "mlb-statsapi-firstPitch"
    assert "start_audit" not in provenance  # audit stamp is never rewritten


def test_restore_provenance_preserves_verified_stale_capture_fields():
    provenance = restore_provenance(_legacy_bet(
        start_status="VERIFIED", closing_quality="STALE",
        closing_source="historical", closing_observed_at="2026-07-11T18:35:00Z",
        start_detected_at="2026-07-11T18:38:10Z",
    ))
    assert provenance["start_status"] == "VERIFIED"
    assert provenance["closing_quality"] == "STALE"
    assert provenance["closing_source"] == "historical"
    assert provenance["closing_observed_at"] == "2026-07-11T18:35:00Z"
    assert provenance["start_detected_at"] == "2026-07-11T18:38:10Z"
