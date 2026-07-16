#!/usr/bin/env python3
"""
Diagnostic: classify every Bets row that is NOT contributing a trusted CLV by
ROOT CAUSE, so each subset maps to a concrete fix. Read-only — writes nothing.

Two orthogonal failure modes are teased apart because they need different fixes:
  * price_state  — do we have a numeric closing price at all, and if not why?
  * start_state  — can we verify the price was pregame (actual-start resolvable)?

A row is "trusted" only when it has a numeric price AND a verified/safe start.
Everything else is bucketed and counted.

Usage (from Bet-Result-Checker-github/):
  python scripts/closing_gap_taxonomy.py            # summary to stdout
  python scripts/closing_gap_taxonomy.py --json out.json   # + per-row detail
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv

load_dotenv()

from config import (
    BET_COL, SHEET_TAB, AUTOMATED_BET_TYPES,
    BET_TYPE_PARLAY, BET_TYPE_PROP, CLOSING_ODDS_ERROR_CODES,
)

AMERICAN_RE = re.compile(r"^[+-]?\d+$")
# Books whose closing line is not on the TOA historical feed.
MANUAL_BOOKS = {"polymarket", "betopenly"}
KALSHI = "kalshi"
# Sports with an actual-start resolver today (mirrors actual_start.py:
# MLB via statsapi + every ESPN_ROUTES key).
RESOLVER_SPORTS = {
    "baseball_mlb", "basketball_nba", "basketball_wnba",
    "soccer_usa_mls", "soccer_fifa_world_cup",
    "soccer_england_efl_cup",
    "tennis_atp_wimbledon", "tennis_wta_wimbledon",
    "manual_wta_libema",
    "mma_mixed_martial_arts",
}


def has_numeric_price(closing: str) -> bool:
    text = (closing or "").strip()
    if not text or text.upper() in {"VOID", "N/A"} or text in CLOSING_ODDS_ERROR_CODES:
        return False
    return bool(AMERICAN_RE.match(text))


def price_state(row: dict) -> str:
    """Why is there no numeric closing price? (or 'has_price')."""
    closing = (row.get("closing_odds") or "").strip()
    if has_numeric_price(closing):
        return "has_price"
    upper = closing.upper()
    if upper == "VOID":
        return "void"
    if closing in CLOSING_ODDS_ERROR_CODES:
        return {
            "MANUAL ENTRY": "err_manual_entry",
            "SPORT NOT ON API": "err_sport_not_on_api",
            "SELECTION NOT FOUND": "err_selection_not_found",
            "BOOK NOT FOUND": "err_book_not_found",
            "GAME NOT FOUND": "err_game_not_found",
        }[closing]
    if upper == "N/A":
        return "na_exhausted"
    return "blank"


def priceability(row: dict) -> str:
    """Structural reason a row can/can't be auto-priced, independent of outcome."""
    bet_type = (row.get("bet_type") or "").strip()
    book = (row.get("book") or "").strip().lower()
    if (row.get("live_bet") or "").strip().upper() == "TRUE":
        return "live_bet"
    if bet_type == BET_TYPE_PROP:
        return "prop"
    if bet_type == BET_TYPE_PARLAY:
        return "parlay"
    if bet_type not in AUTOMATED_BET_TYPES:
        return "other_untyped"
    if book in MANUAL_BOOKS:
        return "manual_book"
    if book == KALSHI:
        return "kalshi_with_ticker" if (row.get("kalshi_ticker") or "").strip() else "kalshi_no_ticker"
    return "auto_priceable"


def start_resolvability(row: dict) -> str:
    """Can we establish an actual start to verify the price was pregame?"""
    if (row.get("actual_start") or "").strip():
        return "already_resolved"
    sport = (row.get("sport") or "").strip()
    if row.get("bet_type") == BET_TYPE_PARLAY:
        return "parlay_per_leg"
    if sport in RESOLVER_SPORTS:
        return "resolver_exists"
    return "no_resolver"


