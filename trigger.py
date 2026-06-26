import sys
from datetime import datetime, timezone
import pytz
from sheets_reader import load_pending_bets, load_unresolved_pl_bets, load_manual_payout_pending_pl_bets
from poller import poll_bet, complete_pl_payout, complete_manual_payout_pl, _parse_game_datetime
from config import SHEET_TAB

CENTRAL = pytz.timezone("America/Chicago")


def main():
    print("=" * 60)
    print(f"  Bet Result Checker")
    print(f"  Started: {datetime.now(CENTRAL).strftime('%m/%d/%Y %I:%M:%S %p CT')}")
    print("=" * 60)

    # ── Load pending bets ────────────────────────────────────────
    print(f"[trigger] Loading pending bets from '{SHEET_TAB}' tab...")
    try:
        pending = load_pending_bets(SHEET_TAB)
    except Exception as e:
        print(f"[trigger] ❌ Failed to load pending bets: {e}")
        sys.exit(1)

    if not pending:
        print("[trigger] ✅ No pending bets found.")

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

    # ── Process each ready bet ───────────────────────────────────
    results = {"resolved": 0, "still_pending": 0, "needs_review": 0}

    if ready:
        print(f"\n[trigger] Processing {len(ready)} bet(s)...\n")

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
            # cron-job.org-triggered GitHub Actions run IS the retry
            # mechanism now, not an in-process wait).
            #
            # Wrapped in try/except (2026-06-26): a transient Sheets API
            # error (e.g. a 429 read-quota hit) used to propagate all the
            # way up and crash the ENTIRE run, losing the P/L Completion
            # and Manual Payout passes below even though other bets in
            # this loop had already succeeded. One bet's bad luck with a
            # rate limit shouldn't cost every other bet's progress for
            # this run -- the next scheduled run will retry this one.
            try:
                status = poll_bet(bet)
            except Exception as e:
                print(f"[trigger] ❌ BetID {bet['bet_id']}: unexpected error -- {e}. "
                      f"Continuing with the next bet; this one will be retried next run.")
                status = "error"

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
        unresolved_pl = load_unresolved_pl_bets(SHEET_TAB)
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
            try:
                status = complete_pl_payout(bet)
            except Exception as e:
                print(f"[trigger] ❌ BetID {bet['bet_id']}: unexpected error -- {e}. "
                      f"Continuing with the next row; this one will be retried next run.")
                status = "skipped"
            pl_results[status] = pl_results.get(status, 0) + 1
            print()
    else:
        print("[trigger] ✅ No rows need P/L/Payout completed.\n")

    # ── Manual Payout → P/L Derivation: for books where the automated   ──
    # ── odds-based formula can't be trusted (config.MANUAL_PAYOUT_      ──
    # ── REQUIRED_BOOKS, e.g. Kalshi), this picks up rows where the user ──
    # ── has manually typed in the real Payout from their account, and   ──
    # ── derives P/L from it.                                            ──
    print(f"[trigger] Checking for rows with a manually-entered Payout awaiting P/L...")
    try:
        manual_payout_pl = load_manual_payout_pending_pl_bets(SHEET_TAB)
    except Exception as e:
        print(f"[trigger] ❌ Failed to load manual-Payout P/L bets: {e}")
        manual_payout_pl = []

    manual_pl_results = {"completed": 0, "skipped": 0}

    if manual_payout_pl:
        print(f"\n[trigger] {len(manual_payout_pl)} row(s) have a manual Payout awaiting P/L...\n")
        for i, bet in enumerate(manual_payout_pl, start=1):
            print(f"─ Manual Payout→P/L {i}/{len(manual_payout_pl)} "
                  f"─────────────────────────")
            print(f"  BetID:  {bet['bet_id']}")
            print(f"  Book:   {bet['book']}")
            print(f"  Payout: {bet['payout']}")
            try:
                status = complete_manual_payout_pl(bet)
            except Exception as e:
                print(f"[trigger] ❌ BetID {bet['bet_id']}: unexpected error -- {e}. "
                      f"Continuing with the next row; this one will be retried next run.")
                status = "skipped"
            manual_pl_results[status] = manual_pl_results.get(status, 0) + 1
            print()
    else:
        print("[trigger] ✅ No rows have a manual Payout awaiting P/L.\n")

    # ── Summary ──────────────────────────────────────────────────
    print("=" * 60)
    print(f"  Run complete: "
          f"{datetime.now(CENTRAL).strftime('%m/%d/%Y %I:%M:%S %p CT')}")
    print(f"  Resolved:       {results['resolved']}")
    print(f"  Still pending:  {results['still_pending']} (not final yet -- next scheduled run will check again)")
    print(f"  Needs Review:   {results['needs_review']} (check ESPN via Stats page, or resolve manually)")
    print(f"  P/L Completed:  {pl_results['completed']} (Result existed, P/L/Payout filled in)")
    print(f"  P/L Skipped:    {pl_results['skipped']} (missing Fee/Boost %, or already had a value)")
    print(f"  Manual Payout→P/L Completed: {manual_pl_results['completed']} (derived from a manually-entered Payout)")
    print(f"  Manual Payout→P/L Skipped:  {manual_pl_results['skipped']}")
    print("=" * 60)


if __name__ == "__main__":
    main()
