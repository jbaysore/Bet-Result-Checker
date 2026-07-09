"""
Infer Odds API market keys from sheet Notes + Bet Type + Selection.

Mirrors odds-tool marketKey.js / shared/marketKey.mjs, with extra Notes
signals from the opportunity scanner (Alt Spread, Alt Total, Team Total).
"""

import re

from config import (
    BET_TYPE_DRAW, BET_TYPE_MONEYLINE, BET_TYPE_SPREAD, BET_TYPE_TOTAL,
)


def infer_market_key(notes: str, bet_type: str, selection: str) -> str:
    """
    Return an Odds API market key when inferable, else ''.

    Notes take precedence for alternate markets logged by the scanner.
  Cannot distinguish main vs alternate spread from selection alone (e.g.
    Cubs -3.5) — leave blank; closing_odds cascade handles that.
    """
    note = notes or ""
    bt = (bet_type or "").strip()
    sel = (selection or "").strip()

    if re.search(r"alt spread", note, re.IGNORECASE):
        return "alternate_spreads"
    if re.search(r"alt total", note, re.IGNORECASE):
        return "alternate_totals"
    if re.search(r"team total", note, re.IGNORECASE):
        return "team_totals"

    if bt == BET_TYPE_TOTAL and re.search(r"\bTeam Total\b", sel, re.IGNORECASE):
        return "team_totals"

    if bt in (BET_TYPE_MONEYLINE, BET_TYPE_DRAW):
        return "h2h"
    if bt == BET_TYPE_SPREAD:
        return "spreads"
    if bt == BET_TYPE_TOTAL:
        return "totals"

    return ""
