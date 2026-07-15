"""Pinnacle same-event, same-market closing benchmark with power devig."""

from __future__ import annotations

from datetime import datetime, timezone

import requests

from config import ODDS_API_BASE, ODDS_API_KEY
from closing_odds import parse_selection, to_decimal_odds


FEATURED_MARKETS = "h2h,spreads,totals"


def implied_probability(american) -> float | None:
    try:
        value = float(american)
    except (TypeError, ValueError):
        return None
    if value < 0:
        return abs(value) / (abs(value) + 100)
    if value > 0:
        return 100 / (value + 100)
    return None


def power_devig(probabilities: list[float]) -> list[float]:
    total = sum(probabilities)
    if total <= 0:
        return [0.0 for _ in probabilities]
    if total <= 1 or any(probability <= 0 or probability >= 1 for probability in probabilities):
        return [probability / total for probability in probabilities]
    def f(exponent):
        return sum(probability ** exponent for probability in probabilities) - 1
    low, high = 1.0, 2.0
    while f(high) > 0 and high < 256:
        high *= 2
    for _ in range(80):
        middle = (low + high) / 2
        if f(middle) > 0:
            low = middle
        else:
            high = middle
    exponent = (low + high) / 2
    fair = [probability ** exponent for probability in probabilities]
    fair_total = sum(fair)
    return [probability / fair_total for probability in fair]


def fair_decimal(probability: float) -> float | None:
    return round(1 / probability, 8) if probability > 0 else None


def fair_american(probability: float) -> int | None:
    if not 0 < probability < 1:
        return None
    if probability >= .5:
        return round(-(probability / (1 - probability)) * 100)
    return round(((1 - probability) / probability) * 100)


def fetch_pinnacle_featured(sport_key: str) -> dict:
    try:
        response = requests.get(
            f"{ODDS_API_BASE}/sports/{sport_key}/odds",
            params={
                "apiKey": ODDS_API_KEY, "regions": "eu", "bookmakers": "pinnacle",
                "markets": FEATURED_MARKETS, "oddsFormat": "american", "dateFormat": "iso",
            },
            timeout=15,
        )
        response.raise_for_status()
        return {
            "ok": True, "events": response.json(),
            "fetched_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "credits": {
                "last": response.headers.get("x-requests-last"),
                "used": response.headers.get("x-requests-used"),
                "remaining": response.headers.get("x-requests-remaining"),
            },
        }
    except (requests.RequestException, ValueError) as exc:
        return {"ok": False, "events": [], "error": str(exc), "credits": {}}


def _matches_selected(outcome: dict, selection: dict, mode: str) -> bool:
    name = str(outcome.get("name") or "")
    if mode == "h2h":
        wanted = str(selection.get("selection_team") or "")
        return wanted.lower() == "draw" and name.lower() == "draw" or (
            wanted.lower() in name.lower() or name.lower() in wanted.lower()
        )
    if mode == "spread":
        wanted = str(selection.get("selection_team") or "")
        return (wanted.lower() in name.lower() or name.lower() in wanted.lower()) and abs(
            float(outcome.get("point")) - float(selection.get("selection_point"))
        ) < 1e-6
    if mode == "total":
        return name.lower() == selection.get("selection_side") and abs(
            float(outcome.get("point")) - float(selection.get("selection_point"))
        ) < 1e-6
    return False


def pinnacle_quote_for_bet(events: list, bet: dict) -> dict | None:
    selection = parse_selection(bet.get("bet_type", ""), bet.get("selection", ""))
    if not selection:
        return None
    mode = selection.get("extract_mode")
    market_key = {"h2h": "h2h", "spread": "spreads", "total": "totals"}.get(mode)
    if market_key is None:
        return None
    event_id = str(bet.get("event_id") or "").strip()
    if not event_id:
        return None
    event = next((item for item in events if str(item.get("id")) == event_id), None)
    if not event:
        return None
    bookmaker = next((book for book in event.get("bookmakers", []) if book.get("key") == "pinnacle"), None)
    if not bookmaker:
        return None
    market = next((item for item in bookmaker.get("markets", []) if item.get("key") == market_key), None)
    if not market:
        return None
    outcomes = market.get("outcomes") or []
    selected = next((outcome for outcome in outcomes if _matches_selected(outcome, selection, mode)), None)
    if not selected:
        return None
    if mode == "spread":
        point = float(selected.get("point"))
        market_outcomes = [outcome for outcome in outcomes if (
            outcome is selected or abs(float(outcome.get("point", 999999)) + point) < 1e-6
        )]
    elif mode == "total":
        point = float(selected.get("point"))
        market_outcomes = [outcome for outcome in outcomes if abs(float(outcome.get("point", 999999)) - point) < 1e-6]
    else:
        market_outcomes = outcomes
    if len(market_outcomes) not in (2, 3) or selected not in market_outcomes:
        return None
    raw_probs = [implied_probability(outcome.get("price")) for outcome in market_outcomes]
    if any(probability is None for probability in raw_probs):
        return None
    fair_probs = power_devig(raw_probs)
    selected_index = market_outcomes.index(selected)
    probability = fair_probs[selected_index]
    return {
        "event_id": event.get("id"), "market": market_key,
        "selected_price": selected.get("price"),
        "outcomes": [{"name": outcome.get("name"), "point": outcome.get("point"), "price": outcome.get("price")}
                     for outcome in market_outcomes],
        "fair_probability": probability,
        "fair_decimal": fair_decimal(probability),
        "fair_american": fair_american(probability),
        "book_last_update": market.get("last_update") or bookmaker.get("last_update"),
    }
