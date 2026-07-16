#!/usr/bin/env python3
"""Repair onboarding-provisional rows once their grain is promoted (plan Phase 5).

A row capped PROVISIONAL by the onboarding gate carries an `onboarding:` Notes
marker. When its capability grain later reaches Verified (Phase 4 promotion), the
close can be re-derived and, if it now satisfies the full quality contract
(pre-start by authoritative start + margin, fresh book quote → VERIFIED_CLOSE),
the row is upgraded — the ORIGINAL values preserved permanently in a `pre-repair:`
marker and the new close tagged Closing Source = recovery-onboarding.

"Affected" = the row's own grain is now trusted AND it is not already repaired.
Failure leaves the row exactly as it was (concept safety #8). Idempotent: a
repaired row (pre-repair: marker / recovery-onboarding source) is never touched
again.

Usage (from Bet-Result-Checker-github/):
  py scripts/repair_onboarded_rows.py                 # preview only
  py scripts/repair_onboarded_rows.py --apply
  py scripts/repair_onboarded_rows.py --apply --bet-id 42
"""

from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv

load_dotenv()

import onboarding_policy as policy
from capability_profile import CapabilityProfile
from context_registry import ContextRegistry


# ── Pure classification (unit-tested) ────────────────────────────────────────
def classify_repair_row(bet: dict, is_trusted: bool) -> dict:
    """retry/skip decision for one onboarding row. Pure."""
    notes = str(bet.get("notes") or "")
    quality = str(bet.get("closing_quality") or "").strip().upper()
    source = str(bet.get("closing_source") or "").strip().lower()
    result = str(bet.get("result") or "").strip().upper()

    if source == "recovery-onboarding" or "pre-repair:" in notes:
        return {"bucket": "skip", "reason": "already repaired"}
    if "onboarding:" not in notes:
        return {"bucket": "skip", "reason": "no onboarding marker"}
    if quality == "VERIFIED_CLOSE":
        return {"bucket": "skip", "reason": "already verified"}
    if result == "VOID" or str(bet.get("closing_odds") or "").strip().upper() == "VOID":
        return {"bucket": "skip", "reason": "VOID — no CLV to repair"}
    if not is_trusted:
        return {"bucket": "skip", "reason": "grain not yet promoted — still provisional"}
    return {"bucket": "retry", "reason": "grain promoted — re-derive trusted close"}


def _is_trusted(profile: CapabilityProfile, registry: ContextRegistry, bet: dict) -> bool:
    if bet.get("is_parlay"):
        return bool(bet.get("legs")) and all(_is_trusted(
            profile, registry, {**leg, "book": bet.get("book", "")})
            for leg in bet["legs"])
    resolution = registry.resolve(bet.get("sport", ""), game_date=bet.get("game_date"))
    if not resolution.is_known:
        return False
    family = policy.market_family_for(bet.get("market_key"), bet.get("bet_type"))
    return profile.require_clv(resolution.context_id, bet.get("book", ""), family).trusted


# ── Loading ──────────────────────────────────────────────────────────────────
def load_onboarding_rows(bet_id: str | None = None) -> list[dict]:
    from config import SHEET_TAB, BET_TYPE_PARLAY
    from parlay import parse_legs
    from sheets_reader import _get_bets_rows, _resolve_bet_col_indices, _pad_bet_row, _bet_cell

    rows = _get_bets_rows(SHEET_TAB)
    if not rows:
        return []
    col = _resolve_bet_col_indices(rows[0])
    target = str(bet_id).strip() if bet_id is not None else None
    out = []
    for row_idx, row in enumerate(rows[1:], start=2):
        row = _pad_bet_row(row, col)
        this_id = _bet_cell(row, col, "bet_id")
        if not this_id or (target is not None and this_id != target):
            continue
        notes = _bet_cell(row, col, "notes")
        # Cheap pre-filter: only rows the gate marked (or, for --bet-id, any row).
        if target is None and "onboarding:" not in notes and "pre-repair:" not in notes:
            continue
        bet_type = _bet_cell(row, col, "bet_type")
        legs = parse_legs(_bet_cell(row, col, "legs")) if bet_type == BET_TYPE_PARLAY else []
        out.append({
            "row_idx": row_idx, "bet_id": this_id,
            "sport": _bet_cell(row, col, "sport"), "book": _bet_cell(row, col, "book"),
            "team1": _bet_cell(row, col, "team1"), "team2": _bet_cell(row, col, "team2"),
            "game_date": _bet_cell(row, col, "game_date"), "game_start": _bet_cell(row, col, "game_start"),
            "selection": _bet_cell(row, col, "selection"), "bet_type": bet_type,
            "is_parlay": bet_type == BET_TYPE_PARLAY, "legs": legs,
            "odds_taken": _bet_cell(row, col, "odds_taken"), "result": _bet_cell(row, col, "result"),
            "market_key": _bet_cell(row, col, "market_key"), "event_id": _bet_cell(row, col, "event_id"),
            "kalshi_ticker": _bet_cell(row, col, "kalshi_ticker"), "notes": notes,
            "closing_odds": _bet_cell(row, col, "closing_odds"),
            "decimal_closing": _bet_cell(row, col, "decimal_closing"),
            "clv": _bet_cell(row, col, "clv"),
            "closing_quality": _bet_cell(row, col, "closing_quality"),
            "closing_source": _bet_cell(row, col, "closing_source"),
            "actual_start": _bet_cell(row, col, "actual_start"),
            "actual_start_source": _bet_cell(row, col, "actual_start_source"),
            "actual_start_confidence": _bet_cell(row, col, "actual_start_confidence"),
            "start_status": _bet_cell(row, col, "start_status"),
        })
    return out


