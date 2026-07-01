"""
Re-scan behavior for NEEDS_REVIEW results: the poller retries NEEDS_REVIEW
rows, but write_result never overwrites a real WIN/LOSS/PUSH/VOID or a manual
PENDING.
"""

import pytest

import sheets_reader
import sheets_writer
from config import RESULT_NEEDS_REVIEW, RESULT_WIN, RESULT_PENDING


# ── Loader: which rows get re-polled ───────────────────────────────────────────

_HEADERS = [
    "BetID", "Sport", "Book", "Team 1", "Team 2", "Game Date", "Game Start Time",
    "Selection", "Bet Type", "OddsTaken", "Stake", "Fee", "Bet Category",
    "Promo ID", "Result", "Legs",
]


def _row(bet_id, result):
    return [bet_id, "baseball_mlb", "fanduel", "Yankees", "Red Sox",
            "2026-06-30", "19:05", "Yankees", "Moneyline", "-150",
            "10", "0", "Standard", "", result, ""]


def test_loader_repolls_needs_review_only(monkeypatch):
    rows = [
        _HEADERS,
        _row("1", ""),                    # blank -> pending, include
        _row("2", RESULT_NEEDS_REVIEW),   # needs review -> re-poll, include
        _row("3", RESULT_WIN),            # settled -> skip
        _row("4", RESULT_PENDING),        # manual -> skip
    ]
    monkeypatch.setattr(sheets_reader, "_get_bets_rows", lambda tab: rows)

    bets = sheets_reader.load_pending_bets("Bets")
    assert {b["bet_id"] for b in bets} == {"1", "2"}


# ── Writer: overwrite rules ────────────────────────────────────────────────────

class _FakeSheet:
    def __init__(self):
        self.updates = []

    def update_cells(self, cells):
        self.updates.append(cells)


def _patch_writer(monkeypatch, current_result):
    col = {"result": 12, "payout": 13, "pl": 14, "fee": 15, "bet_id": 1}
    monkeypatch.setattr(sheets_writer, "_bets_col_letter_lookup", lambda: col)
    sheet = _FakeSheet()
    monkeypatch.setattr(sheets_writer, "_get_sheet", lambda: sheet)
    # Row: BetID col 1, Result col 12 (1-based).
    row = ["42"] + [""] * 10 + [current_result] + ["", "", ""]
    monkeypatch.setattr(sheets_writer, "_read_bet_row", lambda s, idx: row)
    return sheet


def test_writer_overwrites_needs_review_with_real_result(monkeypatch):
    sheet = _patch_writer(monkeypatch, RESULT_NEEDS_REVIEW)
    ok = sheets_writer.write_result(2, RESULT_WIN, "42", pl=8.0, payout=18.0)
    assert ok is True
    assert len(sheet.updates) == 1


def test_writer_refuses_to_overwrite_real_result(monkeypatch):
    sheet = _patch_writer(monkeypatch, RESULT_WIN)
    ok = sheets_writer.write_result(2, "LOSS", "42", pl=-10.0)
    assert ok is False
    assert sheet.updates == []


def test_writer_refuses_to_overwrite_pending(monkeypatch):
    sheet = _patch_writer(monkeypatch, RESULT_PENDING)
    ok = sheets_writer.write_result(2, RESULT_WIN, "42", pl=8.0, payout=18.0)
    assert ok is False
    assert sheet.updates == []


def test_writer_noops_on_repeated_needs_review(monkeypatch):
    sheet = _patch_writer(monkeypatch, RESULT_NEEDS_REVIEW)
    ok = sheets_writer.write_result(2, RESULT_NEEDS_REVIEW, "42")
    assert ok is False           # no-op signalled
    assert sheet.updates == []   # cell not churned


def test_writer_writes_into_blank(monkeypatch):
    sheet = _patch_writer(monkeypatch, "")
    ok = sheets_writer.write_result(2, RESULT_WIN, "42", pl=8.0, payout=18.0)
    assert ok is True
    assert len(sheet.updates) == 1
