import sys
from datetime import datetime
import pytz

from sheets_reader import load_pending_promotions, load_bets_by_promo_id, get_book_fee_before_odds
from sheets_writer import write_promo_qualifying_cost, write_promo_resolution
from promo_resolver import evaluate_promo
from config import (
    SHEET_TAB, PROMO_TYPE_PROFIT_BOOST, PROMO_TYPE_PROFIT_BOOST_DAILY,
    PROMO_TYPE_ODDS_BOOST, BET_CATEGORY_PROFIT_BOOST, BET_CATEGORY_ODDS_BOOST,
)

CENTRAL = pytz.timezone("America/Chicago")


def main():
    print("=" * 60)
    print(f"  Promotion Updater")
    print(f"  Started: {datetime.now(CENTRAL).strftime('%m/%d/%Y %I:%M:%S %p CT')}")
    print("=" * 60)

    today = datetime.now(CENTRAL).date()

    # ── Load pending promotions ──────────────────────────────────
    print(f"[promo_trigger] Loading Pending promotions...")
    try:
        pending_promos = load_pending_promotions()
    except Exception as e:
        print(f"[promo_trigger] ❌ Failed to load Promotions tab: {e}")
        sys.exit(1)

    if not pending_promos:
        print("[promo_trigger] ✅ No Pending promotions found.")
        print("=" * 60)
        return

    print(f"[promo_trigger] {len(pending_promos)} Pending promotion(s) found.")

    # ── Load every linked Bets row once, grouped by Promo ID ────
    # Reading the whole Bets tab once and grouping in memory, rather than
    # a separate Sheets API call per promo, keeps this cheap regardless
    # of how many promos are Pending in a given run.
    print(f"[promo_trigger] Loading linked bets from '{SHEET_TAB}' tab...")
    try:
        bets_by_promo = load_bets_by_promo_id(SHEET_TAB)
    except Exception as e:
        print(f"[promo_trigger] ❌ Failed to load Bets tab: {e}")
        sys.exit(1)

    # ── Cache Book Settings' Fee Before Odds flag per book ───────
    # Only ever needed for Profit Boost and Profit Boost (Daily Until Win)
    # (token value requires recomputing an unboosted P/L baseline using
    # the SAME fee mechanic the real bet used) -- looked up lazily, once
    # per book actually encountered, rather than reading every book up front.
    fee_before_odds_cache = {}

    def get_fee_before_odds_cached(book: str) -> bool:
        if book not in fee_before_odds_cache:
            fee_before_odds_cache[book] = get_book_fee_before_odds(book)
        return fee_before_odds_cache[book]

    # ── Evaluate each pending promo ───────────────────────────────
    results = {"finalized": 0, "qualifying_cost_filled": 0, "still_pending": 0,
               "not_implemented": 0, "write_failed": 0}

    for i, promo in enumerate(pending_promos, start=1):
        promo_id = promo["promo_id"]
        promo_type = promo["promo_type"]

        print(f"\n─ Promo {i}/{len(pending_promos)} "
              f"─────────────────────────────────────────")
        print(f"  Promo ID:   {promo_id}")
        print(f"  Promo Name: {promo['promo_name']}")
        print(f"  Type:       {promo_type}")
        print(f"  Book:       {promo['book']}")

        linked_bets = bets_by_promo.get(promo_id, [])
        print(f"  Linked bets: {len(linked_bets)}")

        fee_before_odds_lookup = None
        if promo_type in (PROMO_TYPE_PROFIT_BOOST, PROMO_TYPE_PROFIT_BOOST_DAILY,
                          PROMO_TYPE_ODDS_BOOST):
            # Build {book: bool} only for the books actually appearing among
            # this promo's linked reward bets -- normally just one (the promo's
            # own book), but built from the bets themselves rather than assumed,
            # in case a reward bet got logged against a different book. Profit
            # Boost needs it to recompute an UNBOOSTED baseline; Odds Boost to
            # recompute the ORIGINAL-ODDS baseline -- both must use the same fee
            # mechanic the real bet used.
            reward_categories = {BET_CATEGORY_PROFIT_BOOST, BET_CATEGORY_ODDS_BOOST}
            books_in_play = {
                b["book"] for b in linked_bets
                if b["bet_category"] in reward_categories and b["book"]
            }
            fee_before_odds_lookup = {b: get_fee_before_odds_cached(b) for b in books_in_play}

        verdict = evaluate_promo(promo, linked_bets, today, fee_before_odds_lookup)

        if verdict.get("not_implemented"):
            print(f"  ⏭️  {verdict['log'][0]}")
            results["not_implemented"] += 1
            continue

        for line in verdict["log"]:
            print(f"    {line}")

        wrote_something = False

        if verdict["qualifying_cost_fill"] is not None:
            ok = write_promo_qualifying_cost(
                promo["row_idx"], promo_id, verdict["qualifying_cost_fill"]
            )
            if ok:
                results["qualifying_cost_filled"] += 1
                wrote_something = True
            else:
                results["write_failed"] += 1

        if verdict["finalize"] is not None:
            ok = write_promo_resolution(
                promo["row_idx"], promo_id,
                status=verdict["finalize"]["status"],
                realized_date=today.isoformat(),
                realized_amount=verdict["finalize"]["realized_amount"],
            )
            if ok:
                results["finalized"] += 1
                wrote_something = True
            else:
                results["write_failed"] += 1

        if not wrote_something and verdict["finalize"] is None:
            results["still_pending"] += 1

    # ── Summary ──────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print(f"  Run complete: {datetime.now(CENTRAL).strftime('%m/%d/%Y %I:%M:%S %p CT')}")
    print(f"  Finalized:               {results['finalized']} (Realized or Unused)")
    print(f"  Qualifying Cost filled:  {results['qualifying_cost_filled']}")
    print(f"  Still pending:           {results['still_pending']} (nothing to do yet)")
    print(f"  Type not yet automated:  {results['not_implemented']}")
    print(f"  Write failures:          {results['write_failed']}")
    print("=" * 60)


if __name__ == "__main__":
    main()
