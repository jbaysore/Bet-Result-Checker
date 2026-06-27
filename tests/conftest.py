"""Shared factories for bet/promo row dicts used across promo_resolver tests."""


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
