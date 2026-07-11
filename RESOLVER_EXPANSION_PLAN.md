# Resolver Expansion — Round 3 handoff (Phase 3: MLB player props)

**Audience:** implementing agent (Opus). **Owner:** Josh. **Rewritten:** 2026-07-10
(Fable), after auditing Round 2. **Repos touched:** this repo (primary) + `odds-tool`
(Notes stamping at pick'em log time).

**Ruling principle (unchanged, Josh verbatim intent): accuracy and reliability over
everything.** Every failure path raises toward manual review; no branch ever defaults
to WIN or LOSS. A notification is annoying; a wrong settlement is unacceptable.

**RATIFIED by Josh 2026-07-10 (do not re-litigate):**
1. ✅ done — Quarter-line half-results as `HALF WIN` / `HALF LOSS`.
2. Player-prop DNP: auto-VOID only when the player is entirely absent from the final
   box score; anything ambiguous (0 PA / 0 batters faced, suspended game) → manual.
3. Props settle only on Final + re-verify (stat unchanged across two observations
   ≥ 60 min apart); state lives IN the sheet row (Railway has no durable disk).
4. Pick'em entries: mode stamped into Notes at log time; power all-hit settles at row
   odds; any void/reduction or flex → legs auto-resolved, payout routed to
   manual-payout.

---

## STATUS — Rounds 1 & 2 SHIPPED and AUDIT-CONFIRMED (do not redo, do not refactor)

Audited by Fable 2026-07-10: checker 265 pytest green, odds-tool 366 node green, all
claims verified in code.

- **Team totals** auto-resolve against the team's own score (tried first in the Total
  branch, clean fallthrough; short-name matching reused).
- **MMA/boxing moneylines** auto-resolve via `sources/espn_fights.py` (exact-normalized
  matching, both-fighter bout identification, every ambiguity → manual). The F1
  date-format bug is FIXED via shared `date_utils.parse_sheet_date` (M/D/YYYY + ISO),
  used by both `espn_fights._dates_param` and the poller.
