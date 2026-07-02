"""
Historical closing price for a Kalshi bet, from Kalshi's public (keyless) API.

Kalshi is a contracts exchange, not on The Odds API historical feed, so its
closing line comes straight from Kalshi's candlesticks endpoint. Given the
market ticker stored on the bet (resolved by the odds-tool at log time), we
pull the candle at ~game start and convert the yes-side price to American odds
the SAME way the live-odds path does (odds-tool/kalshiDirectH2h.js: toAmericanOdds
on the yes ask, falling back to the last traded price) so CLV compares
like-for-like with the recorded OddsTaken.

Public and keyless -- confirmed the candlesticks endpoint returns data without
auth (a 401 would mean a key is required; it returns 400 for missing params).
"""

import time
import requests
from datetime import timezone

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"

# How far back before game start to pull minute candles when locating the close.
_CLOSING_WINDOW_SECONDS = 3 * 60 * 60  # 3 hours


def to_american(prob):
    """Probability in dollars (0-1) -> American odds. Ports noVig.js toAmericanOdds."""
    try:
        p = float(prob)
    except (TypeError, ValueError):
        return None
    if p <= 0 or p >= 1:
        return None
    if p >= 0.5:
        return round(-(p / (1 - p)) * 100)
    return round(((1 - p) / p) * 100)


def series_from_ticker(ticker):
    """Kalshi market ticker -> its series (the segment before the first '-')."""
    return (ticker or "").split("-", 1)[0]


def get_market_result(ticker, label=""):
    """
    The Kalshi market's own settlement for a bet's captured ticker:

        'yes'  -> the bet's SELECTION won   (we stored the selection's market)
        'no'   -> the selection lost
        None   -> not finalized yet, no ticker, or an API error

    Because the stored ticker is the selection's own yes-market, 'yes' maps
    straight to a WIN with no team matching. This is authoritative (it's the
    exact contract you held, settled by Kalshi's own rules) and covers games
    the Odds API scores feed is slow on or doesn't carry.
    """
    if not ticker:
        return None
    url = f"{KALSHI_BASE}/markets/{ticker}"
    try:
        resp = requests.get(url, timeout=15)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        m = resp.json().get("market") or {}
    except (requests.RequestException, ValueError) as e:
        print(f"[kalshi] {label}: market fetch failed: {e}")
        return None

    status = str(m.get("status") or "").lower()
    result = str(m.get("result") or "").lower()
    if status in ("settled", "finalized") and result in ("yes", "no"):
        return result
    return None


def _candle_yes_price(candle):
    """
    Yes-side closing price (dollars, 0-1) for one candle, mirroring the live
    path's 'yes_ask, else last traded' preference. Skips a degenerate ask of
    1.0 (no real offer) and 0 (no market).
    """
    def _num(section, field):
        raw = (candle.get(section) or {}).get(field)
        try:
            v = float(raw) if raw is not None else None
        except (TypeError, ValueError):
            return None
        return v if (v is not None and 0 < v < 1) else None

    return _num("yes_ask", "close_dollars") or _num("price", "close_dollars")


def get_closing_american(ticker, game_start_dt, label=""):
    """
    Closing American odds for a Kalshi bet's market at ~game start, or None.

    Args:
        ticker:        the Kalshi market ticker stored on the bet.
        game_start_dt: tz-aware datetime of game start.
        label:         short identifier for log lines.

    Returns int (American) on success, or None on any failure (missing ticker,
    no candle, unusable price, or API error). The caller decides whether that
    means retry or fall back to manual entry.
    """
    if not ticker or game_start_dt is None:
        return None
    series = series_from_ticker(ticker)
    if not series:
        return None

    end_ts = int(game_start_dt.astimezone(timezone.utc).timestamp())
    start_ts = end_ts - _CLOSING_WINDOW_SECONDS
    url = f"{KALSHI_BASE}/series/{series}/markets/{ticker}/candlesticks"
    params = {"start_ts": start_ts, "end_ts": end_ts, "period_interval": 1}

    candles = None
    for attempt in range(3):
        try:
            resp = requests.get(url, params=params, timeout=15)
            if resp.status_code == 404:
                print(f"[kalshi] {label}: market '{ticker}' not found (404).")
                return None
            resp.raise_for_status()
            candles = resp.json().get("candlesticks") or []
            break
        except requests.RequestException as e:
            if attempt < 2:
                time.sleep(2)
                continue
            print(f"[kalshi] {label}: candlestick fetch failed: {e}")
            return None

    if not candles:
        print(f"[kalshi] {label}: no candles in the pre-start window for {ticker}.")
        return None

    # The last candle at/just before start with a usable yes price is the close.
    for candle in sorted(candles, key=lambda c: c.get("end_period_ts", 0), reverse=True):
        if candle.get("end_period_ts", 0) > end_ts:
            continue
        price = _candle_yes_price(candle)
        if price is not None:
            american = to_american(price)
            if american is not None:
                print(f"[kalshi] {label}: closing yes-price {price} -> {american:+d} "
                      f"(candle @ {candle.get('end_period_ts')}).")
                return american

    print(f"[kalshi] {label}: no usable yes price in candles for {ticker}.")
    return None
