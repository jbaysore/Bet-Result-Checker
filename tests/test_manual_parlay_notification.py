import json
from datetime import datetime, timedelta, timezone

import poller
import sheets_reader
from config import RESULT_NEEDS_REVIEW


HEADERS = [
    "BetID", "Sport", "Book", "Team 1", "Team 2", "Game Date", "Game Start Time",
    "Selection", "Bet Type", "OddsTaken", "Stake", "Fee", "Bet Category",
    "Promo ID", "Result", "Legs",
]


def test_loader_keeps_ungradable_parlay_for_manual_review(monkeypatch):
    legs = json.dumps([
        {
            "sport": "soccer_fifa_world_cup",
            "team1": "France",
            "team2": "Spain",
            "gameDate": "2026-07-14",
            "gameStart": "14:00",
            "betType": "Total",
            "selection": "Over 1.5",
        },
        {
            "sport": "soccer_fifa_world_cup",
            "team1": "France",
            "team2": "Spain",
            "gameDate": "2026-07-14",
            "gameStart": "14:00",
            "betType": "Prop",
            "selection": "PROP: France 3+ Shots On Target",
        },
    ])
    row = [
        "333", "soccer_fifa_world_cup", "draftkings", "France", "Spain",
        "2026-07-14", "14:00", "PARLAY", "Parlay", "+100", "10", "0",
        "Standard", "", "", legs,
    ]
    monkeypatch.setattr(sheets_reader, "_get_bets_rows", lambda tab: [HEADERS, row])

    bets = sheets_reader.load_pending_bets("Bets")

    assert len(bets) == 1
    assert bets[0]["bet_id"] == "333"
    assert bets[0]["is_parlay"] is False
    assert bets[0]["manual_review_required"] is True
    assert "cannot be graded automatically" in bets[0]["manual_review_reason"]


def test_manual_parlay_notifies_only_after_standard_give_up(monkeypatch):
    writes = []
    monkeypatch.setattr(
        poller,
        "write_result",
        lambda row_idx, result, bet_id, **kwargs: writes.append((row_idx, result, bet_id)),
    )
    bet = {
        "row_idx": 12,
        "bet_id": "333",
        "manual_review_reason": "Parlay contains a custom prop",
    }
    give_up_at = datetime(2026, 7, 14, 23, 0, tzinfo=timezone.utc)

    assert poller._poll_manual_review(
        bet, give_up_at - timedelta(seconds=1), give_up_at,
    ) == "still_pending"
    assert writes == []

    assert poller._poll_manual_review(bet, give_up_at, give_up_at) == "needs_review"
    assert writes == [(12, RESULT_NEEDS_REVIEW, "333")]
