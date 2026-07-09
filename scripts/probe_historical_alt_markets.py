"""
One-off probe: confirm The Odds API historical endpoint returns
alternate_spreads, alternate_totals, and team_totals for a book.

Usage (from Bet-Result-Checker-github/):
  python scripts/probe_historical_alt_markets.py

Requires ODDS_API_KEY in .env or environment. Exits 0 if all three markets
return HTTP 200 with parseable data (or empty events list — still valid).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

import requests

from closing_odds import region_for_book_key

ODDS_API_BASE = "https://api.the-odds-api.com/v4"
API_KEY = os.getenv("ODDS_API_KEY")

# Recent MLB game snapshot — adjust date if needed for a known past slate.
SPORT = "baseball_mlb"
DATE_ISO = "2026-07-08T23:00:00Z"
BOOK = "draftkings"
MARKETS = ["alternate_spreads", "alternate_totals", "team_totals"]


def probe_market(market: str) -> dict:
    url = f"{ODDS_API_BASE}/historical/sports/{SPORT}/odds"
    params = {
        "apiKey": API_KEY,
        "regions": region_for_book_key(BOOK),
        "markets": market,
        "oddsFormat": "american",
        "dateFormat": "iso",
        "date": DATE_ISO,
        "bookmakers": BOOK,
    }
    resp = requests.get(url, params=params, timeout=15)
    out = {
        "market": market,
        "status": resp.status_code,
        "events": 0,
        "sample_outcomes": 0,
    }
    if resp.status_code == 200:
        data = resp.json()
        events = data.get("data") or []
        out["events"] = len(events)
        for ev in events[:3]:
            for bk in ev.get("bookmakers") or []:
                if bk.get("key") != BOOK:
                    continue
                for mkt in bk.get("markets") or []:
                    if mkt.get("key") == market:
                        out["sample_outcomes"] += len(mkt.get("outcomes") or [])
    return out


def main():
    if not API_KEY:
        print("SKIP: ODDS_API_KEY not set — probe not run (implementation assumes markets are supported).")
        return 0

    print(f"Probing historical {SPORT} @ {DATE_ISO} book={BOOK}\n")
    failed = []
    for market in MARKETS:
        r = probe_market(market)
        print(f"  {market}: HTTP {r['status']}, events={r['events']}, outcomes(sample)={r['sample_outcomes']}")
        if r["status"] not in (200,):
            failed.append(market)

    if failed:
        print(f"\nFAILED markets: {failed}")
        return 1
    print("\nAll alternate/team-total markets accepted by historical endpoint.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
