"""Kalshi closing-odds source: price conversion, candle selection, routing."""

from datetime import datetime, timezone

import pytest

from sources import kalshi


# ── Pure helpers ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("prob,american", [
    (0.60, -150),
    (0.40, 150),
    (0.50, -100),
    (0.10, 900),
])
def test_to_american(prob, american):
    assert kalshi.to_american(prob) == american


def test_to_american_degenerate():
    assert kalshi.to_american(1.0) is None
    assert kalshi.to_american(0.0) is None
    assert kalshi.to_american("nope") is None


def test_series_from_ticker():
    assert kalshi.series_from_ticker("KXMLBGAME-26JUL042210SDLAD-SD") == "KXMLBGAME"
    assert kalshi.series_from_ticker("") == ""


def test_candle_yes_price_prefers_ask_then_traded():
    assert kalshi._candle_yes_price(
        {"yes_ask": {"close_dollars": "0.6000"}, "price": {"close_dollars": "0.1300"}}) == 0.60
    # Degenerate ask (1.0 = no real offer) falls back to the last traded price.
    assert kalshi._candle_yes_price(
        {"yes_ask": {"close_dollars": "1.0000"}, "price": {"close_dollars": "0.1300"}}) == 0.13
    assert kalshi._candle_yes_price({"yes_ask": {}, "price": {}}) is None


# ── get_closing_american (mocked HTTP) ─────────────────────────────────────────

class _FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _patch_candles(monkeypatch, candles):
    payload = {"candlesticks": candles}
    monkeypatch.setattr(kalshi.requests, "get", lambda *a, **k: _FakeResp(payload))


def test_get_closing_american_picks_last_pre_start_candle(monkeypatch):
    start = datetime(2026, 7, 4, 22, 0, tzinfo=timezone.utc)
    start_ts = int(start.timestamp())
    candles = [
        {"end_period_ts": start_ts - 120, "yes_ask": {"close_dollars": "0.50"}, "price": {"close_dollars": "0.50"}},
        {"end_period_ts": start_ts - 60,  "yes_ask": {"close_dollars": "0.60"}, "price": {"close_dollars": "0.58"}},
        # A candle AFTER start must be ignored.
        {"end_period_ts": start_ts + 60,  "yes_ask": {"close_dollars": "0.90"}, "price": {"close_dollars": "0.90"}},
    ]
    _patch_candles(monkeypatch, candles)
    # Last pre-start candle yes_ask close 0.60 -> -150.
    assert kalshi.get_closing_american("KXMLBGAME-X-Y", start, "t") == -150


def test_get_closing_american_no_candles(monkeypatch):
    _patch_candles(monkeypatch, [])
    start = datetime(2026, 7, 4, 22, 0, tzinfo=timezone.utc)
    assert kalshi.get_closing_american("KXMLBGAME-X-Y", start, "t") is None


def test_get_closing_american_no_ticker():
    start = datetime(2026, 7, 4, 22, 0, tzinfo=timezone.utc)
    assert kalshi.get_closing_american("", start, "t") is None


# ── closing_odds routing ───────────────────────────────────────────────────────

def test_closing_odds_routes_kalshi_with_ticker(monkeypatch):
    import closing_odds
    monkeypatch.setattr("sources.kalshi.get_closing_american", lambda *a, **k: -150)
    bet = {
        "bet_id": "1", "sport": "baseball_mlb", "book": "kalshi",
        "team1": "San Diego", "team2": "LA Dodgers", "game_date": "2026-06-30",
        "game_start": "19:05", "bet_type": "Moneyline", "selection": "San Diego",
        "kalshi_ticker": "KXMLBGAME-26JUL042210SDLAD-SD",
    }
    res = closing_odds._fetch_closing_price(bet, "BetID 1")
    assert res == {"price": -150, "error": None}


def _patch_market(monkeypatch, market):
    monkeypatch.setattr(kalshi.requests, "get", lambda *a, **k: _FakeResp({"market": market}))


@pytest.mark.parametrize("status,result,expected", [
    ("finalized", "yes", "yes"),
    ("finalized", "no", "no"),
    ("settled", "yes", "yes"),
    ("active", "", None),          # not settled yet
    ("finalized", "", None),       # finalized but no clear side (e.g. void)
])
def test_get_market_result(monkeypatch, status, result, expected):
    _patch_market(monkeypatch, {"status": status, "result": result})
    assert kalshi.get_market_result("KXMLBGAME-X-Y") == expected


def test_get_market_result_no_ticker():
    assert kalshi.get_market_result("") is None


def test_closing_odds_kalshi_without_ticker_is_manual(monkeypatch):
    import closing_odds
    from config import CLOSING_ODDS_MANUAL_REQUIRED
    bet = {
        "bet_id": "1", "sport": "baseball_mlb", "book": "kalshi",
        "team1": "San Diego", "team2": "LA Dodgers", "game_date": "2026-06-30",
        "game_start": "19:05", "bet_type": "Moneyline", "selection": "San Diego",
        "kalshi_ticker": "",
    }
    res = closing_odds._fetch_closing_price(bet, "BetID 1")
    assert res["error"] == CLOSING_ODDS_MANUAL_REQUIRED
