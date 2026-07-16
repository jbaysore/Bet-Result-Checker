# CLV Accuracy Plan — Actual-Start-Aware Closing Odds

**Created:** 2026-07-15 · **Rev 3:** 2026-07-15 (second external review pass)
**Status:** Draft — pending Josh's review
**Scope:** Bet-Result-Checker (worker, historical importer, Kalshi path) AND odds-tool (scanner archive, Bets-page recovery, CLV consumers). Historical repair of ~400 rows is **flag-only**; re-pull is a separate decision after the audit report.

---

## 1. The problem

Every closing-odds capture path keys off a **scheduled** start time (TOA's `commence_time` estimate). Games frequently start earlier; once live, prices are in-play and CLV against them is garbage and systematically biased.

**Five** affected paths (verified against source):

| # | Path | Where | Mechanism |
|---|---|---|---|
| 1 | Proactive worker | `closing_capture_worker.py` | T-10/5/1 ladder vs scheduled `Commence UTC`; latest sample wins — early start ⇒ live T-1 finalizes. |
| 2 | Historical importer | `closing_odds.py:709` via `trigger.py:202` | Auto-fills any blank ClosingOdds at `scheduled − 1 min`. Poisoned the ~400 rows. |
| 3 | Scanner archive | `scannerDb.js` `hardExpireCommenced()` | Closing from `price_latest` at scheduled commence; upsert reactivates expired rows (`:171-179`); scan filters only by scheduled commence (`scanner.js:968`) → live prices as "pregame" opportunities. |
| 4 | Bets-page recovery | `closingOddsRecovery.js:143` | Snapshots at scheduled −1/−6/−11 — re-triggerable by hand. |
| 5 | Kalshi closing | `sources/kalshi.py:113` | Candle window ends at scheduled start; Kalshi trades in-play. |

## 2. The fix

TOA `/scores` as a **true-start signal** (confirmed: `scores`/`last_update` null until in-progress; ~30s updates; 1 credit/sport without `daysFrom`; per-sport coverage; `commence_time` never documented to update), wrapped in a state model that separates **two different questions** the sheet previously conflated: *was the price pregame?* and *is it a good closing price?*

### 2.1 Start-safety state (per event; worker AND scanner)

- **`VERIFIED_PREGAME`** — event matched by id in a fresh (within-TTL) scores response, no live signal. Sampling allowed.
- **`VERIFIED_LIVE`** — live signal present. Finalize from a safely earlier sample; stop sampling.
- **`UNKNOWN`** — scores error, stale response, missing/ambiguous event, or uncovered sport. No sampling past scheduled commence; anything written under UNKNOWN is marked accordingly.

### 2.2 Two provenance dimensions on the Bets row

Written atomically with ClosingOdds/CLV by **every** path (guarded single write — if the provenance columns are missing from the sheet, the worker must NOT write ClosingOdds/CLV alone):

- **`Start Status`** (safety): `VERIFIED` / `UNVERIFIED` / `UNKNOWN` / `LEGACY_UNAUDITED`
- **`Closing Quality`** (is it a close?): `VERIFIED_CLOSE` / `SAFE_BUT_EARLY` / `STALE` / `PROVISIONAL` / `MANUAL`
- **`Closing Source`**: `worker-live` / `historical` / `recovery-historical` / `recovery-closing-capture` / `recovery-scanner-archive` / `recovery-kalshi` / `kalshi-candle` / `manual` (recovery variants have different reliability — don't collapse them)
- **`Closing Observed At`**, **`Start Detected At`**, **`Actual Start`** (+ source/confidence, see Phase 2)

**Quality assignment at finalize:**
- Live detected + latest verified-pregame sample within both the safety margin (≤ `detected_at − 75s`) **and** a max-age window (sample ≤ ~5 min old, tunable) → `VERIFIED_CLOSE`.
- Verified sample exists but is older than max-age (API failures left a gap) → `STALE`. A T-15 sample is pregame-safe but is not a close — it must not become trusted CLV.
- Cap reached (commence + 45 min) still `VERIFIED_PREGAME` → write latest sample as `SAFE_BUT_EARLY` (pregame-safe, but an arbitrary cap price, and the game may be postponed — never `VERIFIED_CLOSE`). Result-checker's void flow handles postponements.
- Written under `UNKNOWN` safety (uncovered sports, ambiguous events) → `PROVISIONAL`.
- **Pooled CLV consumers use `Closing Quality = VERIFIED_CLOSE` only** (plus the legacy contract, §Phase 4). Everything else stays visible on the row, excluded from aggregates by default.

**Quote timestamps, not just sampling timestamps.** `sampled_at` proves when *we* asked, not when the *book* last priced. TOA returns `bookmakers[].last_update` / `markets[].last_update` and the historical endpoint returns the snapshot's `timestamp` — all currently discarded at extraction. Each sample stores `fetched_at` + `book_last_update` (+ `snapshot_at` for historical); quality checks enforce book-quote freshness too (a T-1 fetch of a quote the book last touched at T-20 is not a verified close → `STALE`).

### 2.3 Legacy cutover (no fail-open blanks)

- Before enabling the new writers: one-time backfill stamps every existing Bets row `Start Status = LEGACY_UNAUDITED`.
- After cutover (tracked by an explicit migration marker, not inferred from empty cells): **blank Start Status/Closing Quality means UNKNOWN and is excluded** — a new row whose provenance write failed can't silently stay pooled. (The guarded single write makes this near-impossible anyway; blank-means-excluded is the backstop.)

---

## Phase 0 — Shadow verification (persistent, no-write)

Not throwaway probes: a **shadow monitor** running over at least one representative slate (ideally 2–3 days: MLB day with staggered starts + soccer + WNBA), logging state transitions and would-have-finalized decisions, writing nothing.

1. Scores flip semantics per sport (0-0 at ~true start vs first score); detection lag vs MLB statsapi first-pitch → sets the margin per sport.
2. Coverage classification per sport key — distinguishing **unsupported** vs **off-season** vs **temporarily empty** vs **transient error** (a static `SCORES_UNSUPPORTED` list goes stale; classification is re-probed on a daily TTL, transient errors map to UNKNOWN not unsupported).
3. Event id stability across `/scores`, `/events`, odds endpoints.
4. MLB actual-start field: verify `GET /api/v1.1/game/{gamePk}/feed/live` → `gameData.gameInfo.firstPitch` (extractor must be built; `mlb_statsapi.py` has schedule/boxscore only).
5. Historical snapshot granularity on our plan (5 vs 10 min) → audit buckets.
6. Pinnacle region + per-market coverage.
7. Bookmaker `last_update` behavior near close (how fresh are book quotes in the final 10 min?) → sets the staleness threshold in §2.2.

Exit: findings + chosen margins/slot count appended here.

**Implementation status (2026-07-15):** the shadow monitor is built and green (355 tests). No sheet writes; observations go to a JSONL log for later analysis.
- `sources/mlb_statsapi.py` — added `get_game_feed_live()` + `first_pitch()` / `scheduled_start()` parsers (v1.1 feed/live → `gameData.gameInfo.firstPitch`). Item 4.
- `sources/scores_live.py` — fresh no-cache `/scores` client (no `daysFrom`), `event_live_state()` (PREGAME/LIVE/COMPLETED), coverage classifier + daily-TTL `CoverageCache` (transient ≠ unsupported), per-call credit headers. Existing `odds_api._fetch_scores` untouched. Items 1, 2.
- `shadow_start_monitor.py` — persistent loop: per-event start-transition detection, MLB detection-lag vs firstPitch, `simulate_finalize()` would-have-finalized decisions (VERIFIED_CLOSE / STALE / MISSED / SAFE_BUT_EARLY), credit accounting. Opt-in paid probes default OFF: `SHADOW_BOOK_FRESHNESS` (item 7), `SHADOW_EVENTS_STABILITY` (item 3), `SHADOW_PINNACLE_PROBE` (item 6), `SHADOW_HISTORICAL_PROBE` (item 5). Baseline is scores-only (1 credit/sport/poll).
- Run: `python shadow_start_monitor.py` (tunables: `SHADOW_POLL_SECONDS`, `SHADOW_MARGIN_SECONDS`, `SHADOW_MAX_AGE_SECONDS`, `SHADOW_SPORTS`, `SHADOW_LOG_PATH`). Deploy alongside the capture worker for 2–3 representative slates, then fill the numbers below from the JSONL.
**Phase 0 findings (2026-07-15, one-shot probes — ~20 credits):**

| Item | Result | Status |
|---|---|---|
| 5. Snapshot interval | **Exactly 5 minutes** (`timestamp` 01:55:37, `previous` 01:50:37, `next` 02:00:37) | **MEASURED** |
| 6. Pinnacle | Region **`eu`** confirmed; `last_update` present at book AND market level; quote was 20s fresh pregame | **MEASURED** |
| 3. Event id stability | MLB `/scores` ids == `/events` ids (2/2, zero mismatches; small sample — All-Star break) | Measured, small n |
| 2. Coverage | All six sport keys (MLB, WNBA, MLS, World Cup, MMA, boxing) return HTTP 200 with `scores` + `last_update` fields, 1 credit each. **No sport is HTTP-unsupported.** Whether `scores` flips at true start is still unproven per sport | Partially measured |
| 4. MLB firstPitch | `schedule?hydrate=gameInfo` returns `gameInfo.firstPitch` — **164/164 final games** over 14 days, zero missing. No feed/live call needed (cheaper than planned) | **MEASURED** |
| MLB start-delta distribution (bonus) | 164 games: median **+60s late**, p90 +300s, earliest −60s, latest +7200s. **0.6% started early at all; 0% early by ≥2 min** | **MEASURED** |

**Implication of the start-delta finding:** MLB games essentially never beat their *official* schedule — they start on time or late. So MLB contamination, if present, most likely enters via (a) the sheet's recorded time being **TOA's** commence estimate, which can deviate from the official schedule (doubleheader game 2 placeholders, reschedules), (b) other sports — MMA/boxing card timing is inherently estimate-based and is now the prime suspect, or (c) *late* starts making `scheduled − 1` samples merely early rather than live. Phase 6's audit compares firstPitch against the **sheet-recorded** time (the actual poison vector), which will settle this. The late-start skew (median +60s, p90 +5min) also independently validates the worker's keep-sampling-past-commence behavior.

**Adopted working constants (conservative defaults, parameterized — shadow monitor refines them in production alongside Phase 1):**
- Detection safety margin: **90s** (scores ~30s update + 30s poll + jitter) — pending live measurement
- Sampling cadence: **90s**; VERIFIED_CLOSE max-age: **5 min**; slots: **4** (= `ceil((90+2×90)/90)+1`, tolerates one missed cycle)
- Book-quote staleness threshold: **10 min** — pending live measurement (single Pinnacle data point: 20s)
- Audit geometry: interval = 5 min ⇒ `LIKELY_SUSPECT` when `actual_start < scheduled − 6 min`
- Flip-semantics trust: MLB/WNBA/MLS/World Cup enabled at launch; **MMA/boxing capture as `PROVISIONAL`** until the shadow monitor proves their `scores` flip at fight start (coverage exists, timing unproven, and they're the highest early-start risk)

**Still live-run-only (shadow monitor, concurrent with Phase 1 — does NOT block it):** per-sport scores-flip lag (tightens the 90s margin), book-freshness distribution in the final 10 min, MMA/boxing flip semantics, larger-n event-id stability.

## Phase 1 — Worker redesign + minimal consumer guard (atomic deploy)

1. **Dedicated live-status client** — the existing `_fetch_scores` (`sources/odds_api.py:162`) has a process-lifetime cache and `daysFrom=3`; unusable. New fetcher: no `daysFrom`, ~25s TTL, explicit error state → UNKNOWN. Existing helper untouched.
2. **Event id plumbing, end to end** — `card.eventId` exists in LogBetWizard's card state but is currently dropped (used only as a React key); carry it through wizard state → `/api/bets` payload → queue column. Worker backfills missing ids via free `/events`; ambiguous (doubleheaders) → UNKNOWN, never guess.
3. **Rolling sample history, sized from Phase 0** — slots = `ceil((max_sport_margin + max_tolerated_gap) / cadence) + 1` (3 at 90s cadence with a 75s margin; NOT hardcoded before Phase 0 numbers exist). Each slot: price, fetched_at, book_last_update, start-state. **Escape hatch:** if Phase 0 pushes any sport's margin past ~180s, switch to an append-only samples tab instead — rolling slots are only valid while margin < 2×cadence.
4. **Finalize per §2.1/§2.2** with the guarded single write.
5. **Interim contamination guard** — `load_bets_needing_closing_odds()` skips rows whose Start Status marks them early-start/unknown-pending, so `trigger.py` can't re-poison Phase 1's FALLBACK rows before Phase 2 lands.
6. **Uncovered sports**: ladder to scheduled commence only; finalize `Start Status = UNVERIFIED`, `Closing Quality = PROVISIONAL`.
7. **Staged queue migration (not "lockstep")** — both repos hard-require exact 25 headers and odds-tool hardcodes `A1:Y1`, so simultaneous deploys still race. Sequence: (a) deploy both readers/writers tolerant of "required 25-column prefix + optional appended columns"; (b) append the new columns; (c) enable new behavior; (d) later tighten to the full schema. No tab rename (`closingOddsRecovery.js:373` reads this tab).
8. **Minimal consumer guard ships in the same deploy** (not Phase 4): Stats/Coach pool only rows whose Closing Quality is blank-legacy (`LEGACY_UNAUDITED` under the §2.3 contract) or `VERIFIED_CLOSE`. Without this, Phases 1–3 write excluded-by-intent values that the client still pools for days.
9. **Legacy backfill + migration marker** per §2.3, run before enabling.
10. **Tests**: pre-margin sample selection; newest-inside-margin falls back a slot; STALE when gap exceeds max-age; SAFE_BUT_EARLY at cap; UNKNOWN never samples past commence; guarded write refuses without provenance columns; prefix-tolerant header validation both repos; blank-status exclusion post-cutover.

## Phase 2 — Historical/recovery/Kalshi hardening

**Actual-start resolvers (build new; per-sport adapters with fixtures** — event matching, timezones, doubleheaders, delays, postponements, missing timestamps; `config.py:298` confirms no reusable ESPN plumbing exists): MLB statsapi first-pitch; ESPN summary first-play wallclock for WNBA/MLS/soccer. Align structure with the resolver-expansion plan.

**Resolver architecture (decided):** the checker is the single resolver. It persists authoritative **`Actual Start`** (+ `Actual Start Source`, confidence) to the Bets row; odds-tool consumes it from the sheet. No parallel JS adapters, no new HTTP service. If `Actual Start` is blank, recovery cannot produce a `VERIFIED_CLOSE` write.

- **Historical importer**: when actual start is confidently resolved, snapshot at **`actual_start − margin` in either direction** — a game starting 25 min late should be priced near its real close, not the scheduled time (mirrors the worker's late-start behavior). Quality `VERIFIED_CLOSE`. Fall back to `min(actual, scheduled) − 1 min` **only** when confidence is insufficient → `SAFE_BUT_EARLY`, never a verified close.
- **Bets-page recovery**: same policy, reading `Actual Start` from the sheet. The preview/confirm fingerprint (`closingOddsRecovery.js:243`) already hashes snapshot timestamps (so a changed actual start changes it), but must additionally include resolver version + start status so any resolution change between preview and confirm forces a new preview. Writes carry the granular `recovery-*` source values.
- **Kalshi candles**: window ends at `min(actual_start, scheduled)`; resolved-late starts may extend to actual; unresolved → `SAFE_BUT_EARLY`/`PROVISIONAL`.
- **Parlays** (importer/recovery only — worker queue excludes them): each leg resolves its own event + actual start. Row-level columns are a **summary only**; per-leg provenance (event id, actual start, status, quality, snapshot used) is stored as structured JSON on the ClosingCapture/audit record. Combined `Closing Quality = VERIFIED_CLOSE` **only when every leg is verified**; one unverified leg makes the row `SAFE_BUT_EARLY`/`PROVISIONAL` (worst leg wins).

## Phase 3 — Scanner archive fix (odds-tool)

- Same live-status client semantics, per active sport.
- **Durable `event_lifecycle` tombstone** (upsert reactivation at `scannerDb.js:171-179` makes expiry insufficient): events with `detected_live_at` are excluded from price recording, opportunity computation, additional-market processing, and upsert — checked at the top of the cycle. **Retention & rescheduling defined:** rows purge at `commence + 7 days`; if an event id reappears with a commence_time moved materially into the future (> 6h — rescheduled/reused event), the tombstone clears rather than suppressing a valid future game.
- **Definite observation schema** (`price_changes` records changes, not observations; `price_latest` is overwritten — neither can prove a quote was alive near a historical cutoff): new **`closing_samples`** table — `(quote key, odds, observed_at, book_last_update)` — recorded each cycle only for events inside T-20 → cutoff. Closing stamp = latest closing_sample ≤ `detected_at − margin`, subject to the same freshness rules; else `closing_skip_reason = stale_quote`. Bounded by the window; prune with the tombstone.
- `hardExpireCommenced()` stays as fallback; freshness against `min(detected_live_at, commence)`.
- Tests: reactivation blocked by tombstone; tombstone clears on reschedule; stamp uses closing_samples not price_latest; stale-gap → skip reason.

## Phase 4 — Full consumer integration + legacy contract

Phase 1 ships the minimal guard; this phase completes it:

- StatsPage + Coach: excluded-count surfacing ("n excluded: unverified start / stale / provisional"), toggle to include, quality breakdown in Coach's CLV report.
- **Explicit legacy filtering contract (connects Phase 6 → consumers):**
  - `Start Status = LEGACY_UNAUDITED` and no `Start Audit` yet → pooled (pre-audit status quo).
  - After the audit: `Start Audit = SAFE` → pooled; `LIKELY_SUSPECT` / `INDETERMINATE` / `UNRESOLVABLE` → excluded by default.
  - Re-pulled-and-verified rows get `Start Status = VERIFIED`, `Closing Quality = VERIFIED_CLOSE` and pool normally.
- Recovery UI requires explicit confirmation to overwrite anything with quality below `VERIFIED_CLOSE`.

## Phase 5 — Pinnacle closing benchmark

**Recommendation stands: add Pinnacle-close CLV alongside same-book CLV; don't replace.**

- **Fetch shape:** featured markets (h2h/spreads/totals) batch **per sport per cycle** — one `/odds` call covers every tracked event; the per-event endpoint is only needed for additional markets (alternates, team totals). Cheaper than per-event fetching.
- **Contract:** exact event id, market family, full-game period (v1), exact point; Pinnacle point ≠ bet point → blank (no interpolation in v1).
- **Devig:** port `noVig.js` power devig to Python now with shared test vectors (JS/Python parity is a named test); two-way both sides; three-way (soccer incl. Draw) full devig. CLV = `decimal_taken / novig_pinnacle_close − 1`.
- **Reproducibility inputs live in ClosingCapture (decided):** opposite-side prices, no-vig probability, closing point, capture timestamp, book_last_update. Bets carries only `Pinnacle Close` + `Pinnacle CLV`. Consequence, accepted: reproducing a Pinnacle CLV requires retaining the ClosingCapture record — the queue tab becomes a permanent audit record, not a scratch queue (no row deletion).
- Same §2.1/2.2 state and margin rules; sparse where Pinnacle lacks the market.

## Phase 6 — Historical audit of the ~400 rows (flag-only)

`scripts/clv_start_audit.py`. Snapshot geometry (requested `scheduled − 1 min` ⇒ served snapshot ∈ `(scheduled − 1 − interval, scheduled − 1]`), but the interval is "approximately" regular and even a post-start snapshot can contain a book quote whose own `last_update` was pregame — so the free audit **never claims certainty**:

- **`SAFE`** — `actual_start > scheduled − 1 min` (strictly; boundary equality is NOT safe): every possible served snapshot predates the start.
- **`LIKELY_SUSPECT`** — `actual_start < scheduled − 1 min − interval`: every plausible snapshot is post-start. Not "confirmed" — reserved for a re-query proving the exact selected bookmaker quote's `last_update` was after actual start (that re-query is ~the cost of a re-pull, so it folds into the re-pull decision).
- **`INDETERMINATE`** — between, or boundary-equal.
- **`UNRESOLVABLE`** — no actual-start source.

Writes `Actual Start` + `Start Audit`; touches no CLV. Deliverable: delta distribution, bucket counts, aggregate CLV with/without each bucket → re-pull decision. Filtering consequences per Phase 4's contract.

---

## Sequencing

| Order | Phase | Why |
|---|---|---|
| 1 | Phase 0 | Margins, slot count, coverage classes, MLB field, snapshot interval, book-freshness threshold all come from here. Runs a few days as a shadow monitor. |
| 2 | Phase 1 | Stops the bleeding; ships atomically with legacy backfill, migration marker, importer guard, and the minimal consumer guard. |
| 3 | Phase 2 | Unblocks guarded rows; fixes importer, recovery, Kalshi; decides nothing new (resolver architecture already fixed here). |
| 4 | Phase 3 | Scanner tombstone + closing_samples. |
| 5 | Phase 4 | Full consumer surfacing + legacy contract. |
| 6 | Phase 6 | Audit (needs Phase 2 resolvers). |
| 7 | Phase 5 | Pinnacle — additive. |

## Credit budget & measurement

Window-union estimates (staggered slates keep a sport's scores polling alive for hours; ~10 samples on-time to ~40 at cap, × market cascade). Log `x-requests-last/used/remaining` per call from day one; per-day soft budget; degrade gracefully (cadence 90s→180s, then scores-only); review after the first full slate.

## New live-sheet changes (column backlog)

- **Bets**: `Start Status`, `Closing Quality`, `Closing Source`, `Closing Observed At`, `Start Detected At`, `Actual Start`, `Actual Start Source`, `Start Audit`, `Pinnacle Close`, `Pinnacle CLV`.
- **ClosingCapture**: appended columns (Event ID, sample slots × {price, fetched_at, book_last_update, state}, Start Detected At, per-leg/Pinnacle audit JSON). Tab becomes a permanent audit record.
- One-time: `LEGACY_UNAUDITED` backfill + migration marker.

## Open items

- [ ] Phase 0 findings; final margin/slot-count/staleness numbers.
- [ ] Samples-tab escape hatch trigger (any sport margin > ~180s).
- [ ] Re-pull decision after Phase 6 (LIKELY_SUSPECT/INDETERMINATE resolution).

---

## Implementation completed — 2026-07-15

The remaining plan was implemented across `Bet-Result-Checker-github` and
`odds-tool`. No production Google Sheet migration or historical audit was run
during implementation; those remain explicit rollout steps below.

### Bet-Result-Checker implementation

- Added the shared start/provenance policy in `closing_provenance.py`, including
  environment-tunable 90-second safety margin, 90-second cadence, four rolling
  sample slots, five-minute maximum sample age, ten-minute bookmaker staleness,
  and the 45-minute delayed-start cap.
- Rebuilt `closing_capture_worker.py` around fresh `/scores` state, durable
  rolling samples, Odds API event IDs, bookmaker timestamps, pre-margin sample
  selection, delayed-start sampling, UNKNOWN fail-closed behavior, permanent
  ClosingCapture audit records, and atomic ClosingOdds/CLV/provenance writes.
- Added per-call credit logging plus a daily soft budget: full cadence degrades
  to 180 seconds at 70% of the configured budget and to scores-only at the soft
  ceiling. Defaults remain overrideable through environment variables.
- Added `actual_start.py`: MLB uses the hydrated Stats API `firstPitch`; WNBA,
  MLS, and World Cup use ESPN summary first-play wallclock. Ambiguous events,
  missing timestamps, postponements, and unsupported sports remain unresolved
  rather than being guessed.
- Hardened `closing_odds.py`, `trigger.py`, the one-shot retry script, and the
  Kalshi candle path to use actual-start cutoffs and granular quality. A
  confidently resolved start can still be downgraded to `STALE` when the
  selected bookmaker quote timestamp is not fresh.
- Made closing writes fail closed at `sheets_writer.write_closing_odds()`:
  provenance is mandatory and all core provenance columns must exist. The
  reader also treats blank post-cutover provenance as UNKNOWN and excludes it.
- Added per-leg parlay start/quality auditing with worst-leg row summaries.
- Added `pinnacle_closing.py`: exact Odds API event ID, featured full-game
  market, exact point, two/three-way power devig, quote freshness, reproducible
  audit inputs, and separate Pinnacle Close/Pinnacle CLV outputs.
- Added `scripts/migrate_clv_provenance.py`: idempotently appends the Bets
  columns, stamps existing rows `LEGACY_UNAUDITED`, and records the required
  `clv-actual-start-v1` marker in `SchemaMigrations`. The Railway worker refuses
  to start until this marker exists.
- Added `scripts/clv_start_audit.py`: processes only `LEGACY_UNAUDITED` rows,
  writes Actual Start/source/confidence plus Start Audit, never changes
  ClosingOdds or CLV, and reports bucket counts, start deltas, bucket CLV, and
  aggregate CLV with each bucket removed.
- Preserved the shadow monitor as a no-sheet-write JSONL observer and updated
  the README with the new worker and migration contract.

### odds-tool implementation

- Carried Odds API Event ID through scanner cards, Log Bet state, `/api/bets`,
  the Bets row, and ClosingCapture.
- Made ClosingCapture tolerate the immutable legacy 25-column prefix plus the
  appended rolling-sample/provenance schema and expanded reads beyond `A:Y`.
- Added the fresh scanner `/scores` client, `event_lifecycle` tombstones, and
  bounded `closing_samples` observations with bookmaker timestamps.
- Live/completed events are blocked from price recording, opportunity
  computation, additional-market processing, notification, and reactivation.
  A tombstone clears only for a material reschedule more than six hours later.
- Delayed events explicitly reported PREGAME no longer hard-expire merely
  because scheduled commence passed. Scheduled-time expiry remains the safe
  fallback when scores state is unavailable.
- Hardened Bets-page recovery with actual-start snapshot targets, resolver
  version/start status in the authorization fingerprint, exact event-ID-first
  matching, quote freshness, granular recovery sources, worst-leg quality,
  per-leg audit persistence, and mandatory confirmation below
  `VERIFIED_CLOSE`.
- Added the canonical CLV inclusion contract to Stats and Coach. Verified
  closes pool normally; legacy rows pool only before audit or when audited
  SAFE; stale/provisional/indeterminate rows are excluded by default and their
  counts are surfaced with an include-excluded toggle.
- Added a shared set of Python/JavaScript power-devig parity vectors.

### Validation completed

- Bet-Result-Checker: **374 pytest tests passed**.
- odds-tool: **552 Node tests passed**.
- Focused lifecycle/recovery/quality tests: **55 passed**.
- odds-tool production Vite build passed (existing large-chunk warning only).
- Final Python syntax compilation and checker `git diff --check` passed.
- Temporary pytest dependencies/caches created for validation were removed.

## Required rollout order

This order keeps the old 25-column queue contract from racing the new appended
schema and keeps every closing write fail-closed during cutover.

### 1. Back up and pause automation

1. In Google Sheets, make a copy of the tracking spreadsheet.
2. Temporarily pause the Railway `closing_capture_worker.py` service.
3. Temporarily pause the external cron-job.org trigger for the GitHub Actions
   checker until the migration and restarts below are complete.

### 2. Validate the local code one final time

Bet-Result-Checker:

```powershell
cd C:\Users\Joshua\APIs\Bet-Result-Checker-github
py -m pip install -r requirements.txt -r requirements-dev.txt
py -m pytest -q
```

odds-tool:

```powershell
cd C:\Users\Joshua\APIs\odds-tool
npm.cmd test
npm.cmd run build:client
```

### 3. Commit and push Bet-Result-Checker

`Bet-Result-Checker-github` is on `main` with
`origin=https://github.com/jbaysore/Bet-Result-Checker.git`.

```powershell
cd C:\Users\Joshua\APIs\Bet-Result-Checker-github
git status --short
git add -A
git commit -m "Implement actual-start-aware CLV capture"
git push origin main
```

At implementation time, `C:\Users\Joshua\APIs\odds-tool` was **not** inside a
usable Git worktree: it had no `.git`, and `C:\Users\Joshua\APIs\.git` was an
empty directory. Its changes are therefore local-only. Do not run `git init`
over it blindly. If odds-tool is supposed to be backed by a remote repository,
restore its actual worktree/remote before attempting to push; otherwise the
local restart in step 4 is what activates these changes.

### 4. Restart odds-tool on the tolerant code

Close the existing odds-tool API and Vite windows first. The launcher skips a
service that already looks healthy, so leaving the old Node process alive would
leave the old code in memory.

```powershell
cd C:\Users\Joshua\APIs
powershell -NoProfile -ExecutionPolicy Bypass -File .\odds-tool\scripts\start-dev.ps1
```

Open `http://127.0.0.1:3000` and confirm the app loads. The API readiness check
is `http://127.0.0.1:3001/api/credits`.

### 5. Run the one-time live-sheet migration

The checker `.env` must contain `SHEET_ID`, `SHEET_TAB=Bets`, and either
`GOOGLE_APPLICATION_CREDENTIALS` or `GOOGLE_APPLICATION_CREDENTIALS_JSON`.

```powershell
cd C:\Users\Joshua\APIs\Bet-Result-Checker-github
py scripts\migrate_clv_provenance.py
```

The command is idempotent. Verify that it reports appended/backfilled counts,
that the Bets headers now contain the provenance/Pinnacle columns, existing
rows show `LEGACY_UNAUDITED`, and `SchemaMigrations` contains
`clv-actual-start-v1`.

### 6. Deploy and resume the new worker/checker

1. In Railway, deploy the latest `main` commit and resume the service.
2. Confirm the log contains `worker started`, the configured margin/slot values,
   and credit accounting rather than a missing-migration-marker error.
3. Re-enable the cron-job.org GitHub Actions trigger.
4. Manually run the `Run Bet Result Checker` GitHub workflow once and confirm
   its unit-test, checker, and promotion-updater steps pass.

### 7. Smoke-test the end-to-end capture path

1. Log one supported, single-selection, non-live future bet from an odds-tool
   scanner card.
2. Confirm its Bets row contains Event ID.
3. Confirm a `ClosingCapture` row appears with `PENDING` status and the appended
   columns.
4. During the event's tracking window, confirm rolling sample fields receive
   price, fetched-at, bookmaker-last-update, and start-state values.
5. After `/scores` detects live, confirm the worker finalizes once and writes
   ClosingOdds, CLV, Start Status, Closing Quality, Closing Source, observed/
   detected timestamps, and Pinnacle fields when an exact fresh market exists.
6. Confirm Stats and Coach exclude non-verified quality by default.

### 8. Run the remaining shadow measurements in a separate PowerShell window

Do not monitor every active Odds API sport: the baseline costs one credit per
selected sport per poll. Select only sports with events in the window. Example
for an MLB/WNBA/MLS slate:

```powershell
cd C:\Users\Joshua\APIs\Bet-Result-Checker-github
$env:SHADOW_SPORTS="baseball_mlb,basketball_wnba,soccer_usa_mls"
$env:SHADOW_POLL_SECONDS="60"
$env:SHADOW_MARGIN_SECONDS="90"
$env:SHADOW_MAX_AGE_SECONDS="300"
$env:SHADOW_BOOK_FRESHNESS="1"
$env:SHADOW_EVENTS_STABILITY="1"
$env:SHADOW_FRESHNESS_BOOK="pinnacle"
$env:SHADOW_FRESHNESS_REGION="eu"
$env:SHADOW_LOG_PATH="shadow_logs/start_monitor-live.jsonl"
py shadow_start_monitor.py
```

Run it from at least 30 minutes before the first selected event until at least
45 minutes after the last selected event, then stop with Ctrl+C. On an MMA or
boxing event day, repeat with:

```powershell
$env:SHADOW_SPORTS="mma_mixed_martial_arts,boxing_boxing"
py shadow_start_monitor.py
```

Retain `shadow_logs/start_monitor-live.jsonl`. Use it to decide whether to
tighten the 90-second margin/ten-minute quote threshold and whether MMA/boxing
can be promoted from PROVISIONAL.

### 9. Run the flag-only historical audit

This writes only Actual Start/source/confidence and Start Audit for
`LEGACY_UNAUDITED` rows. It does not change ClosingOdds or CLV.

```powershell
cd C:\Users\Joshua\APIs\Bet-Result-Checker-github
py scripts\clv_start_audit.py 2>&1 | Tee-Object -FilePath clv_start_audit_report.txt
```

Review the SAFE, LIKELY_SUSPECT, INDETERMINATE, and UNRESOLVABLE counts plus the
aggregate CLV-without-bucket figures. Do not re-pull historical odds until that
report supports a specific re-pull decision.

### 10. Review the first full slate and tune only from evidence

After one representative slate, inspect Railway credit logs, ClosingCapture
sample gaps, stale/provisional counts, start-detection lag, and Pinnacle quote
freshness. Keep the defaults unless the shadow/capture data supports changing
them. If any required margin exceeds approximately 180 seconds, implement the
plan's append-only samples-tab escape hatch before increasing rolling-slot
requirements further.

---

# Implementation Audit — 2026-07-15 (post-implementation review)

Audited commit `8134dc2` (Bet-Result-Checker) + current odds-tool tree against rev 3. Test suites: **374 passed (Python), 552 passed (JS), 0 failures** — including the Python/JS power-devig parity fixture. The Phase 6 audit was already run against the live sheet (381 legacy rows stamped).

## Verified correct (spot-checked against source, not just tests)

- **§2.1/2.2 model**: three start states with UNKNOWN fail-closed; `Start Status` vs `Closing Quality` split; margin (90s) + max-age (5 min) + book-quote freshness all enforced at finalize (`closing_provenance.py`); `quote_is_fresh` treats a missing `book_last_update` as not-fresh (strict, correct).
- **Guarded single write**: `write_closing_odds` refuses to write without a provenance payload or with the 8 provenance columns missing; odds-tool's manual-entry endpoint has the same refusal.
- **Migration**: `SchemaMigrations` marker, fail-closed worker startup (`require_migration_marker`), LEGACY_UNAUDITED backfill, prefix-tolerant queue-header validation on BOTH repos, extension columns addressed by name — the staged migration is as specified.
- **Phase 2**: importer targets `actual_start − 90s` in either direction when CONFIDENT (→ VERIFIED_CLOSE, downgraded to STALE when the book quote wasn't fresh in the snapshot); `min()` fallback → SAFE_BUT_EARLY/PROVISIONAL; resolvers for MLB (statsapi firstPitch via schedule-hydrate, feed/live fallback) + WNBA/MLS/World Cup (ESPN first-play wallclock); ambiguous ESPN match → UNRESOLVED, never guessed.
- **Parlays**: per-leg resolution, worst-leg-wins combined quality, per-leg audit JSON persisted to ClosingCapture (`write_closing_capture_audit`).
- **Kalshi**: candle window ends at actual start when resolved, `min(actual, scheduled)` otherwise.
- **Scanner (Phase 3)**: `event_lifecycle` tombstone checked before price recording/opportunity computation and in upsert (`tombstoned` classification); reschedule-clear at >6h commence shift; purge wired into the existing pruner; `closing_samples` with at-or-before + freshness stamping (incl. `book_last_update`) on live detection; verified-PREGAME games stay alive past scheduled commence; `hardExpireCommenced` excludes them and now also reads closing_samples.
- **Consumers (Phase 4)**: `clvQuality.mjs` implements the exact legacy contract (VERIFIED_CLOSE pooled; LEGACY_UNAUDITED pooled until `Start Audit`, then SAFE only; blank provenance on a post-migration row → excluded), used by StatsPage (excluded counts + include toggle) and Coach; JS mirrors Python `legacy_row_is_pooled`.
- **Pinnacle (Phase 5)**: featured markets batched per sport; exact event-id + exact-point contract (point mismatch → no quote); 2-way and 3-way power devig (bisection) with shared vectors (`tests/fixtures/power_devig_vectors.json` ↔ `powerDevigParity.test.js`); reproducibility JSON in ClosingCapture; Bets carries only `Pinnacle Close`/`Pinnacle CLV`; Pinnacle quote freshness checked before writing.
- **Recovery**: reads `Actual Start` from the sheet (checker is sole resolver); fingerprint now includes resolver version, actual start, confidence, and start status per leg.
- **Phase 6 audit**: buckets per spec (strict `>` on the boundary → INDETERMINATE), flag-only, LEGACY_UNAUDITED rows only. **It was run**: 381 rows → SAFE 118 · LIKELY_SUSPECT 24 · INDETERMINATE 32 · UNRESOLVABLE 207; delta median +60s, min −5h, max +17.4h. Pooled-CLV averages: SAFE ≈ 0.05 vs UNRESOLVABLE ≈ 2.67 — inflated CLV concentrates almost entirely in UNRESOLVABLE rows (sports with no resolver: fight cards etc.), consistent with the Phase 0 prediction that MMA/boxing timing, not MLB early starts, is the main poison vector.

## Findings

### HIGH

**H1 — No capture window: the worker samples from queue-entry, not T-15, and the budget degrade then starves the actual close.**
`process_queue` has no time gate: every non-final queue row triggers scores polling for its sport, Pinnacle featured fetches, and (via `sample_due`, cadence-only) odds sampling every 90s cycle — even for games days away. A day-ahead slate ≈ 960 scores + 960 Pinnacle credits per sport per day plus ~960/day per bet, so `CLOSING_DAILY_SOFT_BUDGET=2500` exhausts within hours; the worker then enters `scores-only` mode, in which sampling is blocked entirely (worker line 507) — including for games actually approaching their close that evening. Net effect under realistic load: budget burned on far-future rows in the morning, no samples at night, everything finalizes FALLBACK — and via H2 those bets then never get closing odds at all.
*Fix:* gate scores polling, Pinnacle fetches, and sampling to rows inside `commence − 15 min → cap` (plan Phase 1.3); optionally make degrade modes drop far-from-close rows first instead of disabling sampling globally.

**H2 — Blank-provenance dead end: new parlays and worker-missed bets never get closing odds again.**
`/api/bets` appends new rows with **blank** `Start Status` (server.js:3040-3074 writes no provenance). The new importer guard in `load_bets_needing_closing_odds` skips any blank-status row once the provenance schema exists ("blank must fail closed"). But: (a) parlays are never queue-eligible, so they stay blank forever and the importer — the only path that prices them, including all the new per-leg Phase 2 machinery — never picks them up; (b) worker `finalize()` on FALLBACK writes provenance to the queue row only, never the Bets row, so worker-missed singles also stay blank and are skipped forever. Fail-closed for contamination, but a silent functional kill of parlay CLV and of the importer's backstop role.
*Fix (pick one):* let the importer treat a blank-status row whose game has commenced and which has no active queue row as eligible — it now writes correct provenance itself, which is exactly the designed Phase 2 path; or stamp Bets `Start Status` at log time (e.g. parlays → a `PENDING_IMPORT` value the guard allows) and on worker FALLBACK.

### MEDIUM

**M1 — One transient scores error at/after commence finalizes prematurely.** In `process_queue`, `now >= commence and sample_state == "UNKNOWN"` finalizes immediately as PROVISIONAL. A single failed `/scores` call (network blip, 429) at commence+1s forfeits the late-start path for every pending bet in that sport that cycle — the same game may be verified PREGAME 90s later. *Fix:* require N consecutive UNKNOWN cycles (or a grace window, e.g. commence+5 min) before the UNKNOWN finalize.

**M2 — Scanner `closing_samples` stops recording at scheduled commence, defeating its own late-start handling.** `recordPrices` only inserts samples when `observedMs <= commenceMs` (scannerDb.js:447), but the scan loop deliberately keeps verified-PREGAME games alive past commence. For a late start, prices keep flowing to `price_latest` but never into `closing_samples`, so `markEventLive` finds only a stale pre-commence sample → `stale_quote` skip instead of the better post-commence pregame close. *Fix:* extend the insert window while the event has no lifecycle row, e.g. `observedMs <= commenceMs + cap` gated on tombstone absence.

**M3 — Recovery "earlier snapshots" are scheduled-relative and can be post-start for early games.** With `includeEarlierSnapshots`, fallback snapshots are `commence − 6/−11 min` even when a CONFIDENT actual start exists (closingOddsRecovery.js:159-162). For a game that started well early — the exact rows this tool exists to repair — those fallbacks can be after actual start (live prices) while the leg's pre-assigned quality stays VERIFIED_CLOSE regardless of which snapshot supplied the price. *Fix:* compute fallbacks relative to the effective cutoff and/or downgrade quality when a non-primary snapshot is used.

### LOW

- **L1** — `pinnacle_quote_for_bet`/`_matches_selected` call `float(outcome.get("point"))` unguarded; a missing point raises TypeError inside the worker cycle (caught by the outer loop, but aborts the remaining rows that cycle). Wrap per-row.
- **L2** — Doubleheader event-id resolution uses closest-commence (`find_event`) rather than the plan's ambiguous→UNKNOWN. Low risk since the queue's Commence UTC originates from the same TOA event, but it is a spec deviation.
- **L3** — Housekeeping: the two `sys.path` script fixes are uncommitted; `clv_start_audit_report.txt` is UTF-16 (PowerShell redirect artifact) — re-emit as UTF-8 if it's meant to be kept.
- **L4** — `_record_credits` counts 1 per capture attempt; a market-family cascade (2 calls) or the multi-market Pinnacle fetch costs more, so the soft budget undercounts in exactly the situations that are already expensive (compounds H1).

## Recommended action order

1. **H1 + H2 before the next slate** — both are small, contained changes (a window gate in `process_queue`; an importer-eligibility tweak) and both silently defeat the system's purpose under real load.
2. M1/M2 next (each is one conditional); M3 whenever recovery is next touched (or disable `includeEarlierSnapshots` for confident-actual rows in the interim).
3. The re-pull decision for the 24 LIKELY_SUSPECT + 32 INDETERMINATE rows is now unblocked; the 207 UNRESOLVABLE rows (avg CLV 2.67 vs SAFE's 0.05) argue for extending resolvers to fight cards or accepting their permanent exclusion from pooled CLV.

---

# Suspect Re-Pull — 2026-07-15 (executed)

`scripts/repull_suspect_closing.py` (new; tests in `tests/test_repull_suspect_closing.py`) re-pulled the 56 LIKELY_SUSPECT + INDETERMINATE rows using each row's audit-stamped `Actual Start` (snapshot at `actual − 90s` → `VERIFIED_CLOSE`, `Start Status = VERIFIED`, `Closing Source = recovery-historical`). Failures restore the original values + `LEGACY_UNAUDITED` provenance, so no row is ever left in an intermediate state.

**Result: 52 of 56 repaired** (three runs; reports in `repull_suspect_report*.json`):
- Avg CLV on the repaired rows: **+0.182% (poisoned) → −0.815% (true close)**. Largest corrections: BetID 373 +55.56%→+5.66%, 355 +13.70%→+0.41%, 284 0%→−8.71%.
- **4 permanent leftovers**, originals restored and excluded from pooled CLV by the legacy contract: 368/364 (SELECTION NOT FOUND — the exact alternate line no longer existed at the true close), 314 (BOOK NOT FOUND — book had pulled the market), 52 (MANUAL ENTRY — Kalshi-style book without a ticker; hand-enter if desired).

Two implementation notes from the run:
1. **Sheets write quota**: 56 back-to-back clear+write cycles hit Google's 60-writes/min limit; the driver now paces (`--pace`, default 5s) and retries across quota windows. Failed rows were restored, none lost.
2. **Historical additional markets** (root cause of 13 "transient" failures): the sport-level historical endpoint 422s on `alternate_*`/`team_totals`. Added `_fetch_historical_event_snapshot` to `closing_odds.py` — historical events roster (1 credit, cached) → `/historical/sports/{sport}/events/{id}/odds` (10 credits) — mirroring the live path and odds-tool's recovery. This also fixes the same latent gap for ALL future importer/retry fetches of additional-market bets, not just this backfill.

**Remaining legacy work (the "rest")**: the 207 UNRESOLVABLE rows — either build fight-card/other-sport actual-start resolvers and repeat audit→re-pull, or accept their permanent exclusion from pooled CLV.

---

# Closing-Gap Taxonomy — 2026-07-15 (`scripts/closing_gap_taxonomy.py`)

Read-only classifier over all 381 rows. **140 contribute a trusted CLV; 241 do not.** The "207 UNRESOLVABLE" figure was misleading — it merged a resolver *bug*, several *missing* resolvers, and plain re-fetch gaps. Almost none of it is permanent. The 241 split into two modes:

### Mode A — HAS a closing price, start not verifiable (169)

| Subset | Rows | Root cause | Fix |
|---|---|---|---|
| World Cup | 66 | **Soccer resolver bug** — `resolve_espn_actual_start` finds the game (event id resolves) but `first_play_wallclock` returns None: ESPN soccer summaries carry `plays: []`; timing is in `keyEvents`/`commentary` (+ `header.competitions[0].date` scheduled fallback). **Also silently breaks MLS** (same code path). | Fix soccer first-event extraction → re-audit → re-pull. No new resolver. |
| Tennis (ATP/WTA Wimbledon + manual) | 65 | No tennis resolver | Build tennis resolver (ESPN covers tennis) |
| MMA (19) + Boxing (3) | 22 | No combat resolver | Build combat resolver (ESPN covers MMA/boxing) |
| NBA (6) + CFL (5) | 11 | Sport not in `ESPN_ROUTES` table | Add route entries — plumbing already exists (same as WNBA/MLS) |
| already-resolved / parlay-per-leg edge | 5 | misc | case-by-case |

### Mode B — NO closing price at all (72)

| Subset | Rows | Fix |
|---|---|---|
| Auto-priceable, blank or N/A (mostly MLB/WNBA/MLS on normal books) | 53 | Re-fetch pass; the new `_fetch_historical_event_snapshot` recovers the alternate-market subset that previously 422'd |
| Manual/book-specific (polymarket, betopenly, kalshi-no-ticker) | 7 | Hand-enter or build a book-specific source |
| VOID (settled — no CLV by design) | 6 | none (correct as-is) |
| Prop (1) / Parlay (5) — structural | 6 | Props need player-prop pricing; parlays auto-price once legs are priceable |

**Bottom line:** ~66 recoverable by fixing one soccer bug, ~98 by building/extending resolvers for sports ESPN already covers, ~54 by a re-fetch pass, and only ~13 (VOID + props + exotic books) are genuinely not automatable — most of which *shouldn't* have a CLV anyway. Effectively zero rows are permanently lost.

**Suggested order:** (1) soccer extraction fix → re-audit soccer → re-pull [66 + latent MLS]; (2) re-fetch pass over the 53–54 no-price auto rows; (3) add NBA/CFL routes [11]; (4) tennis resolver [65]; (5) combat resolver [22].

## Soccer fix — DONE 2026-07-15 (subset 1 of the taxonomy)

Root cause was exactly as diagnosed: `actual_start.first_play_wallclock` only scanned `plays[]`, which is empty for soccer; ESPN puts the kickoff wallclock in `keyEvents[]` (the `type:"kickoff"` entry). Fixed by scanning **both** `plays` and `keyEvents` and taking the earliest wallclock — one rule for every sport. Verified live: Spain v France → CONFIDENT `2026-07-14T19:00:07Z`. 394 tests green (added soccer keyEvents coverage).

Then re-audited (329 remaining LEGACY_UNAUDITED rows; `resolve_actual_start` reuses already-stamped Actual Start, so only previously-unresolved rows were re-touched — no regression risk):
- **UNRESOLVABLE 207 → 148**; **SAFE 118 → 173 (+55)**. As predicted, soccer kicks off on time, so the World Cup rows classify SAFE and are pooled **as-is with their existing pregame closing prices — zero credits, no re-pull**.
- Remaining suspects (8) re-pulled: **3 recovered** (372/366/363, previously blank → VERIFIED_CLOSE); 5 genuinely unrecoverable (368/364/365 SELECTION NOT FOUND, 314 BOOK NOT FOUND, 52 MANUAL ENTRY), originals restored.

**Net: Trusted/pooled CLV 140 → 197 (+57). Gap 241 → 184.** The single soccer bug fix (plus a handful of re-pulls) was the biggest lever, and it also permanently repairs MLS actual-start resolution going forward. Remaining gap is now tennis (65) + combat (22) + NBA/CFL (11) resolvers, the ~53 no-price re-fetch pass, and ~13 by-design (VOID/prop/exotic-book).

## NBA resolver — DONE 2026-07-15 (subset 2)

Added `basketball_nba: ("basketball", "nba")` to `ESPN_ROUTES` — NBA uses the proven `plays[].wallclock` path (verified live: Spurs–Knicks → CONFIDENT tip-off `2026-06-04T00:44:26Z`, 508 plays). 395 tests green. Re-audit: **UNRESOLVABLE 148 → 142; SAFE 173 → 179 (+6)** — like soccer, NBA starts on-time-or-late so all 6 NBA rows classified SAFE and are pooled with existing pregame prices, no re-pull. **Trusted CLV 197 → 203; gap 184 → 178.**

**CFL rejected (no fallback policy):** ESPN `football/cfl` scoreboard returns zero events for past games — no clean actual-start source, so CFL stays excluded rather than take a scheduled-time guess. Combat (MMA/boxing) and tennis likewise parked: no *confirmed* per-fight / first-serve actual-start signal, and Josh's rule is verified-actual or excluded — no scheduled-date fallback.

**Remaining gap (178):** tennis (65) + combat (22) — both need a confirmed non-scheduled start source before they qualify; ~51 no-price re-fetch pass on already-resolvable MLB/WNBA/MLS rows (no new code, event-snapshot fix helps); 12 World Cup remnants (ESPN returned no kickoff timing — excluded under no-fallback); CFL (5); ~13 by-design VOID/prop/exotic-book.

---

# Implementation Audit Resolution — 2026-07-15

Every finding in the post-implementation audit above was addressed.

- **H1:** the Railway worker now gates scores, Pinnacle, event resolution, and
  same-book sampling to `T-15` through the 45-minute delayed-start cap. At the
  daily soft ceiling it enters a critical mode that drops Pinnacle and
  non-critical sampling while preserving same-book capture in the final five
  minutes and through a verified delayed start.
- **H2:** blank post-cutover rows remain excluded from consumers, but the
  historical importer may repair them after verifying that no active
  ClosingCapture row owns the BetID. This restores parlay imports and the
  fallback path for worker-missed singles without racing the live worker;
  inability to read the queue remains fail-closed.
- **M1:** UNKNOWN scores state no longer finalizes at commence. The worker waits
  a configurable five-minute grace period for transient `/scores` failures.
- **M2:** scanner `closing_samples` now retain verified-pregame observations
  through the same 45-minute late-start cap. The existing lifecycle tombstone
  check still prevents any detected-live price from entering the archive.
- **M3:** optional recovery snapshots are now five/ten minutes before the
  effective safe cutoff rather than scheduled commence. An earlier fallback
  continues to downgrade a nominally verified leg to `SAFE_BUT_EARLY` and
  require confirmation.
- **L1:** missing or malformed Pinnacle points fail closed instead of raising.
- **L2:** worker event-ID backfill now refuses repeated-team matchups, including
  doubleheaders, rather than selecting the closest commence.
- **L3:** the direct-invocation `sys.path` fixes remain included, and the saved
  Phase 6 audit report was converted from PowerShell UTF-16 to UTF-8.
- **L4:** live market credit accounting now uses the actual
  `x-requests-last` cost of every uncached request, including market-family
  cascades and event lookups, instead of assuming one credit per bet attempt.

Regression coverage was added for the T-15 gate, UNKNOWN grace, blank-row
importer ownership, doubleheader deferral, exact cascade credit cost, malformed
Pinnacle points, late-start scanner samples, actual-start-relative recovery
fallbacks, and fallback quality downgrade.

Validation after the audit fixes:

- Bet-Result-Checker: **381 pytest tests passed**.
- odds-tool: **555 Node tests passed**.
- Focused CLV/scanner suites: **49 Python + 35 Node tests passed**.
- odds-tool production Vite build passed (existing large-chunk warning only).
- Python/JavaScript syntax checks, UTF-8 JSON parse, and checker
  `git diff --check` passed.

---

# Historical recovery — Tennis and combat batches (executed 2026-07-15/16)

## Tennis

Added a strict ESPN tennis resolver for completed ATP/WTA singles matches. It
matches both players inside the correct singles draw, requires `timeValid` and
completed status, and uses the retained competition start rather than the
tournament date. Ambiguous or missing matches remain unresolved. After the
targeted audit/recovery, trusted CLV increased to **293 rows** and the gap fell
to **93**.

## Combat sports

Implemented a UFC-only actual-start resolver using ESPN's per-bout play feed.
The ordinary competition timestamp is the **card** start and is explicitly not
used. A bout resolves only when strict normalized fighter matching finds one
completed competition and its play feed contains exactly one period-1 `Round
Start` wallclock. The parent card event id is retained to address that play
feed, and the verified provider alias `Zachary Reese` ↔ `Zach Reese` is narrow
and explicit. Boxing is intentionally excluded: the tested ESPN site/core
boxing routes return 404/invalid-sport responses, so no scheduled/card-time
fallback was introduced.

Targeted live-sheet batch: **22 rows** (19 UFC, 3 boxing).

- UFC: **19/19 actual starts resolved** from the opening bell.
- Original audit geometry: **14 SAFE**, **4 INDETERMINATE**, **1
  LIKELY_SUSPECT**.
- Boxing: **3 UNRESOLVABLE** (BetIDs 28, 29, 79), retained unchanged and
  excluded from pooled CLV.
- The five non-SAFE UFC rows were re-pulled at actual start minus the safety
  margin. **5/5 succeeded**, with guarded restoration never needed:
  - 182: close `+105` → `+115`; CLV `9.76%` → `4.65%`
  - 279: close `+550` → `+850`; CLV `100.00%` → `36.84%`
  - 276: close `-122` → `-120`; CLV `4.91%` → `4.13%`
  - 38: close `200` → `+200`; CLV stayed `-10.00%`
  - 37: close `-222` → `-220`; CLV `4.48%` → `4.19%`
- Those five re-pulls used **50 Odds API credits** total. The final read-back
  found all five with `Start Status = VERIFIED`, `Closing Quality =
  VERIFIED_CLOSE`, and the expected actual-start provenance.
- Full checker test suite: **406 passed**.

Net after combat: trusted/pooled CLV **293 → 312 (+19)**; gap **93 → 74**.
The post-combat gap contains 18 priced rows: 5 CFL + 3 boxing without a start
resolver, 6 already carrying actual starts but needing case-specific cleanup,
2 tennis matches still unmatched, and 2 parlay/per-leg cases. There are also 42
auto-priceable rows without a closing price.

**Recommended next batch:** the three plausibly recoverable priced rows that
already have actual starts (BetIDs **8, 9, 278**). They need no new sport
resolver: 8/9 need actual-start re-pulls and 278 needs a better-than-STALE
capture. The other three in that six-row category are already-known source
dead ends (52 is manual/Kalshi without a usable ticker; 364/368 previously
failed because the exact selection was unavailable), so retrying all six would
mostly spend credits without improving the dataset.

## Three-row follow-up — executed 2026-07-16

The recommended batch was attempted with exact before/after snapshots and
guarded restoration. **None of the three could be promoted into trusted CLV:**

- **8 and 9:** both use the sheet-only `manual_wta_libema` sport alias. The Odds
  API `all=true` directory (zero credits) confirms that it has no Libéma sport
  key, so there is no historical endpoint from which to reconstruct the two
  exact sportsbook closes. BetID 8's first attempt returned 404 and restored
  its original `-476 / 0.88%`; 9 was not sent through the same known-invalid
  route. Both remain `LEGACY_UNAUDITED / INDETERMINATE` and excluded.
- **278:** the actual-start re-pull succeeded technically and returned the same
  `-136 / 5.65%`, but FanDuel's retained quote timestamp was still outside the
  freshness limit. It truthfully remains `VERIFIED / STALE` and excluded rather
  than being mislabeled as a verified close.

The run used **11 Odds API credits** (the event-scoped team-total lookup for
278); the sport directory cost zero. The recovery utility now also (a) permits
an explicitly targeted `VERIFIED / STALE` row while preserving its complete
original provenance on failure, (b) fails fast for `manual_*` sport aliases,
and (c) redacts API credentials from rendered request-error URLs. **410 tests
passed.** Trusted/gap totals remain **312 / 74**.

**Recommended next batch:** the ten completed N/A rows with working actual-start
resolvers and no already-confirmed permanent source failure: BetIDs **39, 125,
223, 277, 294, 296, 305, 309, 323, 345**. This is a higher-yield guarded
historical re-fetch batch. BetID 314 is deliberately omitted because its prior
attempt already proved the required book quote unavailable.

## Missing-price 23-row batch — executed 2026-07-16

The apparent 23-row historical recovery bucket contained three different
states after an exact live-sheet freeze:

- **11 upcoming bets** (237, 238, 252, 287, 292, 331, 340, 362, 384, 385,
  387), dated July 16–22. Their blank closing cells are expected; they remain
  owned by the normal capture worker and were deliberately not touched.
- **1 misclassified fight** (39): Brad Pauls–Bradley Goldsmith is boxing data
  stored under the MMA sport key. The UFC resolver correctly found no matching
  competition, so no scheduled-time/provisional closing price was written.
- **11 completed retryable rows** (125, 223, 277, 294, 296, 305, 309, 314,
  323, 345, 365). Each was cleared and fetched through guarded recovery; every
  unavailable market was restored to its exact original value and provenance.

**Result: 0 of 11 recovered.** The historical snapshots did not retain the
required book/total selection at the verified cutoff. The most common failure
was `BOOK NOT FOUND` after both the main totals and event-scoped alternate-total
paths; BetID 365 again returned `SELECTION NOT FOUND`. Final read-back confirmed
the ten N/A rows are still N/A, 365 retains its original error/provenance, and
no interrupted clear/write state exists. The run used **118 Odds API credits**.

Post-run census is unchanged: **312 trusted / 74 excluded**. These completed
rows should not be bulk-retried again without a new historical book source or
new evidence that the provider's archived markets changed.

## EFL Cup resolver — implemented 2026-07-16

Added `soccer_england_efl_cup -> soccer/eng.league_cup` to the authoritative
ESPN actual-start routes and to the gap taxonomy. ESPN's live scoreboard was
verified against all three exact upcoming rows: Rochdale–Tranmere (event
401877066), Crawley Town–York City (401877065), and Wrexham–Middlesbrough
(401881129). The resolver uses the existing strict two-team match plus earliest
`keyEvents` kickoff wallclock; it also correctly matches the sheet's `Wrexham
AFC` against ESPN's `Wrexham` without introducing a broad fuzzy alias.

All three bets (358, 359, 360) are still upcoming on August 1/3/7, so their
sheet cells were deliberately left untouched for the live worker. After each
match completes, the route can provide a confident actual kickoff for normal
capture fallback or targeted re-audit/recovery. Resolver coverage moves these
three rows out of the `no_resolver` cause, although the current trusted/gap
total does not change until the events occur and prices are captured. Full
checker suite: **411 passed**.
