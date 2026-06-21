import sys
from datetime import datetime, timezone
import pytz
from sheets_reader import load_pending_bets, load_unresolved_pl_bets
from poller import poll_bet, complete_pl_payout, _parse_game_datetime
from config import COL, SHEET_TAB

CENTRAL = pytz.timezone("America/Chicago")


def main():
    print("=" * 60)
    print(f"  Bet Result Checker")
    print(f"  Started: {datetime.now(CENTRAL).strftime('%m/%d/%Y %I:%M:%S %p CT')}")
    print("=" * 60)

    # ── Load pending bets ────────────────────────────────────────
    print(f"[trigger] Loading pending bets from '{SHEET_TAB}' tab...")
    try:
        pending = load_pending_bets(SHEET_TAB, COL)
    except Exception as e:
        print(f"[trigger] ❌ Failed to load pending bets: {e}")
        sys.exit(1)

    if not pending:
        print("[trigger] ✅ No pending bets found. Nothing to do.")
        sys.exit(0)

    # ── Filter to bets where game time has passed ────────────────
    now_ct = datetime.now(CENTRAL)
    ready = []
    future = []

    for bet in pending:
        game_dt = _parse_game_datetime(bet["game_date"], bet["game_start"])
        if game_dt is None:
            print(f"[trigger] ⚠️  BetID {bet['bet_id']}: skipping — "
                  f"could not parse game datetime.")
            continue
        if game_dt.astimezone(CENTRAL) <= now_ct:
            ready.append(bet)
        else:
            future.append(bet)

    print(f"\n[trigger] {len(pending)} pending bet(s) found:")
    print(f"          {len(ready)} ready to check (game time passed)")
    print(f"          {len(future)} not yet started (will be skipped this run)")

    if future:
        print("\n[trigger] Upcoming bets (skipped this run):")
        for bet in future:
            print(f"          BetID {bet['bet_id']} — "
                  f"{bet['team1']} vs {bet['team2']} — "
                  f"{bet['game_date']} {bet['game_start']}")

    if not ready:
        print("\n[trigger] ✅ No bets ready to check right now.")
        sys.exit(0)

    # ── Process each ready bet ───────────────────────────────────
    print(f"\n[trigger] Processing {len(ready)} bet(s)...\n")

    results = {"resolved": 0, "still_pending": 0, "needs_review": 0}

    for i, bet in enumerate(ready, start=1):
        print(f"─ Bet {i}/{len(ready)} "
              f"─────────────────────────────────────────")
        print(f"  BetID:     {bet['bet_id']}")
        print(f"  Sport:     {bet['sport']}")
        print(f"  Game:      {bet['team1']} vs {bet['team2']}")
        print(f"  Date:      {bet['game_date']} {bet['game_start']}")
        print(f"  Bet type:  {bet['bet_type']}")
        print(f"  Selection: {bet['selection']}")
        print()

        # poll_bet now checks ONCE and returns a status string -- it never
        # sleeps or loops internally (2026-06-20 redesign: the 30-min
        # Cloud Scheduler trigger IS the retry mechanism now, not an
        # in-process wait).
        status = poll_bet(bet)

        if status == "resolved":
            results["resolved"] += 1
        elif status == "needs_review":
            results["needs_review"] += 1
        elif status in ("still_pending", "not_yet_time"):
            results["still_pending"] += 1
        # "error" is intentionally not counted in any bucket above --
        # poll_bet already prints a ❌ line for it; this loop doesn't need
        # a separate counter for something that's already loud in the logs.

        print()

    # ── P/L/Payout Completion: fill in rows that have a Result but are ──
    # ── missing P/L and Payout (most commonly: a manually-resolved      ──
    # ── NEEDS_REVIEW bet where Result was typed in by hand)             ──
    print(f"[trigger] Checking for rows with a Result but missing P/L/Payout...")
    try:
        unresolved_pl = load_unresolved_pl_bets(SHEET_TAB, COL)
    except Exception as e:
        print(f"[trigger] ❌ Failed to load unresolved P/L bets: {e}")
        unresolved_pl = []

    pl_results = {"completed": 0, "skipped": 0}

    if unresolved_pl:
        print(f"\n[trigger] {len(unresolved_pl)} row(s) need P/L/Payout completed...\n")
        for i, bet in enumerate(unresolved_pl, start=1):
            print(f"─ P/L Completion {i}/{len(unresolved_pl)} "
                  f"─────────────────────────")
            print(f"  BetID:  {bet['bet_id']}")
            print(f"  Result: {bet['result']}")
            status = complete_pl_payout(bet)
            pl_results[status] = pl_results.get(status, 0) + 1
            print()
    else:
        print("[trigger] ✅ No rows need P/L/Payout completed.\n")

    # ── Summary ──────────────────────────────────────────────────
    print("=" * 60)
    print(f"  Run complete: "
          f"{datetime.now(CENTRAL).strftime('%m/%d/%Y %I:%M:%S %p CT')}")
    print(f"  Resolved:       {results['resolved']}")
    print(f"  Still pending:  {results['still_pending']} (not final yet -- next scheduled run will check again)")
    print(f"  Needs Review:   {results['needs_review']} (check ESPN via Stats page, or resolve manually)")
    print(f"  P/L Completed:  {pl_results['completed']} (Result existed, P/L/Payout filled in)")
    print(f"  P/L Skipped:    {pl_results['skipped']} (missing Fee/Boost %, or already had a value)")
    print("=" * 60)


if __name__ == "__main__":
    main()