from context_registry import ALIAS_EVENT_ID, ContextRegistry
from capability_profile import CapabilityProfile
import config
import onboarding_decisions
import pytest
import sheets_reader
from scripts.run_onboarding_verifier import (
    _reconcile_pending_bet_intents, ephemeral_row_is_verifiable,
)


def test_ephemeral_row_verifies_from_row_evidence_without_profile_trust():
    registry = ContextRegistry([{
        "Context ID": "soccer/special/event-abc", "Alias Type": ALIAS_EVENT_ID,
        "Alias Value": "evt-1", "Edition Start": "", "Edition End": "",
        "Mapping Version": "1", "Status": "active", "Notes": "ephemeral",
    }])
    row = {
        "Sport": "soccer_special", "Event ID": "evt-1", "Game Date": "2026-07-16",
        "Closing Quality": "PROVISIONAL", "Notes": "onboarding: special",
        "Closing Observed At": "2026-07-16T19:55:00Z",
        "Actual Start": "2026-07-16T20:00:00Z", "Actual Start Confidence": "CONFIDENT",
        "ClosingOdds": "-110",
    }
    assert ephemeral_row_is_verifiable(row, registry)


def test_ephemeral_row_rejects_quote_inside_safety_margin():
    registry = ContextRegistry([{
        "Context ID": "soccer/special/event-abc", "Alias Type": ALIAS_EVENT_ID,
        "Alias Value": "evt-1", "Status": "active",
    }])
    row = {
        "Sport": "soccer_special", "Event ID": "evt-1",
        "Closing Quality": "PROVISIONAL", "Notes": "onboarding: special",
        "Closing Observed At": "2026-07-16T19:59:30Z",
        "Actual Start": "2026-07-16T20:00:00Z", "Actual Start Confidence": "CONFIDENT",
        "ClosingOdds": "-110",
    }
    assert not ephemeral_row_is_verifiable(row, registry)


def test_pending_future_bet_reconciles_without_scanner_or_start_time(monkeypatch):
    monkeypatch.setattr(config, "ONBOARDING_ENFORCE", True)
    monkeypatch.setattr(sheets_reader, "load_onboarding_bet_intents", lambda _tab: [{
        "bet_id": "42", "sport": "soccer_brand_new", "book": "DraftKings",
        "team1": "A", "team2": "B", "game_date": "2099-01-01",
        "game_start": "20:00", "event_id": "future-event",
        "market_key": "h2h", "bet_type": "Moneyline", "legs": [],
    }])
    applied = []
    monkeypatch.setattr(onboarding_decisions, "apply_discovery",
                        lambda profile, row, payload: applied.append((row, payload)) or 4)
    summary = _reconcile_pending_bet_intents(
        CapabilityProfile([]), ContextRegistry([]), apply=True)
    assert summary == {"examined": 1, "needed": 1, "created": 4, "failed": 0}
    assert applied[0][1]["intent"] == "bet"
    assert applied[0][0]["Context ID"] == "soccer/brand_new"


def test_onboarding_reader_includes_manually_settled_bet_types(monkeypatch):
    headers = list(config.BET_COL.values())

    def sheet_row(**values):
        by_header = {
            "BetID": "51", "Book": "DraftKings", "Sport": "basketball_nba",
            "Team 1": "A", "Team 2": "B", "Game Date": "2099-01-01",
            "Game Start Time": "20:00", "Selection": "Player over 20.5 points",
            "Bet Type": "Prop", "Market Key": "player_points", "Result": "",
            **values,
        }
        return [by_header.get(header, "") for header in headers]

    monkeypatch.setattr(sheets_reader, "_get_bets_rows", lambda _tab: [
        headers,
        sheet_row(),
        sheet_row(BetID="52", Result="WIN"),
    ])
    rows = sheets_reader.load_onboarding_bet_intents("Bets")
    assert [row["bet_id"] for row in rows] == ["51"]
    assert rows[0]["bet_type"] == "Prop"
    assert rows[0]["market_key"] == "player_points"


def test_pending_bet_reconciliation_fails_scheduled_run_loudly(monkeypatch):
    monkeypatch.setattr(config, "ONBOARDING_ENFORCE", True)
    monkeypatch.setattr(sheets_reader, "load_onboarding_bet_intents", lambda _tab: [{
        "bet_id": "53", "sport": "soccer_brand_new", "book": "DraftKings",
        "market_key": "h2h", "bet_type": "Moneyline", "legs": [],
    }])
    monkeypatch.setattr(onboarding_decisions, "apply_discovery",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("write failed")))
    with pytest.raises(RuntimeError, match="must not report success"):
        _reconcile_pending_bet_intents(
            CapabilityProfile([]), ContextRegistry([]), apply=True)
