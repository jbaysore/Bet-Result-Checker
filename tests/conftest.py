"""Shared factories for bet/promo row dicts used across promo_resolver tests."""

import pytest


@pytest.fixture(autouse=True)
def _neutralize_onboarding_gate(monkeypatch):
    """Default every test to onboarding OFF so the gate never reaches out to the
    live Google Sheet (worker/closing_odds/poller tests all call finalize paths
    that would otherwise trigger a profile load + shadow-log write). Tests that
    exercise the gate re-enable the flags and inject in-memory caches themselves.
    """
    try:
        import config
        import onboarding_gate
    except Exception:
        return
    monkeypatch.setattr(config, "ONBOARDING_SHADOW_MODE", False, raising=False)
    monkeypatch.setattr(config, "ONBOARDING_ENFORCE", False, raising=False)
    monkeypatch.setattr(onboarding_gate, "_profile", None, raising=False)
    monkeypatch.setattr(onboarding_gate, "_registry", None, raising=False)
    monkeypatch.setattr(onboarding_gate, "_SHADOW_LOG_PATH",
                        "shadow_logs/_test_should_not_write.jsonl", raising=False)


def make_bet(**overrides):
    base = {
        "row_idx": 2,
        "bet_id": "1",
        "date_placed": "2026-06-01",
        "book": "draftkings",
        "sport": "americanfootball_nfl",
        "stake": "100",
        "fee": "0",
        "bet_category": "Standard",
        "promo_id": "1",
        "result": "",
        "payout": "",
        "pl": "",
        "odds_taken": "-110",
    }
    base.update(overrides)
    return base


def make_promo(**overrides):
    base = {
        "row_idx": 2,
        "promo_id": "1",
        "book": "draftkings",
        "promo_name": "",
        "promo_type": "Bonus Bet",
        "boost_pct": "",
        "reward": "1 x $25 Bonus Bet",
        "qualifying_cost": "",
        "bonus_amount": "25",
        "status": "Pending",
        "notes": "",
        "expiration_date": "2026-06-30",
        "expected_reward_count": "1",
        "reward_timing": "End of Window",
        "token_usage_window": "",
        "start_date": "",
    }
    base.update(overrides)
    return base
