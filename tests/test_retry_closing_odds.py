"""
Tests for retry_closing_odds: market key inference, row bucketing, and write paths.
"""

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from closing_odds_exhaustion import (
    is_closing_odds_exhausted,
    next_fail_streak_notes,
    parse_fail_streak,
)
from market_key_infer import infer_market_key
from retry_bucketing import classify_closing_retry_row
from config import (
    CLOSING_ODDS_SELECTION_NOT_FOUND,
    CLOSING_ODDS_EXHAUSTION_THRESHOLD,
)


# ── Market key inference ──────────────────────────────────────────────────────

@pytest.mark.parametrize("notes,bet_type,selection,expected", [
    ("", "Moneyline", "Yankees", "h2h"),
    ("", "Draw", "Draw", "h2h"),
    ("", "Spread", "Cubs -3.5", "spreads"),
    ("", "Total", "Over 8.5", "totals"),
    ("", "Total", "Braves Team Total Over 4.5", "team_totals"),
    ("Opportunity Scanner — Alt Spread @ draftkings", "Spread", "Cubs -7.5", "alternate_spreads"),
    ("logged Alt Total from scanner", "Total", "Over 51.5", "alternate_totals"),
    ("Team Total opportunity", "Total", "Over 4.5", "team_totals"),
    ("", "Prop", "Player Points Over 20.5", ""),
])
def test_infer_market_key(notes, bet_type, selection, expected):
    assert infer_market_key(notes, bet_type, selection) == expected


# ── Row bucketing ─────────────────────────────────────────────────────────────

def _past_game_bet(**overrides):
    base = {
        "bet_id": "1",
        "bet_type": "Spread",
        "selection": "Yankees -1.5",
        "book": "fanduel",
        "team1": "Yankees",
        "team2": "Red Sox",
        "game_date": "2020-06-01",
        "game_start": "19:05",
        "result": "WIN",
        "notes": "",
        "kalshi_ticker": "",
        "market_key": "",
        "is_parlay": False,
        "legs": [],
    }
    base.update(overrides)
    return base


def test_bucket_prop_is_skip():
    c = classify_closing_retry_row(_past_game_bet(bet_type="Prop", selection="Over 20.5"))
    assert c["bucket"] == "skip"
    assert "Prop" in c["reason"]


def test_bucket_fanatics_spread_is_retry():
    c = classify_closing_retry_row(_past_game_bet(book="fanatics"))
    assert c["bucket"] == "retry"


def test_bucket_kalshi_without_ticker_is_manual():
    c = classify_closing_retry_row(_past_game_bet(book="kalshi"))
    assert c["bucket"] == "manual"


def test_bucket_kalshi_with_ticker_is_retry():
    c = classify_closing_retry_row(_past_game_bet(book="kalshi", kalshi_ticker="KXNBAGAME-24"))
    assert c["bucket"] == "retry"


def test_bucket_needs_review_is_skip():
    c = classify_closing_retry_row(_past_game_bet(result="NEEDS_REVIEW"))
    assert c["bucket"] == "skip"


def test_bucket_future_game_is_skip():
    future = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")
    c = classify_closing_retry_row(_past_game_bet(game_date=future))
    assert c["bucket"] == "skip"
    assert "not started" in c["reason"]


# ── Exhaustion markers ────────────────────────────────────────────────────────

def test_fail_streak_increments_and_exhausts():
    notes, exhausted = next_fail_streak_notes("", CLOSING_ODDS_SELECTION_NOT_FOUND)
    assert parse_fail_streak(notes) == (1, CLOSING_ODDS_SELECTION_NOT_FOUND)
    assert not exhausted

    for i in range(2, CLOSING_ODDS_EXHAUSTION_THRESHOLD):
        notes, exhausted = next_fail_streak_notes(notes, CLOSING_ODDS_SELECTION_NOT_FOUND)
        assert parse_fail_streak(notes)[0] == i
        assert not exhausted

    notes, exhausted = next_fail_streak_notes(notes, CLOSING_ODDS_SELECTION_NOT_FOUND)
    assert exhausted
    assert is_closing_odds_exhausted(notes)