def load_rows() -> list[dict]:
    from sheets_reader import (
        _get_bets_rows, _resolve_bet_col_indices, _pad_bet_row, _bet_cell,
    )
    rows = _get_bets_rows(SHEET_TAB)
    if not rows:
        return []
    col = _resolve_bet_col_indices(rows[0])
    keys = [
        "bet_id", "sport", "book", "bet_type", "selection", "kalshi_ticker",
        "closing_odds", "clv", "result", "start_status", "closing_quality",
        "start_audit", "actual_start", "actual_start_confidence",
    ]
    # live_bet lives outside BET_COL's resolver in some sheets; read by header.
    headers = rows[0]
    live_idx = headers.index("Live Bet") if "Live Bet" in headers else None
    out = []
    for row in rows[1:]:
        padded = _pad_bet_row(row, col)
        rec = {key: _bet_cell(padded, col, key) for key in keys if col.get(key) is not None}
        rec["live_bet"] = (padded[live_idx] if live_idx is not None and live_idx < len(padded) else "")
        if (rec.get("bet_id") or "").strip():
            out.append(rec)
    return out


def is_trusted(row: dict) -> bool:
    from closing_provenance import legacy_row_is_pooled
    if not has_numeric_price(row.get("closing_odds", "")):
        return False
    return legacy_row_is_pooled(
        row.get("start_status", ""), row.get("closing_quality", ""),
        row.get("start_audit", ""),
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Classify non-trusted-CLV Bets rows by root cause.")
    parser.add_argument("--json", help="Write full per-row detail to this path")
    args = parser.parse_args(argv)

    rows = load_rows()
    total = len(rows)
    trusted = [r for r in rows if is_trusted(r)]
    gap = [r for r in rows if not is_trusted(r)]

    by_price = Counter()
    by_priceability = Counter()
    price_by_start = defaultdict(Counter)      # price_state -> start_resolvability counts
    noprice_by_priceability = Counter()        # for rows lacking a numeric price
    detail = []

    for r in gap:
        ps = price_state(r)
        pa = priceability(r)
        sr = start_resolvability(r)
        by_price[ps] += 1
        by_priceability[pa] += 1
        if ps == "has_price":
            price_by_start[ps][sr] += 1
        else:
            noprice_by_priceability[pa] += 1
        detail.append({
            "bet_id": r.get("bet_id"), "sport": r.get("sport"), "book": r.get("book"),
            "bet_type": r.get("bet_type"), "closing_odds": r.get("closing_odds"),
            "start_audit": r.get("start_audit"), "closing_quality": r.get("closing_quality"),
            "price_state": ps, "priceability": pa, "start_resolvability": sr,
        })

    def show(title, counter):
        print(f"\n{title}")
        for name, n in counter.most_common():
            print(f"  {n:>4}  {name}")

    print("=" * 64)
    print(f"Total bet rows:            {total}")
    print(f"  Trusted CLV (pooled):    {len(trusted)}")
    print(f"  NOT contributing CLV:    {len(gap)}")
    print("=" * 64)

    show("A. By price_state (do we have a numeric closing price?)", by_price)
    show("B. Rows WITH a price but excluded — by start_resolvability",
         price_by_start["has_price"])
    show("C. Rows with NO price — by structural priceability", noprice_by_priceability)
    show("D. All gap rows — by structural priceability", by_priceability)

    # The headline actionable cut.
    has_price_no_resolver = price_by_start["has_price"]["no_resolver"]
    has_price_resolver = price_by_start["has_price"]["resolver_exists"]
    print("\n" + "=" * 64)
    print("ACTIONABLE SUMMARY")
    print(f"  Have price, resolver EXISTS (re-audit now):     {has_price_resolver}")
    print(f"  Have price, need a NEW start resolver:          {has_price_no_resolver}")
    print(f"  No price, auto-priceable (re-fetch may work):   {noprice_by_priceability.get('auto_priceable', 0)}")
    print(f"  No price, needs manual/book source:             "
          f"{noprice_by_priceability.get('manual_book', 0) + noprice_by_priceability.get('kalshi_no_ticker', 0)}")
    print(f"  Structurally not auto-priceable (prop/parlay):  "
          f"{noprice_by_priceability.get('prop', 0) + noprice_by_priceability.get('parlay', 0)}")
    print("=" * 64)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump({"total": total, "trusted": len(trusted), "gap": len(gap),
                       "by_price_state": dict(by_price),
                       "has_price_by_start": dict(price_by_start["has_price"]),
                       "noprice_by_priceability": dict(noprice_by_priceability),
                       "rows": detail}, fh, indent=2, sort_keys=True)
        print(f"\nPer-row detail written to {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
