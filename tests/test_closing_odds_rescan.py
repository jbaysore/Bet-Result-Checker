"""
Re-scan behavior for closing-odds failure codes: the automation retries rows
whose ClosingOdds holds an error code, but never re-touches a real value,
"VOID", or "N/A".
"""

import pytest

import sheets_reader
import sheets_writer
from config import (
    CLOSING_ODDS_BOOK_NOT_FOUND, CLOSING_ODDS_GAME_NOT_FOUND,
)


# ── Loader: which rows get re-scanned ──────────────────────────────────────────

_HEADERS = [
    "BetID", "Sport", "Book", "Team 1", "Team 2", "Game Date", "Game Start Time",
    "Selection", "Bet Type", "OddsTaken", "ClosingOdds", "Result", "Legs",
]


def _row(bet_id, closing_odds, result=""):
    return [bet_id, "baseball_mlb", "fanduel", "Yankees", "Red Sox",
            "2026-06-30", "19:05", "Yankees", "Moneyline", "-150",
            closing_odds, result, ""]


def test_loader_rescans_error_codes_only(monkeypatch):
    rows = [
        _HEADERS,
        _row("1", ""),                            # blank -> include
        _row("2", CLOSING_ODDS_BOOK_NOT_FOUND),   # error code -> include (re-scan)
        _row("3", CLOSING_ODDS_GAME_NOT_FOUND),   # error code -> include (re-scan)
        _row("4", "-108"),                        # real value -> skip
        _row("5", "N/A"),                         # dismissed -> skip
        _row("6", "VOID"),                        # voided -> skip
    ]
    monkeypatch.setattr(sheets_reader, "_get_bets_rows", lambda tab: rows)

    bets = sheets_reader.load_bets_needing_closing_odds("Bets")
    ids = {b["bet_id"] for b in bets}
    assert ids == {"1", "2", "3"}


def test_loader_repairs_blank_provenance_when_no_live_capture_owns_it(monkeypatch):
    headers = [*_HEADERS, "Start Status", "Closing Quality"]
    blank = [*_row("20", ""), "", ""]
    legacy = [*_row("21", ""), "LEGACY_UNAUDITED", ""]
    monkeypatch.setattr(sheets_reader, "_get_bets_rows", lambda tab: [headers, blank, legacy])
    monkeypatch.setattr(sheets_reader, "_active_closing_capture_bet_ids", lambda: set())
    assert [bet["bet_id"] for bet in sheets_reader.load_bets_needing_closing_odds("Bets")] == ["20", "21"]


def test_loader_keeps_blank_provenance_fail_closed_while_capture_is_active(monkeypatch):
    headers = [*_HEADERS, "Start Status", "Closing Quality"]
    blank = [*_row("20", ""), "", ""]
    monkeypatch.setattr(sheets_reader, "_get_bets_rows", lambda tab: [headers, blank])
    monkeypatch.setattr(sheets_reader, "_active_closing_capture_bet_ids", lambda: {"20"})
    assert sheets_reader.load_bets_needing_closing_odds("Bets") == []


def test_loader_keeps_blank_provenance_fail_closed_when_queue_read_fails(monkeypatch):
    headers = [*_HEADERS, "Start Status", "Closing Quality"]
    blank = [*_row("20", ""), "", ""]
    monkeypatch.setattr(sheets_reader, "_get_bets_rows", lambda tab: [headers, blank])
    monkeypatch.setattr(sheets_reader, "_active_closing_capture_bet_ids", lambda: None)
    assert sheets_reader.load_bets_needing_closing_odds("Bets") == []


# ── Writer: overwrite rules ────────────────────────────────────────────────────

class _FakeSheet:
    def __init__(self):
        self.updates = []

    def update_cells(self, cells):
        self.updates.append(cells)


def _patch_writer(monkeypatch, current_closing):
    """Make write_closing_odds see a single row whose BetID is '42'."""
    col = {
        "closing_odds": 11, "decimal_closing": 12, "clv": 13, "bet_id": 1,
        "start_status": 14, "closing_quality": 15, "closing_source": 16,
        "closing_observed_at": 17, "start_detected_at": 18, "actual_start": 19,
        "actual_start_source": 20, "actual_start_confidence": 21,
    }
    monkeypatch.setattr(sheets_writer, "_bets_col_letter_lookup", lambda: col)
    sheet = _FakeSheet()
    monkeypatch.setattr(sheets_writer, "_get_sheet", lambda: sheet)
    # Row: BetID in col 1, ClosingOdds in col 11 (1-based).
    row = ["42"] + [""] * 9 + [current_closing] + ["", ""]
    monkeypatch.setattr(sheets_writer, "_read_bet_row", lambda s, idx: row)
    return sheet


_PROVENANCE = {
    "start_status": "UNKNOWN", "closing_quality": "PROVISIONAL",
    "closing_source": "historical", "closing_observed_at": "",
    "start_detected_at": "", "actual_start": "", "actual_start_source": "",
    "actual_start_confidence": "UNRESOLVED",
}


def test_writer_overwrites_error_code_with_real_value(monkeypatch):
    sheet = _patch_writer(monkeypatch, CLOSING_ODDS_BOOK_NOT_FOUND)
    ok = sheets_writer.write_closing_odds(2, "42", "-110", 1.909, 0.02, provenance=_PROVENANCE)
    assert ok is True
    assert len(sheet.updates) == 1  # actually wrote


def test_proactive_writer_refuses_to_overwrite_error_code(monkeypatch):
    sheet = _patch_writer(monkeypatch, CLOSING_ODDS_BOOK_NOT_FOUND)
    ok = sheets_writer.write_closing_odds(
        2, "42", "-110", 1.909, 0.02, overwrite_errors=False, provenance=_PROVENANCE,
    )
    assert ok is False
    assert sheet.updates == []


def test_writer_refuses_to_overwrite_real_value(monkeypatch):
    sheet = _patch_writer(monkeypatch, "-110")
    ok = sheets_writer.write_closing_odds(2, "42", "-120", 1.83, 0.01, provenance=_PROVENANCE)
    assert ok is False
    assert sheet.updates == []  # nothing written


def test_writer_refuses_to_overwrite_na(monkeypatch):
    sheet = _patch_writer(monkeypatch, "N/A")
    ok = sheets_writer.write_closing_odds(2, "42", "-110", 1.909, 0.02, provenance=_PROVENANCE)
    assert ok is False
    assert sheet.updates == []


def test_writer_noops_on_identical_error_code(monkeypatch):
    sheet = _patch_writer(monkeypatch, CLOSING_ODDS_BOOK_NOT_FOUND)
    ok = sheets_writer.write_closing_odds(2, "42", CLOSING_ODDS_BOOK_NOT_FOUND, None, None, provenance=_PROVENANCE)
    assert ok is False           # no-op signalled
    assert sheet.updates == []   # cell not churned


def test_writer_updates_error_code_to_different_code(monkeypatch):
    sheet = _patch_writer(monkeypatch, CLOSING_ODDS_BOOK_NOT_FOUND)
    ok = sheets_writer.write_closing_odds(2, "42", CLOSING_ODDS_GAME_NOT_FOUND, None, None, provenance=_PROVENANCE)
    assert ok is True
    assert len(sheet.updates) == 1


def test_writer_refuses_missing_provenance_payload(monkeypatch):
    sheet = _patch_writer(monkeypatch, "")
    assert sheets_writer.write_closing_odds(2, "42", "-110", 1.909, 0.02) is False
    assert sheet.updates == []