def test_fail_streak_resets_on_different_code():
    notes, _ = next_fail_streak_notes("", CLOSING_ODDS_SELECTION_NOT_FOUND)
    notes, _ = next_fail_streak_notes(notes, CLOSING_ODDS_SELECTION_NOT_FOUND)
    notes, _ = next_fail_streak_notes(notes, "BOOK NOT FOUND")
    assert parse_fail_streak(notes) == (1, "BOOK NOT FOUND")


# ── Loader: exhausted rows skipped ───────────────────────────────────────────

_HEADERS = [
    "BetID", "Sport", "Book", "Team 1", "Team 2", "Game Date", "Game Start Time",
    "Selection", "Bet Type", "OddsTaken", "ClosingOdds", "Result", "Legs", "Notes",
]


def _loader_row(bet_id, closing, notes=""):
    return [bet_id, "baseball_mlb", "fanduel", "Yankees", "Red Sox",
            "2020-06-01", "19:05", "Yankees", "Moneyline", "-150",
            closing, "WIN", "", notes]


def test_loader_skips_exhausted_error_rows(monkeypatch):
    import sheets_reader
    exhausted_notes = (
        f"[closing-odds-fail:{CLOSING_ODDS_EXHAUSTION_THRESHOLD}:"
        f"{CLOSING_ODDS_SELECTION_NOT_FOUND}]\n[closing-odds-exhausted]"
    )
    rows = [
        _HEADERS,
        _loader_row("1", ""),
        _loader_row("2", CLOSING_ODDS_SELECTION_NOT_FOUND, exhausted_notes),
    ]
    monkeypatch.setattr(sheets_reader, "_get_bets_rows", lambda tab: rows)
    bets = sheets_reader.load_bets_needing_closing_odds("Bets")
    assert {b["bet_id"] for b in bets} == {"1"}


# ── Dry-run does not call Sheets or API ───────────────────────────────────────

def test_dry_run_does_not_write(monkeypatch):
    import scripts.retry_closing_odds as retry_mod

    bet = _past_game_bet(bet_id="42", row_idx=2, closing_odds="N/A")
    monkeypatch.setattr(
        retry_mod,
        "load_bets_for_closing_retry",
        lambda *a, **k: [bet],
    )
    clear_mock = patch.object(retry_mod, "clear_closing_odds_cells")
    fetch_mock = patch.object(retry_mod, "fetch_closing_odds")

    with clear_mock as clear_m, fetch_mock as fetch_m:
        rc = retry_mod.main(["--all-na"])
        clear_m.assert_not_called()
        fetch_m.assert_not_called()
    assert rc == 0


def test_restore_na_on_failure_default(monkeypatch):
    import scripts.retry_closing_odds as retry_mod

    bet = _past_game_bet(bet_id="42", row_idx=2)
    classification = classify_closing_retry_row(bet)

    monkeypatch.setattr(retry_mod, "clear_closing_odds_cells", lambda *a: True)
    monkeypatch.setattr(retry_mod, "clear_market_key_cell", lambda *a: True)
    monkeypatch.setattr(
        retry_mod,
        "fetch_closing_odds",
        lambda b: {"closing_odds": None, "decimal_closing": None, "clv": None,
                   "error": CLOSING_ODDS_SELECTION_NOT_FOUND},
    )
    writes = []
    monkeypatch.setattr(
        retry_mod,
        "write_closing_odds",
        lambda *a, **k: writes.append(a) or True,
    )
    monkeypatch.setattr(retry_mod, "write_market_key_if_blank", lambda *a: True)
    monkeypatch.setattr(retry_mod, "clear_closing_odds_fail_streak", lambda *a: True)

    outcome = retry_mod._process_bet(
        bet, classification, backfill_market_key=False, leave_error=False,
    )
    assert outcome["status"] == "failed"
    assert writes == [(2, "42", "N/A", None, None)]


def test_leave_error_writes_code(monkeypatch):
    import scripts.retry_closing_odds as retry_mod

    bet = _past_game_bet(bet_id="42", row_idx=2)
    classification = classify_closing_retry_row(bet)

    monkeypatch.setattr(retry_mod, "clear_closing_odds_cells", lambda *a: True)
    monkeypatch.setattr(retry_mod, "clear_market_key_cell", lambda *a: True)
    monkeypatch.setattr(
        retry_mod,
        "fetch_closing_odds",
        lambda b: {"closing_odds": None, "decimal_closing": None, "clv": None,
                   "error": CLOSING_ODDS_SELECTION_NOT_FOUND},
    )
    writes = []
    monkeypatch.setattr(
        retry_mod,
        "write_closing_odds",
        lambda *a, **k: writes.append(a) or True,
    )
    monkeypatch.setattr(retry_mod, "write_market_key_if_blank", lambda *a: True)
    monkeypatch.setattr(retry_mod, "clear_closing_odds_fail_streak", lambda *a: True)

    retry_mod._process_bet(
        bet, classification, backfill_market_key=False, leave_error=True,
    )
    assert writes == [(2, "42", CLOSING_ODDS_SELECTION_NOT_FOUND, None, None)]