- **HALF WIN / HALF LOSS** shipped in BOTH repos. Resolver: `_is_quarter_line` +
  `_combine_halves` (impossible pairs raise → manual, better than the plan's assert)
  across spreads, game totals, AND team totals; the Phase-0 guard is gone as planned.
  P/L composes the scored half + pushed half from the existing branches (fee charged
  once, on the scored half; boost/category/rounding rules reused). Parlays containing a
  HALF leg raise → manual. Cross-repo parity fixture (−0.75, 1-goal win, $100 @ −110 →
  [45.45, 145.45]) asserted green in BOTH suites. odds-tool enumeration audit
  confirmed: `betReviewPl` mirror, `VALID_APPLY_RESULTS`, NeedsReviewPanel options
  gated to quarter-point selections, `betsSummary` (HALF WIN=win / HALF LOSS=loss),
  StatsPage/EntityDetailModal `normalizeResult` folding, BetsPage badges.

### ⚠ JOSH's outstanding actions (not code)
1. **Sheet Result-column data validation** must accept `HALF WIN` / `HALF LOSS` before
   the poller writes one, or the write fails. (Flagged in Round 2; still pending.)
2. Standing PROPOSED items to verify against the FIRST real settlements: HALF LOSS
   fee treatment (fee charged as on a loss), %-of-stake-on-win fee applied to the
   scored half only, and half-profit rounding (component-rounded, not
   full-precision-once — can differ by a cent from some books).
3. F3 carry: first real ESPN draw/No-Contest should confirm `_classify_no_winner`'s
   label scan (it fails to manual until then — safe).

### Cosmetic nits — fold into this round, do not ship separately
- `odds-tool/client/src/components/PromotionsPage.jsx` (~line 260): the promo-linked
  bet list colors results via raw `'WIN'`/`'LOSS'` comparison — a HALF WIN/HALF LOSS
  renders neutral gray. Use the shared normalize-and-fold treatment (StatsPage
  pattern). Cosmetic only.
- `sources/espn_tennis.py`: dateless (TODO left per plan — its scoreboard ignores
  `?dates=`). Leave unless trivial.

---

## Phase 3 — MLB player props via box scores (the ONLY remaining phase)

### Source: `sources/mlb_statsapi.py`
Official MLB Stats API (statsapi.mlb.com, free, no key): schedule-by-date → gamePk +
official status; boxscore → per-player batting/pitching. Settlement-grade — do NOT use
TOA or ESPN for MLB stats. Use `date_utils.parse_sheet_date` for schedule-by-date
(sheet dates are M/D/YYYY — the F1 trap, now solved once; never write a new date
parser).

### Scope guard (accuracy first)
v1 parses ONLY machine-generated selections from the pick'em/promo tools:
`"{Player} {Over|Under} {line} {StatLabel}"`. Anything else → manual, as today.
Stat-label map (from the odds-tool tools' MARKET_LABEL — keep in lockstep): Hits,
Total Bases, Home Runs, RBIs, Runs, Singles (hits−2B−3B−HR), Walks, H+R+RBI, Stolen
Bases → batting; Strikeouts, Outs, Hits Allowed, Earned Runs → pitching. VERIFY every
field name against a real boxscore payload before trusting it (commit one real payload
into tests/).

### The accuracy machinery (RATIFIED policies land here)
- **Name matching:** extract `espn_fights.normalize_fighter_name` into a shared
  normalizer (it IS the ratified standard: accents, case, punctuation, Jr/Sr/II
  suffixes) and use it for players — do not write a third normalizer. EXACT match
  only; no match → manual.
- **DNP:** player entirely absent from the final boxscore → leg VOID. Present but zero
  qualifying appearance (batter 0 PA; pitcher 0 batters faced) → MANUAL. Present with
  a real appearance and a 0 stat → genuine 0, settles.
- **Quarter guard does not apply here** — prop lines are X.5 (or integers, which push);
  do not import the halves machinery into props.
- **Final + re-verify (stateless):** the first run that sees the game officially Final
  writes a staging marker into the row's Notes
  (`props-observed: {leg:result,...} @ <iso>`) and does NOT settle. A later run
  ≥ 60 min after the marker re-fetches and compares: identical → settle + strip
  marker; different → manual + notification carrying both snapshots. Suspended/called
  games → manual. The marker format needs a strict parse/round-trip test — a corrupted
  marker must route to manual, never settle.
- **Pick'em entries (odds-tool change included in this phase):** the pick'em composer
  and DFS promo tool append `Pickem {mode} {n}-pick` to Notes via the existing
  /api/bets notes field. Checker, for a Parlay whose Notes match:
  - power/standard + ALL legs WIN → settle at the row's combined odds;
  - power, any loss, no voids → LOSS;
  - ANY void/reduced outcome, or mode = flex → write per-leg results + hit count into
    Notes, route to manual-payout (the `MANUAL_PAYOUT_REQUIRED_BOOKS` machinery) —
    reduced/flex payouts come from the site's tables, which are app-truth;
  - Parlay rows with Prop legs and NO Pickem marker → manual (predate the convention);
  - NEVER let `combine_parlay_results`'s void-drop odds math touch a Pickem-marked
    row (reduction changes the multiplier TABLE, not the odds product) — note it
    already raises on HALF legs; Pickem routing must short-circuit BEFORE parlay
    combining, not inside it;
  - never parse per-leg odds from Pickem rows (blank/combined by convention).
- **SHADOW MODE gate (the phase's acceptance gate — do not skip):** ship behind
  `PROPS_SHADOW_MODE = True` — compute and write the proposed result into Notes, do
  NOT settle, notifications still ring. Josh compares proposals vs his manual
  settlements for ~a week of slates; he flips the flag. The flag stays afterward as a
  kill switch.
- Tests: real boxscore fixture payloads for every stat label; accent/suffix matching;
  DNP-void vs ambiguous-manual; observed-then-confirm two-pass incl. corrupted-marker
  → manual; pick'em power settle / flex manual-payout routing; Singles derivation
  (H−2B−3B−HR).

WNBA/NBA/NFL box scores are a LATER phase — same skeleton, new source. Do not build
speculatively.

## Traps checklist
1. Fail toward manual, always — the poller's ValueError path is the safety net.
2. Sheet dates are M/D/YYYY — everything goes through `date_utils.parse_sheet_date`.
3. Railway = no durable disk — re-verify state lives in the sheet row's Notes, and a
   malformed marker routes to manual.
4. Pick'em reduced entries are NOT product-odds parlays; Pickem routing decides BEFORE
   `combine_parlay_results` ever runs.
5. One normalizer, shared (fights + props); one date parser, shared.
6. Shadow mode is the acceptance gate and the permanent kill switch.

## Report back to Josh
- The shadow-mode agreement report (proposals vs Josh's manual results across ~a week)
  — the flip decision is Josh's.
- Which stat labels appeared in real payloads unverified, and the committed fixture's
  provenance (which real game).
- Confirmation the PromotionsPage nit shipped.
- Anything this plan forbids that seemed necessary — flagged, not built.
