# Resolver Expansion — COMPLETE (all phases shipped & audited)

**Owner:** Josh. **Final audit:** 2026-07-10 (Fable) — checker 301 pytest green,
odds-tool 366 node green, every Round-3 claim verified in code. **No further
implementation rounds are planned.** What remains is OPERATIONAL (Josh's checklist
below) plus a small polish backlog.

**Ruling principle that governed the build:** accuracy and reliability over
everything — every failure path routes to manual review; no branch defaults to a
result. This held in all three audited rounds.

---

## What auto-resolves now (end state)

| Market / case | Resolution |
|---|---|
| Moneyline, Spread, Total, Draw (game sports) | auto (TOA /scores) |
| Alternate spreads / totals | auto (same parsers) |
| **Team totals** ("X Team Total Over 4.5") | auto, vs the team's own score |
| **Quarter-line Asian spreads/totals** | auto, `HALF WIN`/`HALF LOSS` (two half-stakes) |
| Tennis moneyline | auto (ESPN) |
| **MMA / boxing moneyline** | auto (ESPN; draw→PUSH, NC→VOID, ambiguity→manual) |
| **MLB player props (singles + pick'em entries)** | auto via statsapi.mlb.com — **currently SHADOW MODE** (proposes in Notes, rings the bell, does not settle) |
| Pick'em power, all legs hit | settles at row odds (once shadow off) |
| Pick'em flex / any reduced entry | legs auto-resolved, payout = manual entry (site tables are app-truth) |
| Parlays of automatable legs | auto; a HALF or prop leg without a Pickem marker → manual |
| Kalshi / ReBet payouts | manual payout entry (by design, unchanged) |
| Non-MLB props, unparseable selections, suspended games, doubleheader ambiguity, corrupt markers | manual (by design) |

## JOSH's operational checklist (in order)

1. **Sheet Result-column data validation**: add `HALF WIN` and `HALF LOSS` to the
   allowed values BEFORE deploying Round 2+ — the poller's first half-result write
   fails otherwise. (Flagged twice; still the one blocking item.)
2. **Props shadow week**: deploy, then compare each `props-proposed:` Notes line
   against your own manual settlement for ~a week of slates. When satisfied, flip
   `PROPS_SHADOW_MODE = False` in config.py. The flag remains as a kill switch.
3. **Settling reduced/flex pick'em rows manually** (they stay manual even after
   shadow): use the **payout-entry path**, not a plain WIN apply — a reduced entry's
   payout comes from the site's table, and an odds-based P/L computation would be
   wrong. The `props-payout:` Notes line gives you legs + hit count as a copy job.
4. **First-real-settlement verifications (PROPOSED items)**: HALF LOSS fee treatment,
   %-of-stake-on-win fee on the scored half only, half-profit rounding
   (component-rounded), ESPN's draw/No-Contest status wording
   (`_classify_no_winner` fails to manual until confirmed).

## Audit notes on Round 3 (for the record)

- The ratified policies are encoded exactly: DNP void only on full boxscore absence
  (bench-listed players with 0 PA route to manual — conservative and correct);
  clean-Final requires BOTH `abstractGameState` and `detailedState` = "Final"
  (excludes "Completed Early"/suspended); doubleheaders are ambiguous → manual;
  exact-normalized name matching via the ONE shared normalizer (`name_match.py`,
  now also used by espn_fights); all dates via the ONE shared parser.
- The subtlest trap was handled: a `props-observed:` line that is PRESENT but
  unparseable routes to manual — it is never treated as "no marker" and re-observed.
- Shadow mode exercises the full two-pass machine (markers are stamped and
  re-verified even while shadowed), so the acceptance week tests the real thing.
- `upsert_notes_line` is BetID-guarded and prefix-self-replacing; it never touches
  Result/P/L.
- Pick'em rows are fenced from `combine_parlay_results` at load time
  (`is_parlay=False`), not just at poll time — the reduction≠product-odds trap is
  structurally closed.
- Flagged deviation accepted: reduced/flex pick'em routes to NEEDS_REVIEW + a
  `props-payout:` note rather than the literal MANUAL_PAYOUT_REQUIRED_BOOKS
  machinery. Functionally manual and safe; see checklist item 3 for the one
  behavioral consequence.

## Polish backlog (OPTIONAL — only if these ever annoy)

- Non-MLB pick'em entries (e.g. a UFC entry) currently route to manual with a
  "game not found" reason after an MLB schedule lookup — safe but confusingly
  worded. A sport gate with a clean "unsupported prop sport" reason would tidy it.
- Reduced/flex pick'em could adopt the literal manual-payout machinery
  (`MANUAL_PAYOUT_REQUIRED_BOOKS` flow) so P/L derives from the entered Payout
  automatically.
- `espn_tennis` remains dateless (its scoreboard ignores `?dates=`) — TODO in file.
- WNBA/NBA/NFL prop sources (same skeleton as `mlb_statsapi.py`) when those seasons
  matter — a new design conversation, not this plan.
