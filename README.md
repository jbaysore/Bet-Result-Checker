# Bet Result Checker

Automatically resolves settled sports bets in a Google Sheet: checks pending
bets against The Odds API, writes WIN/LOSS/PUSH/VOID results, and fills in
P/L and Payout using rules that account for bet category, per-book fees,
Profit Boost percentages, and Polymarket's share-based payout mechanic.

## What it does, in order

Each run (`trigger.py`) does four work passes over the `Bets` tab, then prints a summary:

1. **Resolve pending bets** — for every row with a blank `Result` and a game
   that's already started (+ a buffer), check the Odds API once for a final
   score. If found, resolve to WIN/LOSS/PUSH and write the result. If it's
   been more than ~6.5 hours since the game started with no final score,
   write `NEEDS_REVIEW` instead of leaving it stuck — this usually means
   the game was cancelled/postponed (Odds API can't see that), and is
   meant to be resolved via the app's "Check result" flow in the notification bell.

2. **Complete P/L and Payout** — for any row that has a `Result` but is
   missing both `P/L` and `Payout` (most commonly: a `NEEDS_REVIEW` bet you
   resolved manually by typing in the Result yourself), compute and fill
   those in using the same calculation as step 1. Never overwrites a row
   that already has a value in either column.

3. **Manual Payout → P/L** — for books in `MANUAL_PAYOUT_REQUIRED_BOOKS`
   (e.g. Kalshi), derive P/L from a manually-entered `Payout` column.

4. **Capture closing odds** — for started games with a blank `ClosingOdds`
   column, fetch a historical Odds API snapshot at 1 minute before recorded
   game start (same approach as the odds-tool Historical Odds Checker).
   Writes `ClosingOdds`, `DecimalClosingOdds`, and `CLV`. `VOID` results
   get `ClosingOdds = VOID` with no API call. Skips `NEEDS_REVIEW` rows.

   Supported markets: `h2h`, `spreads`, `alternate_spreads`, `totals`,
   `alternate_totals`, and `team_totals`. Legacy rows without a `Market Key`
   column value cascade across main then alternate markets; new bets logged
   from odds-tool stamp `Market Key` for a direct lookup. Team-total
   selections use the format `Braves Team Total Over 4.5`.

### ClosingOdds retry semantics

| `ClosingOdds` value | GitHub checker (~30 min) | API credits |
|---------------------|--------------------------|-------------|
| Blank | Retries (transient) | Spent each run |
| Error code (`SELECTION NOT FOUND`, `MANUAL ENTRY`, etc.) | Retries until exhausted (5 identical failures → `[closing-odds-exhausted]` in Notes) | Spent each run |
| Real odds, `VOID`, or **`N/A`** | Never touched (final) | None |

**`N/A` is the “stop trying” signal.** Rows you dismissed in the odds-tool bell stay quiet forever unless you clear them manually or run the one-shot retry script.

### Retry N/A rows (`scripts/retry_closing_odds.py`)

For rows marked `N/A` that might price now (e.g. after the alternate-market cascade fix):

```bash
# Preview buckets: skip / manual / retry
python scripts/retry_closing_odds.py --all-na

# Retry only the retry bucket once; failures restore N/A (no cron spam)
python scripts/retry_closing_odds.py --write --all-na --include-blank

# Stamp Market Key after success only (fetch always cascades main → alternate)
python scripts/retry_closing_odds.py --write --all-na --backfill-market-key

# Single row; --force overrides skip/manual buckets
python scripts/retry_closing_odds.py --write --bet-id 42 --force

# Surface failures in the bell instead of restoring N/A (cron will re-try)
python scripts/retry_closing_odds.py --write --all-na --leave-error
```

Requires the same `.env` / `GOOGLE_APPLICATION_CREDENTIALS` as `trigger.py`. After success, use odds-tool **Fix Decimal & CLV** if derived columns are still blank.

## Promotion Updater

After bet resolution finishes, the same workflow run also executes
`promo_trigger.py`, which finalizes **Pending** rows on the **Promotions**
tab. It reads linked **Bets** rows by Promo ID and, when conditions are met,
writes **Qualifying Cost**, **Status** (`Realized` or `Unused`), **Realized
Date**, and **Realized Amount**. Supported types: Bonus Bet, Profit Boost,
Deposit Bonus, Insurance Bet, and Profit Boost (Daily Until Win).

Promos depend on settled bet results, so running the promotion updater
immediately after the bet checker in the same workflow is intentional — no
separate cron job is needed.

## How it's triggered

This runs as a GitHub Actions workflow (`.github/workflows/bet-result-checker.yml`),
**not** on a GitHub-native cron schedule -- it's set to `workflow_dispatch`
(manual/API trigger only) and is triggered externally every 30 minutes by
[cron-job.org](https://cron-job.org), which calls GitHub's REST API to fire
the workflow. Each triggered run executes **both** `trigger.py` and
`promo_trigger.py`. Closing odds are captured inside `trigger.py` (pass 4)
— you no longer need a separate Betting-Tracker repo for that.

A separate workflow (`.github/workflows/promotion-updater.yml`) exists only
for manually re-running the promotion updater on its own from the Actions tab.

There used to be a Cloud Run Job + Cloud Scheduler version of this (Docker-based).
It's been fully retired in favor of this GitHub Actions setup -- no GCP
infrastructure, no Docker, is needed to run or deploy this anymore.

### Why it doesn't sleep or loop internally

Each run does **exactly one check per pending bet, then exits** -- it never
waits or retries within a single execution. The external 30-minute trigger
*is* the retry mechanism. This was a deliberate redesign (see git history
on `poller.py`): the original version slept between retries inside the
process, which meant a single run could take hours and would reliably get
killed by whatever was running it, while a brand new run started in
parallel every 30 minutes anyway, re-reading the same bets from scratch.

## Files

| File | Purpose |
|---|---|
| `trigger.py` | Entry point for bet resolution. Loads pending bets, calls `poller.py` for each, runs P/L completion, manual-payout P/L, and closing odds passes. |
| `closing_odds.py` | Historical Odds API client for closing-line capture (ports odds-tool `backfillClosingOdds.js`). |
| `promo_trigger.py` | Entry point for promotion finalization. Loads Pending promos, evaluates each via `promo_resolver.py`, writes Qualifying Cost and resolution fields. |
| `poller.py` | Core logic: `poll_bet()` (resolve one bet), `complete_pl_payout()` (fill in P/L/Payout for an already-resolved bet). |
| `promo_resolver.py` | Pure decision logic for whether a Pending promo can be finalized and with what Realized Amount. |
| `tests/` | pytest suite for `resolver.py` and `promo_resolver.py` (run via `pytest`). |
| `resolver.py` | Pure calculation functions: `resolve()` (WIN/LOSS/PUSH/VOID from a final score), `calculate_pl_and_payout()` (the actual money math, category/fee/boost/Polymarket-aware). |
| `sheets_reader.py` | All reads from Google Sheets (pending bets, unresolved P/L rows, pending promos, Book Settings' fee policies). |
| `sheets_writer.py` | All writes to Google Sheets (`write_result`, `write_pl_payout`, `write_closing_odds`, promo fields), each with a Promo ID / BetID safety check before writing. |
| `config.py` | Constants, column mapping, environment variable loading. |
| `sources/odds_api.py` | The Odds API client used to fetch final scores. |

## Required GitHub secrets

Set under repo Settings → Secrets and variables → Actions:

| Secret | Value |
|---|---|
| `SERVICE_ACCOUNT_JSON` | Full contents of the Google service account JSON key |
| `ODDS_API_KEY` | Your The Odds API key, just the raw key string |
| `SHEET_ID` | The target spreadsheet's ID (from its URL) |

`SHEET_TAB` is set directly in the workflow file (`Bets`), not a secret,
since it's not sensitive.

## Local development

```
pip install -r requirements.txt
```

Create a local `.env` file (never committed -- see `.gitignore`) with:

```
SHEET_ID=...
ODDS_API_KEY=...
GOOGLE_APPLICATION_CREDENTIALS=service_account.json
```

`service_account.json` (also never committed) should be the same service
account key used in the `SERVICE_ACCOUNT_JSON` GitHub secret, saved as a
local file instead of an env var for local runs.

Then: `python trigger.py` and/or `python promo_trigger.py`

## Tests

Unit tests cover P/L math (`resolver.py`) and promo finalization logic
(`promo_resolver.py`) — no Google Sheets or API credentials required.

```
pip install -r requirements.txt -r requirements-dev.txt
pytest
```

GitHub Actions runs `pytest` before each scheduled bet-checker run.

## Dependencies the Bets tab needs

For this to function correctly, the `Bets` tab needs (in addition to the
standard columns): a `Fee` column (per-bet fee, required before any bet's
P/L can be calculated -- the tool refuses to guess $0 if it's blank), and
a `Book Settings` tab (`Book | Refunds Fee On Void | Fee Before Odds`)
describing each book's fee behavior. Both are populated automatically by
the Log Bet Wizard the first time a new book is used; existing books'
settings are entered once, manually, the first time.
