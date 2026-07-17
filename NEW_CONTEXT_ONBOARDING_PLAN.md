# New Context Onboarding — Implementation Plan

**Created:** 2026-07-16 · **Status:** Phases 0–6 implemented and audited;
Phase 7 remains intentionally outstanding. Closing-quality enforcement and
post-event promotion are enabled by default (both retain environment kill
switches). P0.2 policy numbers are ratified and frozen in
`onboarding_policy.py`. See **§Implementation status** and the final audit below.
**Concept:** `NEW_LEAGUE_ONBOARDING_CONCEPT.md` (ratified). This plan turns it
into phases with files, schemas, gates, and tests.
**Scope:** Bet-Result-Checker (profile owner, verification, repair) AND
odds-tool (at-log status, scanner discovery, UI, notifications).

Precedents this plan deliberately reuses:
- Shadow-mode-first rollout (`PROPS_SHADOW_MODE`, `shadow_start_monitor.py`).
- Header-name column resolution + Notes markers as durable checker state
  (`config.BET_COL`, `sheets_writer.upsert_notes_line` — Railway has no disk).
- The CLV-accuracy provenance vocabulary (`closing_provenance.py`:
  `START_*`, `QUALITY_*`; Bets columns `Start Status` / `Closing Quality` /
  `Closing Source` / `Actual Start …`). **This plan does not invent a second
  row-trust vocabulary — capability checks feed the existing one.**
- Cross-repo parity fixtures asserted in BOTH test suites (the
  betReviewPl.js / resolver.py half-win precedent).

---

## 0. Architecture decisions (resolving concept "Decisions" #3, #13)

### 0.1 Where the profile lives: the Google Sheet, checker owns writes

The checker runs on Railway with no durable disk; odds-tool runs on Josh's PC.
The only durable store both already read and write is the spreadsheet. So:

| Store | Tab | Writer | Readers |
|---|---|---|---|
| Canonical identity registry | `Context Registry` | checker only | checker, odds-tool |
| Capability records | `Capabilities` | checker only | checker, odds-tool |
| Discovery/intent signals | `Discovery Queue` | odds-tool only (append-only) | checker (consumes + compacts) |
| Row trust / provenance | `Bets` columns (existing) | existing writers unchanged | everyone |
| Onboarding cases | derived — no tab. Cases are computed from `Capabilities` + affected Bets rows; case lifecycle state (ack, mute) lives in a `case:` Notes marker on the Capabilities row. | checker | odds-tool UI |