def test_transient_failure_restores_na(monkeypatch):
    import scripts.retry_closing_odds as retry_mod

    bet = _past_game_bet(bet_id="42", row_idx=2)
    classification = classify_closing_retry_row(bet)

    monkeypatch.setattr(retry_mod, "clear_closing_odds_cells", lambda *a: True)
    monkeypatch.setattr(retry_mod, "clear_market_key_cell", lambda *a: True)
    monkeypatch.setattr(
        retry_mod,
        "fetch_closing_odds",
        lambda b: {"closing_odds": None, "decimal_closing": None, "clv": None,
                   "error": None},
    )
    writes = []
    monkeypatch.setattr(
        retry_mod,
        "write_closing_odds",
        lambda *a, **k: writes.append(a) or True,
    )
    monkeypatch.setattr(retry_mod, "write_market_key_if_blank", lambda *a: True)
    monkeypatch.setattr(retry_mod, "clear_closing_odds_fail_streak", lambda *a: True)

    outcome = retry_mod._process_bet(
        bet, classification, backfill_market_key=False, leave_error=False,
    )
    assert outcome["status"] == "failed"
    assert writes == [(2, "42", "N/A", None, None)]


def test_fetch_cascades_without_sheet_market_key(monkeypatch):
    import scripts.retry_closing_odds as retry_mod

    bet = _past_game_bet(bet_id="42", row_idx=2, market_key="alternate_totals")
    classification = classify_closing_retry_row(bet)
    seen = {}

    monkeypatch.setattr(retry_mod, "clear_closing_odds_cells", lambda *a: True)
    monkeypatch.setattr(retry_mod, "clear_market_key_cell", lambda *a: True)

    def capture_fetch(b):
        seen["market_key"] = b.get("market_key")
        return {"closing_odds": None, "decimal_closing": None, "clv": None,
                "error": CLOSING_ODDS_SELECTION_NOT_FOUND}

    monkeypatch.setattr(retry_mod, "fetch_closing_odds", capture_fetch)
    monkeypatch.setattr(
        retry_mod,
        "write_closing_odds",
        lambda *a, **k: True,
    )
    monkeypatch.setattr(retry_mod, "write_market_key_if_blank", lambda *a: True)
    monkeypatch.setattr(retry_mod, "clear_closing_odds_fail_streak", lambda *a: True)

    retry_mod._process_bet(
        bet, classification, backfill_market_key=False, leave_error=False,
    )
    assert seen["market_key"] == ""


def test_backfill_market_key_only_after_success(monkeypatch):
    import scripts.retry_closing_odds as retry_mod

    bet = _past_game_bet(bet_id="42", row_idx=2)
    classification = {**classify_closing_retry_row(bet),
                      "inferred_market_key": "alternate_totals"}
    mk_writes = []

    monkeypatch.setattr(retry_mod, "clear_closing_odds_cells", lambda *a: True)
    monkeypatch.setattr(retry_mod, "clear_market_key_cell", lambda *a: True)
    monkeypatch.setattr(
        retry_mod,
        "fetch_closing_odds",
        lambda b: {"closing_odds": "-110", "decimal_closing": 1.909,
                   "clv": 0.02, "error": None},
    )
    monkeypatch.setattr(retry_mod, "write_closing_odds", lambda *a, **k: True)
    monkeypatch.setattr(
        retry_mod,
        "write_market_key_if_blank",
        lambda *a, **k: mk_writes.append(a) or True,
    )
    monkeypatch.setattr(retry_mod, "clear_closing_odds_fail_streak", lambda *a: True)

    outcome = retry_mod._process_bet(
        bet, classification, backfill_market_key=True, leave_error=False,
    )
    assert outcome["status"] == "success"
    assert mk_writes == [(2, "42", "alternate_totals")]