def _restore_provenance(bet: dict) -> dict:
    return {
        "start_status": bet.get("start_status", ""), "closing_quality": bet.get("closing_quality", ""),
        "closing_source": bet.get("closing_source", ""), "closing_observed_at": "",
        "start_detected_at": "", "actual_start": bet.get("actual_start", ""),
        "actual_start_source": bet.get("actual_start_source", ""),
        "actual_start_confidence": bet.get("actual_start_confidence", ""),
    }


def _parse_num(raw):
    text = str(raw or "").strip().rstrip("%")
    try:
        return float(text.replace(",", ""))
    except ValueError:
        return None


# ── Repair one row ───────────────────────────────────────────────────────────
def repair_bet(bet: dict) -> dict:
    from closing_odds import fetch_closing_odds, fetch_parlay_closing_odds
    from sheets_writer import repair_onboarded_close
    try:
        from scripts.retry_closing_odds import _retry_provenance
    except ImportError:
        from retry_closing_odds import _retry_provenance

    row_idx, bet_id = bet["row_idx"], bet["bet_id"]
    outcome = {"bet_id": bet_id, "status": "failed", "detail": "",
               "old_closing": bet.get("closing_odds", "")}
    original = {"close": bet.get("closing_odds", ""),
                "decimal": bet.get("decimal_closing", ""), "clv": bet.get("clv", ""),
                "quality": bet.get("closing_quality", "")}

    # Derive BEFORE clearing so a transient failure leaves the row untouched.
    fetch_bet = {**bet, "_resolve_actual_start": True}
    try:
        result = (fetch_parlay_closing_odds(fetch_bet)
                  if bet.get("is_parlay") else fetch_closing_odds(fetch_bet))
    except Exception as exc:  # noqa: BLE001
        outcome["detail"] = f"fetch raised: {exc} (row untouched)"
        return outcome

    if result.get("closing_odds") is None or result.get("closing_quality") != "VERIFIED_CLOSE":
        outcome["status"] = "still_provisional"
        outcome["detail"] = (f"derivation quality={result.get('closing_quality') or result.get('error')}"
                             " — row left provisional")
        return outcome

    repaired = repair_onboarded_close(
        row_idx, bet_id, result["closing_odds"], result.get("decimal_closing"), result.get("clv"),
        original=original, provenance=_retry_provenance(result))
    if not repaired:
        outcome["detail"] = "atomic repair refused/failed — original row remained untouched"
        return outcome

    outcome["status"] = "repaired"
    outcome["new_closing"] = result["closing_odds"]
    outcome["detail"] = f"{original['close'] or '(blank)'} -> {result['closing_odds']} (VERIFIED_CLOSE)"
    return outcome


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Repair onboarding-provisional rows whose grain was promoted.")
    parser.add_argument("--apply", action="store_true", help="Execute (default is preview only)")
    parser.add_argument("--bet-id", help="Process a single BetID")
    parser.add_argument("--pace", type=float, default=5.0, help="Seconds between writes")
    args = parser.parse_args(argv)

    profile = CapabilityProfile.load()
    registry = ContextRegistry.load()
    bets = load_onboarding_rows(bet_id=args.bet_id)
    if not bets:
        print("No onboarding rows found.")
        return 0

    classified = [(bet, classify_repair_row(bet, _is_trusted(profile, registry, bet))) for bet in bets]
    for bet, cls in classified:
        print(f"  BetID {bet['bet_id']:>6}  [{cls['bucket']:>5}]  {bet.get('book',''):<12} "
              f"{bet.get('team1','')} vs {bet.get('team2','')}  q={bet.get('closing_quality','')}")
        print(f"           -> {cls['reason']}")

    to_process = [bet for bet, cls in classified if cls["bucket"] == "retry"]
    if not args.apply:
        print(f"\nPreview: {len(to_process)} row(s) would be repaired; "
              f"{len(classified) - len(to_process)} skipped. Run with --apply to execute.")
        return 0
    if not to_process:
        print("\nNothing to repair.")
        return 0

    print(f"\nRepairing {len(to_process)} row(s)...\n")
    for position, bet in enumerate(to_process):
        if position and args.pace > 0:
            time.sleep(args.pace)
        outcome = repair_bet(bet)
        print(f"  BetID {outcome['bet_id']}: {outcome['status']} — {outcome['detail']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