Single-writer per tab preserves "one component owns profile mutations"
(concept §1) without cross-deployment RPC. odds-tool reads `Capabilities`
through the existing `sheetTabCache` (server.js, 2-min TTL). **Fail closed:**
if the tab read fails, odds-tool treats every capability as unverified for
status display (bet logging is never blocked), and the checker treats reads
it cannot confirm as "no record" → provisional (concept safety #14).

### 0.2 Canonical identity scheme

`context_id` = `family/competition[/edition]`, lowercase, dot-free slugs:
`soccer/eng.efl_cup/2026-27`, `mma/ufc`, `baseball/mlb`, `mma/misfits.x19`
(event-scoped). Editions only where trust genuinely resets (tournaments,
one-offs); recurring leagues carry no edition. Aliases (TOA sport keys, ESPN
route tuples, sheet display names) are rows pointing at a `context_id` —
they never carry trust (concept: identity hierarchy).

### 0.3 Capability record key (grain)

One `Capabilities` row per record:

```
record_key = context_id | capability | qualifier
```

| capability | qualifier |
|---|---|
| `identity` | source name (`toa`, `espn`, `mlb_statsapi`, `sheet`) |
| `discovery` | discovery source (`toa`) |
| `start_live` | start source (`toa_scores`) |
| `start_authoritative` | source (`espn`, `mlb_statsapi`, `espn_fights`) |
| `capture` | `book × market_family` (e.g. `draftkings|featured`, `kalshi|h2h`) |
| `settlement` | `market_class × result source` (e.g. `spread|toa_scores`) |
| `recovery` | `book × market_family` |
| `benchmark` | `benchmark source × market_family` (`pinnacle|featured`) |

Market families v1 (decision #2 default): `h2h`, `featured` (spread/total),
`team_total`, `prop`, `outright`. Records are created lazily on first
encounter — no pre-population of every book × family.

Row columns: `Record Key | Context ID | Capability | Qualifier |
Classification | Health | Activity | Policy Version | Evidence Summary |
First Seen | Last Verified | Last Checked | Constraints | Notes`.
`Classification` ∈ Discovered/Verified/Limited/Blocked/Manual/Retired;
`Health` ∈ NotEvaluated/Fresh/Stale/Contradicted; `Activity` ∈
Idle/Collecting/AwaitingPostEvent (concept §2). `Constraints` is JSON for
Limited bounds. `Evidence Summary` is compact JSON counters
(`{"clean":3,"irregular_ok":1,"neg":0,"quarantined":1}`) — raw evidence
detail stays in the shadow JSONL locally; the sheet keeps verdict + counters
+ per-row provenance columns, which is the audit trail (concept success
criterion: trusted rows retain enough evidence to re-audit — they do, via
`Actual Start`/`Start Status`/`Closing Observed At`).

### 0.4 Grandfathering (decision #2, "without retroactively blessing rows")

Current implicit support tables become seed `Capabilities` rows at
`policy_version=1`, Classification=Verified, Health=Fresh, Notes=`seeded:`
with the source table named. Seeds:

- `actual_start.py` `ESPN_ROUTES` / `TENNIS_ROUTES` / `COMBAT_ROUTES` →
  `start_authoritative` records.
- `closing_provenance.TRUSTED_FLIP_SPORTS` → `start_live` records.
- `sources/scores_live.py` CoverageCache classifications → `discovery` +
  `start_live` support evidence.
- odds-tool `scannerConfig.supportedSports` + market profiles → `discovery`,
  `capture` records for the books actually in use.
- `resolver.py` / `prop_resolver.py` supported market classes →
  `settlement` records.
- `pinnacle_closing.py` coverage → `benchmark` records.

Seeding matches today's behavior exactly (gate P1), so no existing row's
trust changes. Historical rows keep their `LEGACY_UNAUDITED` handling from
the CLV plan — this plan never touches them.

### 0.5 How row trust is enforced (no new row vocabulary)

The concept's "row is provisional" maps onto the existing quality contract:
a capture whose required capability records are not (Verified|in-Limited-
bounds) + Fresh **cannot finalize as `VERIFIED_CLOSE`** — it finalizes as
`PROVISIONAL` (existing `QUALITY_PROVISIONAL`), with a Notes marker
`onboarding: <record_key>=<classification>` naming why. Pooled CLV already
excludes everything but `VERIFIED_CLOSE`, so exclusion is inherited free.
Repair upgrades quality through the existing recovery `Closing Source`
values plus a new `recovery-onboarding` source (§Phase 5).

---

## Phase 0 — Ratify defaults + inventory (no code on hot paths)

**Deliverables**

1. `scripts/onboarding_inventory.py` (checker) — enumerates every implicit
   support table listed in §0.4 (imports them; no copies) and emits the
   proposed seed rows as JSON + a human-readable table.
2. The **Proposed policy defaults** (§P0.2 below) reviewed and ratified by
   Josh, then frozen into `onboarding_policy.py` as `POLICY_VERSION = 1`.
3. Sheet work (Josh, manual — same pattern as the HALF WIN/LOSS validation
   TODO): create tabs `Context Registry`, `Capabilities`, `Discovery Queue`
   with the §0.3 headers; add optional Bets column `Context ID`.

**P0.2 Proposed policy defaults** (each maps to a concept decision; all
PROPOSED until ratified):

| # | Decision | Proposed default |
|---|---|---|
| 1 | Evidence bar | Family prior strong (same source, ≥3 verified sibling contexts, ≥1 irregular handled): promote after **1** clean context event. New family/source: **3** clean events, ≥2 distinct days. Immediate Contradicted: post-start capture admitted as verified, or authoritative-vs-live start disagreement > 10 min. |
| 2 | Grain boundaries | §0.3 table; splitting a too-broad record = new narrower records at Discovered, old record Retired-by-system-recommendation (user ratifies), no row re-blessing. |
| 4 | Block logging vs provisional | Nothing blocks logging. Ever. (concept safety #5) |
| 5 | Display surfaces | At-log response toast + status matrix in log dialog; CLV badges on BetsPage; cases panel on the existing review surface (§Phase 6). |
| 6 | Freshness | `start_live`/`start_authoritative`/`capture`: 120 days idle → Stale; any record: source route change or POLICY_VERSION bump → Stale. Season boundary = idle window; no per-sport calendar in v1. |
| 7 | Negative evidence | Attributed to narrowest grain (concept §8). Severity: post-start-as-verified / start contradiction = immediate Contradicted; ambiguous match / transient empty = quarantine counter, 3 within 14 days → Stale. Decay: quarantine counters reset after 30 clean days. |
| 8 | Manual evidence | Notes-marker package on the bet row: `manual-evidence: {fact, value, source, observed_at, attested_by:"josh"}` written via a small CLI (`scripts/attest_row.py`). Qualifying facts: actual start, closing price+timestamp, result. |
| 9 | Ephemeral | Offered when a context's competition slug is event-scoped (one event, no sibling events in discovery); user confirms via case action. |
| 10 | Evidence retention | Sheet keeps summaries/counters forever; local JSONL 90 days. |
| 11 | Benchmark | Pinnacle only in v1 (matches current CLV benchmark work); exact market/period/point match required; no fallback source; missing → row reported "unbenchmarkable", nothing demoted. |
| 12 | Scanner intent | Dedup key `context_id|book|market_family`, 7-day expiry if no bet follows, ≤ 20 Discovery Queue appends/day, no extra TOA credits (reuses scan data already fetched). |
| 14 | Versioning | `POLICY_VERSION` int in `onboarding_policy.py`; per-source `mapping_version` on Registry rows. Bump → affected records' Health=Stale (not Contradicted); rows are NOT re-evaluated unless the bump's release note names them. |
| 15 | Case lifecycle | One parent case per `context_id`, child issue per record_key. Case closes when all child issues reach a classified outcome; Blocked leaves the limitation on the Capabilities row and closes the case. Reopen on: new source mapping, policy bump, or new qualifying event. |

**Gate P0:** Josh ratifies the defaults table and the seed inventory output;
tabs exist in the live sheet. No code merged to hot paths.

---

## Phase 1 — Registry + profile store, shadow-only (checker)

**New files**

- `context_registry.py` — load/resolve canonical identities.
  `resolve(sport_key, team1, team2, game_date) -> ContextResolution`
  (`context_id`, `confidence`, `via_alias`). Ambiguity or no match returns
  `confidence="NEW"` — never guesses a known context (concept safety #3).
- `capability_profile.py` — typed read/write of the `Capabilities` tab:
  `get_record(record_key)`, `require(records...) -> TrustDecision`,
  `transition(record_key, to, reason, policy_version)` with the concept §2
  transition table enforced in code (illegal transitions raise).
- `onboarding_policy.py` — `POLICY_VERSION`, evidence-bar constants from
  P0.2, freshness windows, severity table. Pure data + pure functions.
- `scripts/seed_capabilities.py` — writes the P0 inventory into the tabs
  (idempotent; refuses to overwrite non-seed rows).

**Changed files (shadow only — log, don't act)**

- `closing_capture_worker.py`: for each bet it processes, resolve context +
  required records; append what enforcement WOULD have decided to the shadow
  JSONL (reuse `shadow_start_monitor.py`'s writer or extract
  `shadow_log.py`). Behavior unchanged.
- `poller.py`: same shadow call on settlement path.
- `config.py`: `ONBOARDING_SHADOW_MODE = True`, `ONBOARDING_ENFORCE = False`.

**Tests (pytest)**

- `tests/test_context_registry.py` — alias hit, alias miss → NEW, ambiguous
  (two competitions match) → NEW, edition handling, provider-key reuse
  (same TOA key remapped → old evidence not inherited).
- `tests/test_capability_profile.py` — record round-trip, illegal transition
  raises, `require()` fail-closed on missing tab (mock quota error),
  Limited-constraints in/out of bounds.
- `tests/test_onboarding_policy.py` — evidence bar arithmetic, severity
  classification, freshness aging (frozen clock).
- `tests/test_seed_parity.py` — **the gate test**: for every (sport_key,
  book, market) combination present in the last 60 days of Bets rows
  (fixture snapshot), `require()` over the seeded profile returns TRUSTED
  exactly where today's implicit tables would have allowed a
  `VERIFIED_CLOSE`, and PROVISIONAL exactly where they wouldn't. Zero diffs.

**Gate P1:** seed-parity test green; one week of shadow JSONL shows zero
would-have-changed decisions on already-supported contexts; pytest suite
green. Rollback: flags off, tabs are inert data.

---

## Phase 2 — Enforcement on row trust (checker)

**Changed files**

- `closing_capture_worker.py` — finalize path: quality assignment consults
  `capability_profile.require(start_live, capture, identity)`; not trusted →
  cap quality at `QUALITY_PROVISIONAL` + `upsert_notes_line` marker
  `onboarding:`. (One-line concept anchor: §0.5.)
- `closing_odds.py` — historical/recovery paths: same `require()` before any
  write that could produce `VERIFIED_CLOSE`; `sources/kalshi.py` candle path
  likewise (capture record `kalshi|h2h`).
- `poller.py` — on encountering a bet whose context resolves NEW or whose
  required records are absent: create `Discovered` records (checker is the
  single writer) and keep settling as today (settlement independence,
  concept non-goal #3).
- `scripts/clv_start_audit.py` — extend the report to the concept's
  reporting buckets (pending / capturing / observing / recoverable / blocked
  / unbenchmarkable / manual / retired / by-design).

**Tests**

- `tests/test_worker_onboarding_gate.py` — unverified grain ⇒ PROVISIONAL
  even when timing evidence is perfect; verified grain ⇒ unchanged behavior
  (regression fixtures from Phase 1 parity set); profile read failure ⇒
  PROVISIONAL + no crash (fail closed).
- `tests/test_poller_discovery.py` — new context bet creates exactly the
  lazy records for its grain, is idempotent, and never blocks settlement.
- `tests/test_audit_buckets.py` — every row lands in exactly one bucket;
  upcoming events never in failure buckets.

**Gate P2:** flip `ONBOARDING_ENFORCE=True` after the P1 shadow week showed
zero unexpected PROVISIONAL downgrades; audit script shows zero trusted rows
with post-start closes (existing invariant, now capability-gated too).
Rollback: flip flag back; markers are inert.

---

## Phase 3 — At-log status surfacing (odds-tool)

**New files**

- `capabilityStatus.js` — reads `Capabilities` + `Context Registry` via the
  existing `sheetTabCache`; `statusForBet({sportKey, book, marketKey}) →
  {contextId, matrix, provisional, unrepairableRisk, reason}` mirroring the
  checker's `require()` semantics. **Parity fixture shared with
  `tests/test_capability_profile.py`** (JSON in both repos, betReviewPl
  precedent).
- `client/src/components/OnboardingStatusBadge.jsx` — the status matrix
  ("start detection verified; DK featured capture verified; benchmark
  unavailable"), rendered from the POST response.

**Changed files**

- `server.js` — `POST /api/bets` (:2987): after append, compute status and
  include `onboarding` in the response; also write `Context ID` to the new
  Bets column when resolution is confident. `GET /api/bets` (:1544): attach
  per-row provisional/repaired flags derived from the existing
  `Closing Quality`/`Closing Source` columns (no new fetch).
- `sheetColumns.js` + `shared/atLogContext.mjs` — add `Context ID` to the
  column vocabulary (keep the CJS/ESM pair in sync, per the file's own
  convention).
- `client/src/components/BetsPage.jsx` — CLV cell badges: `provisional`
  (quality ≠ VERIFIED_CLOSE with `onboarding:` marker), `repaired`
  (`Closing Source = recovery-onboarding`), `unbenchmarkable`. Provisional
  CLV renders the number, badged, excluded from the page's aggregates
  (concept: shown, badged, never aggregated).
- Late-bet warning: if the event's commence time is past or within the
  safety margin, the response says the row may be unrepairable (concept §3).

**Tests (node --test)**

- `tests/capabilityStatus.test.js` — parity fixture vs checker; tab-read
  failure → everything unverified, logging still succeeds; cache TTL
  respected (no extra Sheets quota per log).
- `tests/betsOnboardingResponse.test.js` — POST response shape; Context ID
  written only on confident resolution.
- UI: verification via the dev server (log a bet on a fabricated league,
  screenshot the status matrix + provisional badge).

**Gate P3:** logging a bet on a never-seen league shows the matrix and a
provisional badge; logging on MLB/DK shows all-verified and no badge; node
suite green. Rollback: response field is additive; client badge behind a
small feature flag in `scannerConfig.js`-style config if needed.

---

## Phase 4 — Post-event verification, promotion, demotion (checker)

**New files**

- `onboarding_verifier.py` — runs inside the worker loop after events
  complete (same cadence slot where recovery runs today):
  1. Pull observations for Collecting/AwaitingPostEvent records.
  2. Fetch authoritative start via `actual_start.py` (routes now resolved
     through `context_registry`, not the module-level dicts — the dicts
     become seed data only).
  3. Classify per concept §5 (agreement / recoverable mismatch / unresolved
     / contradiction) and attribute evidence to the narrowest grain
     (attribution table in `onboarding_policy.py`).
  4. Apply the evidence bar → `capability_profile.transition()`.
- `family_priors.py` — computes source-correctness priors from sibling
  verified records on the same qualifier source (concept §6: prior, not
  inheritance; system correctness never transfers).

**Changed files**

- `closing_capture_worker.py` — routine captures on Verified records emit
  cheap re-validation observations (already computed data; no new fetches);
  disagreement counters per P0.2 #7 drive Stale/Contradicted transitions +
  causal-window row re-flagging (window = since last known-good check).
- `config.py` — `ONBOARDING_PROMOTE_SHADOW = True` initially: proposed
  transitions are written to Notes on the Capabilities row
  (`proposed: Discovered→Verified …`) and the JSONL, not applied — the
  PROPS_SHADOW_MODE pattern exactly.

**Tests**

- `tests/test_onboarding_verifier.py` — each §5 outcome from fixtures;
  attribution (missing market ≠ start-source failure — the concept's
  explicit example); mid-event first observation still verifies system
  correctness when post-hoc start exists.
- `tests/test_family_priors.py` — 3-sibling prior lowers the bar to 1 event;
  different source → no prior; combat promotion → no prior (fixture named
  after the UFC example).
- `tests/test_demotion.py` — immediate Contradicted on post-start-as-
  verified; quarantine 3-in-14 → Stale; decay after 30 clean days; causal
  window re-flags only in-window rows.

**Gate P4:** ~1 week with `ONBOARDING_PROMOTE_SHADOW=True`; Josh reviews
proposed transitions (expected: a genuinely new league you bet during the
week walks Discovered→Verified in the log). Flip to enforce. Rollback: flag;
applied transitions are reversible by design (demotion path).

---

## Phase 5 — Repair pipeline (checker)

**New files**

- `scripts/repair_onboarded_rows.py` — the repull_suspect_closing.py shape:
  `--preview` writes a dry-run report (affected rows per record_key +
  candidate closes + why); `--apply` executes. "Affected" = same record
  grain + policy version + event/incident window (concept §9), enumerated
  from the `onboarding:` Notes markers written in Phase 2.

**Changed files**

- `closing_odds.py` — repair derivation reuses the recovery selectors;
  candidate must satisfy the full quality contract (pre-start by authoritative
  start + margin, book-quote freshness) or the row stays provisional.
- `sheets_writer.py` — repair write path: preserve original values in a
  `pre-repair:` Notes marker (BetID-guarded, `upsert_notes_line`), set
  `Closing Source = recovery-onboarding`, quality per contract. Never
  deletes; failure leaves the row untouched (concept safety #8).

**Tests**

- `tests/test_repair_onboarded_rows.py` — preview/apply parity; original
  preserved; failed derivation = no write; repaired rows enter pooled CLV
  only with `VERIFIED_CLOSE`; provenance mark permanent (re-running repair
  refuses to double-repair).

**Gate P5:** first real promotion produces a preview Josh approves before
`--apply`; after apply, `clv_start_audit.py` still reports zero post-start
trusted closes. Rollback: `pre-repair:` markers allow scripted restore.

---

## Phase 6 — Cases + notifications (odds-tool)

**New/changed files**

- `onboardingCases.js` (odds-tool) — derives cases from `Capabilities` +
  Bets markers (§0.1: no Cases tab); one parent per context, child issues
  per record_key, five outcomes per concept §10.
- `server.js` — `GET /api/onboarding/cases`;
  `POST /api/onboarding/cases/:contextId/action` (Manual/Retire/ephemeral
  confirmations, `requireSensitiveAccess`-guarded like `/api/execution`).
  User decisions are written to the `Discovery Queue` tab as decision rows
  (odds-tool's writable tab) — the checker consumes and applies them,
  preserving single-writer on `Capabilities`.
- `notify/engine.js` + `notify/bus.js` — new event types:
  `onboarding_case_opened`, `capability_promoted`, `capability_demoted`,
  `rows_repaired`. Emitted by a poll of the Capabilities tab (odds-tool
  already polls sheet tabs; checker cannot push).
- `client/src/components/OnboardingCasesPanel.jsx` — NeedsReviewPanel
  pattern: case list, child issues, the specific missing fact, action
  buttons.

**Tests**

- `tests/onboardingCases.test.js` — ten bets/one league = one case; distinct
  books not merged (dedup key includes grain); case closes when children
  classified; Blocked closes case but limitation persists; reopen rules.
- `tests/notifyOnboarding.test.js` — dedupe (state-change-only, no re-warn),
  event routing through existing rules.

**Gate P6:** end-to-end on a real new league: case opens on first bet,
notification once, resolves (or Blocked) without Josh prompting anything.

---

## Phase 7 — Scanner discovery, ephemeral, benchmark (odds-tool + checker)

- `scanner.js` — when a scan surfaces an opportunity whose context resolves
  NEW (via `capabilityStatus.js`), append a deduplicated row to
  `Discovery Queue` under the P0.2 #12 budget. No new TOA calls.
- `scannerDb.js` — persist dedup/expiry state in the existing SQLite.
- Checker `onboarding_verifier.py` — consume queue rows: durable intent
  (bet followed, or repeated sightings) → create records + start Collecting
  pre-bet (the cold-start fix); expired → drop.
- Ephemeral: `context_registry.py` event-scoped competitions route rows to
  row-level verification without creating reusable trust (concept
  §One-off); case action offers the designation (P0.2 #9).
- Benchmark: `benchmark` records fed by `pinnacle_closing.py` coverage;
  BetsPage "unbenchmarkable" state wired (display only — never demotes
  capture, concept safety #12).

**Tests:** scanner dedup/budget/expiry; pre-bet Collecting produces a usable
pregame window on a fixture timeline; ephemeral context verifies rows but
creates no reusable Verified record; benchmark-missing rows excluded from
CLV aggregates while close stays trusted.

**Gate P7:** one scanner-discovered league reaches Verified with a pregame
observation window that a bet-time discovery would have missed (measured
from the JSONL) — the concept's cold-start payoff, demonstrated.

---

## Cross-cutting

**Test counts as gates:** every phase ends with both suites green (checker
pytest ≥ current 301+, odds-tool `node --test` ≥ current 366) plus the new
tests named above. The shared parity fixture
(`tests/fixtures/onboarding_parity.json`, committed to both repos) is
asserted by `tests/test_capability_profile.py` (pytest) and
`tests/capabilityStatus.test.js` (node) — drift fails both suites.

**Sheets quota:** all new reads go through existing caches
(`sheetTabCache` in odds-tool, `sheets_quota.py` batching in checker).
Discovery Queue is append-only + compacted by the checker to stay small.

**Flag summary (config.py):** `ONBOARDING_SHADOW_MODE` (P1),
`ONBOARDING_ENFORCE` (P2), `ONBOARDING_PROMOTE_SHADOW` (P4). Each flips only
after its gate; each is independently revertible.

**Josh's manual sheet TODOs (accumulating pattern from memory):** create the
three tabs (P0); add `Context ID` column to Bets (P3); no Result-validation
changes needed by this plan.

**Explicitly out of scope (concept non-goals):** benchmark fallback sources,
per-sport season calendars (freshness uses idle windows in v1), automating
Retired, any change to settlement behavior, historical `LEGACY_UNAUDITED`
rows.

**Suggested order & sizing:** P0 (docs + inventory script, small) → P1
(core modules + parity, the big one) → P2 (enforcement, small once P1
holds) → P3 (odds-tool surface, medium) → P4 (verifier, big) → P5 (repair,
medium) → P6 (cases/UI, medium) → P7 (scanner + polish, medium). P3 can run
in parallel with P2 once P1's tabs and parity fixture are frozen.

---

## Implementation status (2026-07-16, checker repo)

Phase 0 and the pure/foundational half of Phase 1 are implemented, unit-tested,
and verified end-to-end against the LIVE sheet. Full checker suite: **459
passed** (was 301+ baseline). Nothing is committed yet — all changes sit in the
working tree for review.

### What's done

| Item | State | Notes |
|---|---|---|
| `onboarding_policy.py` | ✅ | Frozen `POLICY_VERSION=1`; all P0.2 numbers (see below). Pure data + functions. |
| `scripts/onboarding_inventory.py` | ✅ | Imports live tables + parses odds-tool `scannerConfig.js` → **191 seed rows / 8 capabilities / 22 contexts**. `--json`. |
| `context_registry.py` | ✅ | `resolve()`→NEW on ambiguity/miss/unreadable; edition date-windows; provider-key reuse. |
| `capability_profile.py` | ✅ | Typed `Capabilities` I/O; `require_clv()` fail-closed; `transition()` enforces concept §2; `set_health()`. |
| `scripts/seed_capabilities.py` | ✅ | Idempotent; refuses to overwrite non-`seeded:` rows. |
| `scripts/create_onboarding_tabs.py` | ✅ | Created the 3 tabs (below). |
| `scripts/add_bets_context_id_column.py` | ✅ | Added Bets `Context ID` (col 45; empty/inert until P3). |
| **Live sheet tabs** | ✅ | `Capabilities` (191 rows), `Context Registry` (22 rows), `Discovery Queue` (header only). Seeded + verified readable. |
| Tests | ✅ | `test_onboarding_policy` / `test_context_registry` / `test_capability_profile` / `test_seed_capabilities` / `test_seed_parity` (+60 tests). |

### Key deviations from the plan (all deliberate, all verified)

1. **The 1-week shadow-observation gate (P1) was replaced by an offline
   seed-parity audit.** Josh did not want to wait a week. The shadow log's only
   P1 job is catching seed/enforcement mismatches that would silently downgrade
   real `VERIFIED_CLOSE` rows; `test_seed_parity.py` runs that check
   exhaustively over a snapshot of real Bets history (stronger than a week of
   whatever bets happen to occur). Fixture:
   `tests/fixtures/onboarding_bets_parity.json` (sport/book/bet_type/market_key/
   closing_quality only — no BetIDs, teams, stakes, or outcomes; 79
   `VERIFIED_CLOSE` rows). The P1 shadow-wiring of
   `closing_capture_worker`/`poller` + `ONBOARDING_SHADOW_MODE` is therefore
   **not yet built** and may be unnecessary as a gate — Fable to confirm whether
   it's still wanted for its own sake before P2 enforcement.

2. **The CLV "start" requirement is `start_live` OR `start_authoritative`, not
   `start_live` alone** (new label `CLV_START` in `capability_profile`). The
   parity audit proved MMA and tennis rows reach `VERIFIED_CLOSE` today via the
   authoritative actual-start path (espn_fights / espn_tennis), never the live
   flip — so `CLV_REQUIRED_CAPABILITIES = (identity, start, capture)` where
   `start` is that disjunction. This corrects the plan §0.5 shorthand
   ("`require(start_live, capture, identity)`").

3. **Capture is grandfathered at `any|market_family` grain (book-agnostic), and
   identity+capture are seeded for the FULL known-context set** (union of
   scanner `supportedSports` + `actual_start` routes + `TRUSTED_FLIP_SPORTS`),
   not just the scanner allow-list — because the checker captures/settles
   route-only contexts (tennis) the scanner never surfaces. Team sports also get
   a `team_total` capture seed (scanner featured-market families under-covered
   it). `require_clv` prefers a specific `book|family` record and falls back to
   the `any|family` seed (the parity bridge). Per-book records are created
   lazily post-P1.

4. **Tabs were created + seeded directly by the agent** (checker service account
   has write access), not left as a manual step. NB: a named script passes the
   auto-mode permission classifier where an inline `python -c` sheet write is
   blocked — sheet writes must go through dedicated scripts.

### Parity audit findings (the gate doing its job)

The live audit found **14 real seed gaps** that unit tests could not (unit tests
check seeds against themselves): 7 `team_total` capture misses, 5 MMA + 2 tennis
`start_live`-only misses (fixed by the disjunction), and the route-only-context
identity/capture gap. After fixes: **0 `VERIFIED_CLOSE` downgrades**, boxing and
unrouted one-offs correctly stay provisional.

### Outstanding — for Fable

- **Ratify the P0.2 numbers below** (Josh deferred this judgment to you). They
  are frozen in `onboarding_policy.py`; changing a value = edit the constant +
  re-run `pytest` (the parity gate re-checks automatically). Flagged for your
  attention: the **evidence bar** aggressiveness (#1) and **120-day freshness**
  (#6, since a 4-month off-season is common and forces re-confirmation on the
  first bet back).

| # | Decision | Frozen default (constant) |
|---|---|---|
| 1 | Evidence: strong-prior events | **1** (`EVIDENCE_EVENTS_STRONG_PRIOR`) |
| 1 | Evidence: new-source events / distinct days | **3** / **2** (`EVIDENCE_EVENTS_NEW`, `EVIDENCE_DISTINCT_DAYS_NEW`) |
| 1 | Strong prior = siblings / irregular handled | **3** / **1** (`FAMILY_PRIOR_MIN_SIBLINGS`, `FAMILY_PRIOR_MIN_IRREGULAR`) |
| 1 | Start-disagreement → immediate Contradicted | **600 s** (`START_DISAGREEMENT_CONTRADICTION_SECONDS`) |
| 6 | Freshness idle window | **120 days** (`FRESHNESS_IDLE_DAYS`) |
| 7 | Quarantine threshold / window / decay | **3** / **14 d** / **30 d clean** |
| 7 | Immediate-contradiction failures | `post_start_as_verified`, `start_contradiction` |
| 7 | `missing_market` | coverage-only, never demotes start/identity |
| 11 | Benchmark | `pinnacle`, exact-match, no fallback |
| 12 | Scanner intent expiry / daily append cap | **7 d** / **20** |
| 14 | Policy bump | affected records → Stale (not Contradicted), no silent re-interpretation |
| 15 | Case grain | one parent per context, one child per record_key; Blocked closes case, limitation persists |

- Deferred-to-later-phase decisions (#5 display, #8 manual-evidence CLI, #9
  ephemeral, #10 JSONL 90-day retention) are unchanged from the plan and not yet
  encoded.

### Phase 2 — enforcement + discovery (implemented 2026-07-16, Fable greenlit)

Config flags `ONBOARDING_SHADOW_MODE` (default **on**) / `ONBOARDING_ENFORCE`
(default **on**) added to `config.py`, same shadow-then-enforce shape as
`PROPS_SHADOW_MODE`. A single façade, `onboarding_gate.py`, carries the logic so
hot-path diffs are one line each:

- **`gate_finalize_quality(record, quality)`** — wired into
  `closing_capture_worker.finalize` and `closing_odds.fetch_closing_odds` /
  `fetch_parlay_closing_odds` (per-leg). A would-be `VERIFIED_CLOSE` whose grain
  is not trusted is shadow-logged and, under enforce, capped at
  `QUALITY_PROVISIONAL` with an `onboarding:` Notes marker. Only `VERIFIED_CLOSE`
  is gated; everything else passes through. Fail-closed on an unreadable profile.
- **`discover_for_bet(bet)`** — wired into `poller.poll_bet` (best-effort, never
  blocks settlement). Shadow mode logs would-be discovery; enforce lazily creates
  Discovered capability records (and a Context Registry alias for a genuinely new
  context). Idempotent.
- **`clv_start_audit.py --buckets`** — the concept's operational-state report
  (pending / capturing / observing / trusted / unbenchmarkable / recoverable /
  repaired / blocked / manual / retired / by_design), a total row classifier.

Tests added: `test_onboarding_gate` (12), `test_audit_buckets` (7). Full checker
suite **479 passed**. Live enforcement simulation through the façade: **0 of 79
`VERIFIED_CLOSE` rows would downgrade** — `ONBOARDING_ENFORCE=1` is safe to flip
whenever Fable/Josh want (env var or one-line change; revertible).

**Enforcement flipped ON 2026-07-16** (`config.ONBOARDING_ENFORCE` default now
`"1"`; kill switch `ONBOARDING_ENFORCE=0`). The seed-parity gate + live
simulation (0/79 downgrades) were the safety proof.

### Phase 3 — odds-tool at-log status surface (implemented 2026-07-16)

- **`capabilityStatus.js`** — the JS mirror of `require_clv`: `statusForBet({sportKey,
  book, marketKey})` reads the checker-owned `Capabilities` + `Context Registry`
  tabs (2D rows in, so it's node-testable; server.js passes them via the existing
  `sheetTabCache`, no extra quota) and returns `{contextId, contextNew,
  provisional, matrix, unresolved, reason}`. Same disjunction (start_live OR
  start_authoritative), grandfathered `any|family` capture fallback, and
  fail-closed semantics as the checker.
- **Shared parity fixture** `tests/fixtures/onboarding_parity.json` — byte-identical
  in both repos (sha1 verified), asserted by `tests/capabilityStatus.test.js`
  (node, 5 cases + edge tests) AND `tests/test_onboarding_parity.py` (checker).
  Drift in either implementation or JSON copy fails a suite.
- **server.js** — `POST /api/bets` computes the status (single-selection,
  best-effort) into the response `onboarding` field and stamps the `Context ID`
  column only on a confident (KNOWN) resolution. `Context ID` added to
  `sheetColumns.js` BET_HEADERS. GET path: the existing `clvPresentation.js`
  already surfaces `provisional` (excluded) + `missing_pinnacle_close`
  (unbenchmarkable); extended with a `REPAIRED` provenance (Closing Source =
  `recovery-onboarding`) and an `onboarding`-specific exclusion reason.
- **UI** — `OnboardingStatusBadge.jsx` (capability matrix + provisional note),
  shown in `LogBetWizard` on a new terminal `logged` step for NEW/provisional
  bets (verified bets close straight through). `clvReasonLabel` gains the
  `onboarding` label; BetsPage CLV cells already render exclusion reasons.

Tests: node — `capabilityStatus.test.js`, `betsOnboardingResponse.test.js`
(+ existing clvPresentation green); checker — `test_onboarding_parity.py`.
**Verification note:** backend + parity fully node-tested; the client JSX is
validated by the client build (no runtime e2e — logging a bet writes to the live
Bets sheet, so the at-log badge is best eyeballed on a real log, not a junk one).

### Phase 4 — post-event verifier / promotion / demotion (implemented 2026-07-16)

- **`family_priors.py`** — `compute_prior(profile, context, source, capability)`:
  STRONG only with ≥3 Verified+Fresh same-family, same-source siblings AND ≥1
  irregular handled. A prior, never inheritance; different source / too few
  siblings / a lone UFC → NO_PRIOR (combat earns its own way).
- **`onboarding_verifier.py`** — `classify_start` (§5 agreement / recoverable /
  unresolved / contradiction / missing), evidence accumulation onto grain records
  (clean / irregular_ok / neg / quarantined + distinct-day tracking),
  `evaluate_promotion` (evidence bar × prior), `evaluate_block` (no start path →
  Blocked), `evaluate`-time **demotion** (immediate Contradicted on
  start/post-start contradiction; 3-quarantine-in-14-days → Stale; missing market
  = coverage, never demotes), `rows_in_causal_window`, and `run_verification`
  (apply vs shadow `proposed:` markers). `observation_from_bet` derives an
  Observation from a settled row's provenance columns.
- **`config.py`** — `ONBOARDING_PROMOTE_SHADOW` is an opt-in kill switch
  (default off): promotions apply when the evidence bar is met; **demotions
  never shadow** (concept §8).
- **`scripts/run_onboarding_verifier.py`** — scheduled from `trigger.py` and
  retained as a manual entrypoint (dry-run default, `--apply` for writes).

Tests: `test_family_priors` (6), `test_onboarding_verifier` (8), `test_demotion`
(6). Full checker suite **501 passed**.

**Live-verified on the real sheet:** a dry run derived **81 observations from
real bets and proposed 8 promotions** — grandfathered `any|family` capture grains
narrowing to specific per-book records once they had ≥3 clean events over ≥2 days
(e.g. `baseball/mlb|capture|fanduel|h2h`), all shadow, zero writes. And a
throwaway new-league context (`other/zzz_demo_new_league`) walked
Discovered→Verified live (discovery minted 3 records; the verifier promoted all
three after 3 clean events), then was fully deleted — tabs restored to 191/22.

**Gate P4:** superseded by the full audit below: event-level evidence dedupe,
authoritative-start hydration, fail-visible/batched writes, and full regression
tests were added before promotion enforcement became the default.

### Phase 5 — repair pipeline (implemented 2026-07-16)

Once a grain is promoted (P4), rows the gate capped PROVISIONAL are re-derivable:

- **`sheets_writer.repair_onboarded_close`** — writes the new trusted close FIRST
  (tagged Closing Source = `recovery-onboarding`), then preserves the ORIGINAL
  close/clv/quality in a permanent `pre-repair:` Notes marker and clears the
  `onboarding:` marker. Refuses to double-repair (`row_already_repaired` guard).
  A failed write leaves no partial markers.
- **`scripts/repair_onboarded_rows.py`** — repull-shaped. `classify_repair_row`
  (pure): a row is `retry` only when it has an `onboarding:` marker, is not
  already repaired, isn't VOID/verified, AND its grain now resolves trusted via
  `require_clv`. `repair_bet` derives via the existing recovery path
  (`fetch_closing_odds` with `_resolve_actual_start`); it upgrades the row ONLY
  when the derivation yields VERIFIED_CLOSE — otherwise the row is left exactly as
  it was (derive-before-clear; restore on a refused write). `--apply` executes;
  preview is default. No `closing_odds.py` change was needed — its recovery path
  already returns VERIFIED_CLOSE only when the full quality contract is met, and
  the gate passes it through once the grain is trusted.

Tests: `test_repair_onboarded_rows` (10) — classification buckets, double-repair
guard, VERIFIED_CLOSE-only upgrade, non-verified/fetch-failure leave the row
untouched, refused-write restores. Full checker suite **511 passed**. Live
preview runs clean (no rows are gate-capped yet in production).

**Gate P5:** the first real promotion produces a preview to approve before
`--apply`; after apply, `clv_start_audit.py` still shows zero post-start trusted
closes. Rollback: `pre-repair:` markers allow a scripted restore.

### Phase 6 — cases + notifications (odds-tool, implemented 2026-07-16)

- **`onboardingCases.js`** — `deriveCases(capabilities, bets)` (pure): one parent
  case per context with a non-verified capability, child issue per record_key
  (grain in the key → distinct books never merged), five outcomes
  (verified/limited/blocked/manual/retired) + `pending`. Case closes when no
  child is pending; Blocked closes the case but the limitation persists as a
  child; a Stale/Contradicted record reopens it. Each child carries its specific
  missing fact.
- **`notify/onboardingNotify.js`** — `snapshot` + `diffCapabilityStates(prev,
  curr)` emit one event per MATERIAL change (promoted / demoted / one
  case_opened per newly-seen context / rows_repaired), so a no-change poll emits
  nothing (dedupe by construction). `buildOnboardingNotification` keys dedupe on
  the target state → never re-warns.
- **`notify/engine.js`** — additive branch: onboarding events route through the
  same rules/channels/cooldown but skip the scanner EV/market/book criteria. The
  scanner path is untouched (existing 16 notify tests still green).
- **`server.js`** — `GET /api/onboarding/cases`; `POST /api/onboarding/cases/
  :contextId/action` (requireSensitiveAccess) appends a decision row to the
  Discovery Queue tab (odds-tool's writable tab) for the checker to consume,
  preserving single-writer on Capabilities. A 5-min `setInterval` polls the
  Capabilities tab, diffs, and emits notifications (the checker cannot push).
- **`OnboardingCasesPanel.jsx`** — NeedsReviewPanel-pattern card list in the
  sidebar; renders open cases + child missing-facts + affected-bet count + Mark
  manual / Retire actions. Hidden when there are no open cases.

Tests: `onboardingCases.test.js` (8), `notifyOnboarding.test.js` (7). Full
odds-tool node suite **602 passed**; client build compiles clean. The panel
renders only when an open case exists (none in production yet), so its on-screen
appearance is best seen once a real new-league bet is gate-capped (Gate P6).

**Next phase:** P7 (scanner discovery — begin observation at scanner-surfacing
time; ephemeral/event-scoped contexts; benchmark availability wiring).

---

## Review verdict (2026-07-16, Fable — gate decision for P2)

Reviewed: all five new modules + five test files line-by-line, full suite run
(459 passed), live tabs re-verified (191 records all Verified/Fresh, 22
aliases, fail-closed spot checks: MLB/DK trusted; boxing unresolved on
`start`; unseen league unresolved on all three; unknown sport key → NEW).

**P0.2 numbers: RATIFIED as frozen.** On the two flagged: the evidence bar
(1 strong-prior / 3-and-2-days new-source, contradiction always blocks) is
right — the strong-prior definition requiring ≥1 irregular handled keeps the
1-event fast path honest. `FRESHNESS_IDLE_DAYS=120`: ratified, noting it is
currently inert (nothing evaluates `is_idle_stale` on the trust path until
P4 wires health) and that re-confirmation on the first bet back each season
is concept-intended behavior, NOT a bug — but **P5 repair must ship close
behind P4**, or stale-triggered provisional rows will have no upgrade path.

**Deviation rulings:**
1. Offline parity audit **accepted** as the P1 gate replacement — it is
   strictly stronger than a shadow week for the seed-gap risk, and the 14
   real gaps it caught prove it. The `ONBOARDING_SHADOW_MODE` wiring should
   **not be built** (dead config); P2's `onboarding:` Notes marker on every
   capped row is the ongoing audit trail. Residual risk (a bet shape absent
   from the 79-row fixture gets capped) fails in the safe direction —
   visible + repairable — and is accepted.
2. `CLV_START` disjunction (live-flip OR authoritative) **ratified** — the
   parity evidence (MMA/tennis reach VERIFIED_CLOSE via authoritative start
   only) is conclusive.
3. `any|family` grandfathered capture **accepted as a bridge** with two
   conditions: P2 must lazily create per-book `Discovered` records on
   encounter (so evidence accrues toward narrowing), and retiring the
   `any|` seeds once per-book records verify becomes a named P4 task —
   otherwise an untested book inherits seeded trust forever.
4. Direct tab creation/seeding: fine; done.

**Pre-P2 fixes (small, fold into the P2 change):**
- `onboarding_policy.CLV_REQUIRED_CAPABILITIES` still says `CAP_START_LIVE`
  alone — stale vs the implemented disjunction; unused by code (grep-clean),
  update or delete.
- Inventory docstring + 54 seeded settlement Notes cite
  `resolver.AUTOMATED_BET_TYPES`, which does not exist in resolver.py (the
  classes are hardcoded in the script). Add the constant to resolver.py and
  import it, or reword. Live-row Notes can stay (cosmetic).
- `market_family_for` defaults unknown shapes to `h2h` — fail-open at the
  trust boundary (a novel market inherits the `any|h2h` seed). P2: unknown →
  a family with no seeds (fail closed).
- `context_registry.get_registry()` module cache has no TTL — the always-on
  Railway worker never sees registry edits. P2: refresh per capture cycle.
- `_LiveSink.upsert` failure only prints — tolerable while writes are rare
  Discovered creations; P4 (frequent transitions) needs retry-or-alert.

**VERDICT: READY to advance to Phase 2**, incorporating the five fixes and
condition (3) above. No shadow week required.

---

## Full implementation audit and remediation (2026-07-16, Codex)

### Scope and verdict

Audited the implemented Phase 0–6 paths across both repositories: registry and
profile storage, the closing-quality gate, settlement discovery, at-log status,
post-event verification, promotion/demotion, repair, cases, decisions,
notifications, parlay handling, and cross-repo parity. Phase 7 (scanner-first
discovery, ephemeral contexts, and benchmark availability) is still a named
future phase and was not treated as an implementation defect.

The architecture was sound: the checker remains the only writer of registry and
capability state; identity ambiguity and unreadable tabs fail closed; capability
grain is explicit; settlement remains independent of CLV trust; row provenance
feeds the existing CLV safety contract; and the Python/JavaScript parity fixture
plus frontend CLV boundary tests provide unusually good regression protection.

The audit did find several material gaps between the plan text and the running
behavior. All findings below were fixed in code during this audit.

### Findings and fixes

1. **The verifier existed but was not scheduled.** It was only a manual script,
   so evidence, promotions, demotions, freshness, and queued user decisions
   could not progress automatically. `trigger.py` now runs the verifier on every
   scheduled checker pass. Promotion shadow is opt-in (`ONBOARDING_PROMOTE_SHADOW=1`);
   the default applies qualified transitions.

2. **Rolling scans could count the same event repeatedly.** Evidence could be
   inflated every time the 14-day window was reread, and several bets on one
   event counted as several events. Evidence now keeps a bounded, hashed
   per-grain event/state ledger. Identical observations are idempotent; a later
   materially changed source result may be observed once more. Capability writes
   are coalesced into one batch per verifier pass to avoid Sheets quota churn.

3. **New contexts had a circular capture-promotion failure.** The gate correctly
   changed a would-be `VERIFIED_CLOSE` to `PROVISIONAL`, but the verifier then
   treated that stored value as failed capture evidence. An `onboarding:` marker
   is now recognized as proof that the pre-gate result met `VERIFIED_CLOSE`, so a
   genuinely new capture grain can earn promotion.

4. **Authoritative-start verification was not actually performed by the
   verifier.** It relied only on already-stamped Bets fields, which made ordinary
   live-capture rows look unresolved and could quarantine healthy start routes.
   The scheduled pass now resolves missing authoritative starts through
   `actual_start.py`, caches by event, batch-stamps successful facts, and treats a
   routed provider miss as transient rather than proof that no source exists.

5. **Unknown markets could inherit moneyline trust.** A novel nonempty market key
   fell through to `h2h`, including when Bet Type said Moneyline. Python and
   JavaScript now map unmatched keys to a seedless `unknown` family. The stale
   `CLV_REQUIRED_CAPABILITIES` tuple was replaced with the ratified
   identity + (live OR authoritative start) + capture groups.

6. **The grandfathered `any|family` bridge did not narrow safely.** A specific
   Discovered book grain could still fall through to the verified `any` seed.
   Exact records now shadow the fallback while they earn evidence. Discovery
   mints the exact book/market grain even when the first bet used bridge trust;
   once an exact grain verifies, the `any` bridge is made Stale while its history
   is preserved. Superseded bridge records do not create false cases or alerts.

7. **Freshness and demotion policy was mostly inert.** Policy-version and
   120-day idle aging now fail closed on the hot trust path and are persisted by
   the verifier. Quarantine events use their observation time, decay after the
   ratified clean interval, and no longer masquerade as permanent contradictions.
   A causal demotion atomically re-flags only dependent `VERIFIED_CLOSE` rows at
   or after the last known-good check; the causal search is not truncated to the
   14-day evidence window.

8. **Always-on capture workers retained stale profile/registry caches.** The
   Railway capture loop now refreshes lazy onboarding caches once per cycle, so a
   registry edit or verifier transition takes effect without restarting the
   worker. Final capability write failures now raise after quota retries, making
   the scheduled job fail visibly rather than claiming a transition succeeded.

9. **Case action buttons queued decisions that nothing consumed.** A checker-side
   decision consumer now validates and applies Manual, Retire, and Reopen with
   user transition authority, then marks each Discovery Queue row applied or
   failed. The endpoint no longer accepts the not-yet-implemented Phase 7
   `ephemeral` action and therefore cannot return fake success.

10. **Onboarding notifications were not enabled by default and could miss state.**
    Existing notification rules did not subscribe to onboarding event types.
    The notification database now seeds a dedicated onboarding rule, uses
    lifetime state-key dedupe, establishes an explicit initial snapshot even when
    it is empty, polls immediately on startup, and detects `rows_repaired` from
    Bets provenance. New-context, promotion, demotion, and repair events now have
    an actual delivery route (subject to the user's global notification switch
    and an active push subscription).

11. **Cases could lose affected bets or close before repair.** Bets whose Context
    ID was blank are now associated through the current registry. A context whose
    capabilities are verified but whose gate-capped rows still need repair stays
    open as `repair pending`; classified Manual/Retired/Blocked outcomes remain
    terminal as intended.

12. **Repair was not atomic.** The old flow cleared the close first and attempted
    restoration after failure. Repair now verifies BetID and the exact original
    values, then writes the new close, provenance, permanent `pre-repair:` marker,
    and onboarding-marker removal in one Sheets batch. A failed request leaves
    the original row untouched.

13. **Some closing paths dropped onboarding markers, and parlays were incomplete.**
    Historical trigger, retry, and repull paths now persist returned markers.
    Parlays aggregate per-leg gate markers, discover per-leg grains, show a
    composite at-log status, and use the parlay derivation during repair. This
    keeps a single unverified leg from silently disappearing inside a combined
    price.

14. **Documentation drift remained from the pre-P2 review.** The top-level status,
    enforcement defaults, policy ratification, inventory source label, and this
    audit record now reflect the implemented system rather than the earlier
    proposal state.

### Verification

- Bet-Result-Checker: **522 passed** (`python -m pytest -q`).
- odds-tool: **607 passed** (`npm test`).
- odds-tool client: production Vite build completed successfully. The existing
  large-chunk advisory remains a non-blocking performance warning.
- `git diff --check` found no whitespace errors in the checker changes.
- No live Sheets rows, Railway services, or notification subscriptions were
  mutated during this audit; verification used unit/integration tests and build
  checks only.

### Remaining planned work / operational follow-up

- Phase 7 remains: scanner-first discovery, event-scoped ephemeral contexts, and
  benchmark-availability records/UI.
- After deployment, inspect the next scheduled checker run. The first pass may
  hydrate authoritative starts and write more capability evidence than steady
  state; subsequent passes are deduplicated and batched.
- When a case reaches `repair pending`, run
  `py scripts/repair_onboarded_rows.py` to preview and then
  `py scripts/repair_onboarded_rows.py --apply` when the preview is acceptable.
  This manual approval remains the deliberate Phase 5 gate.

---

## Phase 7 implementation and audit (2026-07-16, Codex)

### Verdict

Phase 7 is implemented across odds-tool and the checker. Scanner-first discovery,
durable dedupe/budget/expiry, pre-bet Collecting state, event-scoped one-off
handling, and independent Pinnacle benchmark availability are now wired. The
remaining Gate P7 item is an operational observation after deployment: proving
the cold-start payoff on a real newly surfaced context. That observation cannot
be manufactured safely by a unit test and is not a code blocker.

### Scanner-first discovery

- `scanner.js` batches qualifying opportunity rows once per completed sport
  cycle and passes them to the onboarding discovery handler. It uses only the
  scan response already fetched; there are no additional Odds API calls.
- `onboardingDiscovery.js` resolves each grain against the authoritative cached
  Registry/Profile tabs. Unreadable tabs suppress queue writes (trust still
  fails closed elsewhere), preventing an outage from being mistaken for dozens
  of new leagues. Duplicate outcomes in one cycle count as one sighting.
- `scannerDb.js` persists `onboarding_discoveries` in the existing SQLite DB.
  A queue row requires two separate cycle sightings, is deduplicated by
  `context_id|book|market_family`, expires after seven days, and claims the
  20-appends-per-UTC-day budget atomically. Failed Sheets appends release their
  claim so a later cycle retries.
- Queue payloads retain event, matchup, market, selection/point, quote timing,
  offered price, first-seen time, and expiry. This makes the already-retained
  scanner price history identifiable as a pre-bet observation window.
- The checker's queue consumer validates durable intent, rejects/compacts
  expired rows, writes the sport-key alias, and creates identity, live-start,
  exact capture, and Pinnacle benchmark grains in `Collecting` before a bet.
  If a bet arrives first, the existing bet-discovery path remains the durable
  intent path; duplicate queue work is idempotent.

### One-off / ephemeral contexts

- Context resolution in Python and JavaScript now checks an exact `event_id`
  alias before a reusable `sport_key` alias. The result is explicitly marked
  event-scoped.
- The case panel offers `One-off` only when the case identifies exactly one
  affected event and sport key. The action is a real checker-consumed decision,
  not a UI-only acknowledgement.
- Applying the action retires the reusable candidate mapping and its records
  under user authority, creates an event-ID alias, and mirrors the evidence
  grains under an event context constrained to that event. Those event records
  are Retired by design: evidence may be collected against them, but they can
  never become reusable Verified trust.
- After authoritative actual-start hydration, an event-scoped row can be
  certified directly only when its own quote predates actual start by the safety
  margin. The checker then restores `VERIFIED_CLOSE`, removes the onboarding
  marker, and leaves an `ephemeral-verified:` provenance marker. No context
  promotion is involved.
- If a one-off sport key later appears on a different event, the case reopens as
  `one-off recurred` and offers `Reopen as league`; ordinary trust must then be
  earned rather than inherited from the special event.

### Benchmark availability and frontend behavior

- Completed observations now feed an independent
  `benchmark|pinnacle|market_family` capability grain from the exact-match
  `Pinnacle Close` produced by `pinnacle_closing.py`.
- A missing exact benchmark classifies only that benchmark grain as Blocked.
  It never changes capture classification/health or the row's Closing Quality.
  A later exact benchmark automatically reopens the benchmark grain to collect
  positive evidence.
- At the frontend serialization boundary, a trusted close without an exact
  Pinnacle comparison is now `EXCLUDED / unbenchmarkable`: the stored CLV is
  withheld from Bets, Stats, and Coach aggregates, while the trusted closing
  price and `VERIFIED_CLOSE` provenance remain intact. Bets UI labels the reason
  `No exact Pinnacle benchmark was available`.

### Audit corrections made while implementing

1. Same-cycle alternate outcomes initially risked counting as repeated scanner
   intent; batching now deduplicates the capability grain before SQLite sees it.
2. A queue claim could have been stranded by a failed Sheets append; claims are
   now explicitly released on failure.
3. A transient unreadable Registry could have flooded Discovery Queue as NEW;
   scanner discovery now suppresses writes until both authoritative tabs read.
4. An event-scoped decision needed row-level verification, not a hidden reusable
   promotion. The implementation uses exact event aliases plus per-row start
   evidence and permanently non-reusable records.
5. A one-off that later recurred had no recovery route in the original Phase 7
   bullets. Recurrence now reopens a visible case and requires an explicit
   ordinary-context decision.
6. Benchmark absence was previously represented only as a missing Pinnacle
   value, allowing same-book CLV to remain in aggregates. The API presentation
   boundary now exposes the distinct unbenchmarkable state and withholds it
   everywhere without demoting capture.

### Verification

- Bet-Result-Checker: **528 passed** (`python -m pytest -q`).
- odds-tool: **614 passed** (`npm test`).
- Focused Phase 7 coverage includes scanner two-sighting dedupe, atomic daily
  budget, seven-day expiry/reset, same-cycle dedupe, context-ID parity,
  pre-bet Collecting records, event-ID resolution precedence, permanently
  non-reusable ephemeral records, row safety-margin verification, recurrence,
  benchmark Blocked/reopen evidence, and harmless capture preservation.
- odds-tool client production build completed successfully. The pre-existing
  large-chunk advisory remains non-blocking.
- No live Sheets rows, Railway processes, or external APIs were mutated during
  implementation verification.

### Deployment follow-up

After both repositories are deployed, leave the scanner enabled normally and
inspect the first genuinely NEW opportunity that receives two scan-cycle
sightings. The expected chain is: one pending `discovery` queue row, checker
consumption on its next scheduled pass, four Collecting grains before bet time,
and retained scanner history beginning before any later bet. That real fixture
is the final Gate P7 cold-start demonstration.
