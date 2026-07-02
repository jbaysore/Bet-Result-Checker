"""Duplicate BetID detection: flag rows and exclude from automated settlement."""

import pytest

import sheets_reader
import sheets_writer
from config import RESULT_WIN


_HEADERS = [
    "BetID", "Sport", "Book", "Team 1", "Team 2", "Game Date", "Game Start Time",
    "Selection", "Bet Type", "OddsTaken", "Stake", "Fee", "Bet Category",
    "Promo ID", "Result", "Payout", "P/L", "ClosingOdds", "Legs",
]


def _row(bet_id, result="", closing_odds=""):
    return [bet_id, "baseball_mlb", "fanduel", "Yankees", "Red Sox",
            "2026-06-30", "19:05", "Yankees", "Moneyline", "-150",
            "10", "0", "Standard", "", result, "", "", closing_odds, ""]


def test_find_duplicate_bet_id_rows_returns_all_matching_rows(monkeypatch):
    rows = [
        _HEADERS,
        _row("7"),
        _row("7"),
        _row("8"),
    ]
    monkeypatch.setattr(sheets_reader, "_get_bets_rows", lambda tab: rows)

    dupes = sheets_reader.find_duplicate_bet_id_rows("Bets")
    assert len(dupes) == 2
    assert {d["bet_id"] for d in dupes} == {"7"}
    assert sorted(d["row_idx"] for d in dupes) == [2, 3]


def test_loaders_skip_duplicate_bet_ids(monkeypatch):
    rows = [
        _HEADERS,
        _row("7"),                         # pending duplicate
        _row("7"),                         # pending duplicate
        _row("9", RESULT_WIN),             # unresolved P/L duplicate pair
        _row("9", RESULT_WIN),
        _row("10", RESULT_WIN),            # manual payout pending duplicate
        _row("10", RESULT_WIN),
        _row("11", closing_odds=""),       # closing odds duplicate
        _row("11", closing_odds=""),
    ]
    rows[5][15] = "18"
    rows[6][15] = "18"
    monkeypatch.setattr(sheets_reader, "_get_bets_rows", lambda tab: rows)

    pending = sheets_reader.load_pending_bets("Bets")
    unresolved = sheets_reader.load_unresolved_pl_bets("Bets")
    manual = sheets_reader.load_manual_payout_pending_pl_bets("Bets")
    closing = sheets_reader.load_bets_needing_closing_odds("Bets")

    assert pending == []
    assert unresolved == []
    assert manual == []
    assert closing == []


class _FakeSheet:
    def __init__(self, row):
        self.row = row
        self.updates = []

    def update_cell(self, row_idx, col, value):
        self.updates.append((row_idx, col, value))


def test_flag_duplicate_bet_id_writes_notes(monkeypatch):
    col = {"notes": 18, "bet_id": 1}
    monkeypatch.setattr(sheets_writer, "_bets_col_letter_lookup", lambda: col)
    row = ["42"] + [""] * 16 + ["existing note"]
    sheet = _FakeSheet(row)
    monkeypatch.setattr(sheets_writer, "_get_sheet", lambda: sheet)
    monkeypatch.setattr(sheets_writer, "_read_bet_row", lambda s, idx: row)

    ok = sheets_writer.flag_duplicate_bet_id(2, "42", "also on row(s) 2, 5")
    assert ok is True
    assert len(sheet.updates) == 1
    _, notes_col, notes = sheet.updates[0]
    assert notes_col == 18
    assert "⚠ Duplicate BetID:" in notes
    assert "existing note" in notes
