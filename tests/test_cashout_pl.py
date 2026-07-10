"""Tests for manual cashout P/L derivation and poller integration."""

from resolver import derive_cashout_pl
from config import BET_CATEGORY_BONUS_BET, BET_CATEGORY_STANDARD
import poller


def test_cashout_fee_kept():
    assert derive_cashout_pl(95, 100, 0.5, BET_CATEGORY_STANDARD) == -5.5


def test_cashout_fee_before_odds():
    assert derive_cashout_pl(
        108, 100, 2, BET_CATEGORY_STANDARD, fee_before_odds=True
    ) == 8.0


def test_cashout_bonus_bet():
    assert derive_cashout_pl(50, 100, 0, BET_CATEGORY_BONUS_BET) == 50.0


def test_cashout_locked_profit_and_partial_loss():
    assert derive_cashout_pl(120, 100, 0, BET_CATEGORY_STANDARD) == 20.0
    assert derive_cashout_pl(80, 100, 0, BET_CATEGORY_STANDARD) == -20.0


def test_manual_payout_completion_routes_cashout_to_cashout_math(monkeypatch):
    writes = []
    cleared = []
    monkeypatch.setattr(poller, "get_book_fee_before_odds", lambda book: False)
    monkeypatch.setattr(
        poller, "write_pl_only",
        lambda row_idx, bet_id, pl: writes.append((row_idx, bet_id, pl)) or True,
    )
    monkeypatch.setattr(
        poller, "clear_pl_blocked_flag",
        lambda row_idx, bet_id: cleared.append((row_idx, bet_id)) or True,
    )

    status = poller.complete_manual_payout_pl({
        "row_idx": 7,
        "bet_id": "42",
        "book": "DraftKings",
        "result": "CASHOUT",
        "payout": "95.00",
        "stake": "100.00",
        "fee": "0.50",
        "bet_category": BET_CATEGORY_STANDARD,
        "odds_taken": "-110",
    })

    assert status == "completed"
    assert writes == [(7, "42", -5.5)]
    assert cleared == [(7, "42")]


def test_manual_payout_completion_honors_fee_before_odds_for_cashout(monkeypatch):
    writes = []
    monkeypatch.setattr(poller, "get_book_fee_before_odds", lambda book: True)
    monkeypatch.setattr(
        poller, "write_pl_only",
        lambda row_idx, bet_id, pl: writes.append(pl) or True,
    )
    monkeypatch.setattr(poller, "clear_pl_blocked_flag", lambda *args: True)

    status = poller.complete_manual_payout_pl({
        "row_idx": 8,
        "bet_id": "43",
        "book": "Polymarket",
        "result": "CASHOUT",
        "payout": "108.00",
        "stake": "100.00",
        "fee": "2.00",
        "bet_category": BET_CATEGORY_STANDARD,
        "odds_taken": "-110",
    })

    assert status == "completed"
    assert writes == [8.0]


def test_cashout_without_payout_gets_actionable_blocked_note(monkeypatch):
    flags = []
    monkeypatch.setattr(
        poller, "flag_pl_blocked",
        lambda row_idx, bet_id, reason: flags.append((row_idx, bet_id, reason)) or True,
    )
    monkeypatch.setattr(
        poller, "_safe_calculate_pl_payout",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("cashout must not use normal settlement math")
        ),
    )

    status = poller.complete_pl_payout({
        "row_idx": 9,
        "bet_id": "44",
        "book": "DraftKings",
        "result": "CASHOUT",
        "bet_type": "Moneyline",
        "payout": "",
        "stake": "100.00",
        "fee": "0.00",
        "bet_category": BET_CATEGORY_STANDARD,
        "odds_taken": "-110",
    })

    assert status == "skipped"
    assert flags[0][0:2] == (9, "44")
    assert "exact Payout" in flags[0][2]
