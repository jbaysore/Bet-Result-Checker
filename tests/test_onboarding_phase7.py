from context_registry import ALIAS_EVENT_ID, ContextRegistry
from scripts.run_onboarding_verifier import ephemeral_row_is_verifiable


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
