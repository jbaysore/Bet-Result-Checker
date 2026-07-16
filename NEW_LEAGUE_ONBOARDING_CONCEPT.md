# New Context Onboarding: Problem and Conceptual Solution

## Purpose

Define how the betting system should behave when a bet is placed in a
competitive context — a league, tournament, promotion, or special event — that
it has not fully encountered before.

This document describes the problem, the desired user experience, and the
conceptual operating model. It intentionally does not prescribe particular
files, schemas, APIs, jobs, or implementation details.

> **Terminology.** "Context" is used instead of "league" because the unit that
> needs onboarding is not always a league: it may be a tournament edition, a
> one-night combat promotion, or a single special event. Where "league" appears
> below it means "context." Choosing the right unit per case is itself part of
> the model (see *One-off and ephemeral contexts*).

---

## The problem

The system can recognize a context well enough to display odds and log a bet
without necessarily knowing everything required to calculate trustworthy CLV.
Support is not one capability. It is a combination of several independent
capabilities:

0. Resolving the context's identity consistently across every source
   (sheet text, odds feed keys, results/scores sources).
1. Discovering the event and its available markets.
2. Capturing the bettor's sportsbook price near the close.
3. Detecting the event's real start, rather than trusting its scheduled time.
4. Settling the bet from an authoritative result.
5. Recovering the actual start and closing market after the event if live
   capture fails.
6. Having a trustworthy benchmark to measure CLV against at all (a context can
   pass every other check and still have no sharp reference line).

A new context may support some of these capabilities but not others. And most
capabilities do not vary only by context: price capture and recovery vary by
**sportsbook and market family**, settlement varies by **market class**
(moneyline, spread, total, prop, …), and benchmark availability varies by
**benchmark source and market family**. "This league works" is therefore not a
well-formed claim; the well-formed claim is "this capability is verified at
this grain."

### Why identity comes first

Every other capability presumes the system knows *which* context a bet belongs
to — but the league name on the bet row, the odds-feed sport key, and the
results source's league identifier are different vocabularies. Two failure
modes matter:

- A rename or alias makes a **known context look new** (wasted onboarding,
  annoying but safe).
- A new context **aliases onto a verified one** and silently inherits trust it
  never earned. This is a fail-open hole in an otherwise fail-closed design.

Identity resolution is therefore capability #0, and ambiguity in identity must
always resolve toward "new," never toward "known."

Identity also needs hierarchy. A stable canonical identity should distinguish:

- The broad family or sport.
- A recurring competition or promotion.
- A season, tournament edition, or card.
- The individual event.
- Source-specific keys, display names, former names, and aliases.

Aliases point to canonical identities; they do not carry trust themselves.
Provider keys may be renamed or reused, so evidence belongs to the canonical
identity and the source mapping version that produced it. Family relationships
may inform onboarding, but they do not collapse distinct competitions or
editions into one trust record.

### Why scheduled start is not sufficient

Scheduled times can differ from real starts. Games may be delayed, begin
early, or be part of a card whose individual contests start at different
times. Using a scheduled time as though it were the actual start can capture an
in-play price and label it as a closing line. That contaminates CLV statistics
silently, which is worse than having no CLV for the row.

### The cold-start race

Observation cannot begin before the system knows the context exists — but the
first bet in a new context often arrives minutes before the event starts
(scanner-driven +EV bets cluster near close), and sometimes after the real
start. The pregame observation window the evidence model depends on may not
exist for the very bet that triggered onboarding. Any honest design must
(a) let observation begin mid-event and lean on post-hoc sources, and
(b) accept that the first affected bet will be repairable less often than
later ones.

### Current user burden

When support for a context is incomplete, the missing capability may not
become obvious until after bets have settled and their CLV is absent or
excluded. The user must then remember that the context was new, ask for an
investigation, and potentially request a code or configuration change.

This creates several undesirable outcomes:

- New contexts can appear to work even though their CLV will not be trusted.
- Missing support is discovered late rather than when the bet is logged.
- The same context may work for settlement but fail for closing odds or
  historical recovery.
- Upcoming bets can be incorrectly counted as historical failures simply
  because their closing prices are not due yet.
- Support knowledge can be duplicated across different parts of the system and
  become inconsistent.
- The user is expected to remember an operational task that should belong to
  the system.

### Required safety behavior

An unfamiliar context must never be silently treated as fully supported. If
the system cannot prove that a closing price is safely pre-event, that CLV
must be excluded from trusted statistics.

Failing closed protects the dataset, but exclusion alone is not a complete
user experience. The system must also make the limitation visible and actively
work toward resolving it.

---

## Core principles

Five ideas underpin everything below. They are stated once here so the rest of
the document can rely on them.

**1. Capabilities are verified at their natural grain.**
The grain must be narrow enough that success in one route cannot bless an
untested route. The conceptual defaults are:

| Capability | Natural grain |
|---|---|
| Canonical identity | context × source mapping |
| Event discovery and matching | context × discovery source |
| Real-start detection | context × start source |
| Sportsbook price capture and recovery | context × sportsbook × market family |
| Settlement | context × market class × result source |
| Benchmark availability | context × benchmark source × market family |
| Final CLV trust | individual bet row |

Records should be created lazily for combinations the user actually encounters;
the model does not require pre-creating every possible book and market.
A single "context state" exists only as a derived display summary — it is never
the source of truth, and nothing may consult that rollup to make a trust
decision.

**2. Rows are the unit of trust.**
A bet row's CLV is trusted because of evidence attached to *that row*, not
because of the state of its context. A row captured while a context was under
observation stays provisional until the row itself is re-evaluated —
promotion never retroactively blesses rows. Conversely, user-attested manual
evidence that meets the manual-evidence standard can make an individual row
trustworthy without making its context verified. Context capability records
only set defaults and gate automation.

**3. Two kinds of evidence, two kinds of trust.**
- *System correctness*: did our matching, parsing, and capture work for this
  exact capability path and grain? One clean, exactly-matched event can verify
  only the route, book, market family, and source actually exercised.
- *Source correctness*: does this source report true starts and results,
  **including when events go sideways** (delays, postponements, card
  reshuffles)? This is a distributional claim. One on-time game proves almost
  nothing about it, because on-time games were never the risk.

Promotion policy, family priors, and evidence quantity requirements all
follow from keeping these separate.

**4. Fail closed, loudly.**
Unknown means provisional or excluded, never trusted by default — and every
exclusion must be visible and carry a path to resolution (automatic, manual,
or a deliberate user decision to stop).

**5. Provenance and trust are independent.**
A value's origin — native capture, automatic reconstruction, or manual
evidence — must remain visible forever. Its trust verdict is a separate
decision based on the strength of the attached evidence. A reconstructed close
is not automatically weaker than a native one, and a native capture is not
automatically trustworthy.

---

## Desired outcome

The user should be able to place a bet in a new context without remembering to
perform a separate onboarding task.

The system should:

1. Detect that the context is new or only partially supported — ideally when
   the scanner first surfaces it, at latest when the bet is logged.
2. Log the bet normally unless the bet itself is invalid.
3. Show which capabilities are verified and which are not, as status rather
   than a one-time warning.
4. Capture useful evidence without admitting unverified CLV into trusted
   statistics.
5. Attempt to verify capabilities automatically during and after the event.
6. Promote capabilities when the evidence is sufficient — and keep verifying
   them afterward.
7. Re-evaluate **all affected** provisional rows when promotion or recovery
   makes a trustworthy close establishable, preserving permanent provenance.
8. Keep unresolved rows excluded and present one clear, consolidated action
   item per unresolved case.

The user should not have to remember that the context was new. An unresolved
onboarding investigation should remain visible until it reaches a classified
outcome: verified, limited, blocked, manual, or retired. A blocked outcome is
not trusted and may be reopened automatically if new evidence or a new source
appears; it exists so an impossible investigation does not remain "in
progress" forever or require the user to retire it manually.

---

## Conceptual solution

### 1. Treat support as a capability profile

Each context has one authoritative capability profile: the set of capability
records at their natural grains (see Core principle 1), each carrying its own
classification, activity, health, policy version, and evidence summary.

The profile conceptually answers:

- Is the context's identity resolved unambiguously across all sources?
- Can events and markets be discovered?
- Can the selected sportsbook and market family be priced?
- Can the live start transition from the selected source be trusted?
- Is an authoritative actual-start source available after the event?
- Can results be settled automatically? (per market class and source)
- Can missed closing prices be recovered historically? (per book and market
  family)
- Does a compatible benchmark line exist for the selected market family?
- Are there known exceptions for this context or event type?

Every consumer — logging, live capture, settlement, recovery, reporting —
relies on the same profile, so all parts of the system agree. Because the
consumers span more than one deployed system, where this single profile lives
is an explicit pre-implementation decision, not a detail (see *Decisions*).
One component owns profile mutations and transition decisions. If the profile
cannot be read, consumers fail closed rather than falling back to private
copies or remembered defaults.

### 2. Track classification, activity, and health separately

Each capability record has three orthogonal dimensions. This prevents
"evidence collection stopped," "the source is stale," and "this capability is
not trusted" from being collapsed into one ambiguous state.

#### Trust classification

| Classification | Meaning |
|---|---|
| Discovered | A bet or durable scanner-surfaced opportunity introduced this capability need. No trust conclusion yet. Absence of a record means Unseen. |
| Verified | Sufficient evidence of system and source correctness for the record's exact grain and policy version. |
| Limited | Trust is established only within explicit recorded constraints. Rows inside those constraints may be trusted; rows outside them may not. |
| Blocked | Automatic investigation reached a stable negative conclusion with current sources, such as no authoritative start source or no compatible retained market. Rows remain excluded. This is reopenable, not permanent retirement. |
| Manual | The user has classified this capability as requiring user-provided or book-specific information. A user decision, recorded with its reason. |
| Retired | The user has accepted permanent exclusion — "we agreed to stop trying." Always user-ratified, never self-assigned by the system, so cases cannot quietly disappear. |

#### Collection activity

| Activity | Meaning |
|---|---|
| Idle | No evidence collection is currently scheduled. |
| Collecting | Live or pre-event evidence is being collected. |
| Awaiting post-event | Live collection is complete and authoritative post-event evidence is pending. |

#### Verification health

| Health | Meaning |
|---|---|
| Not evaluated | No verification decision exists yet. |
| Fresh | The verification remains within its freshness policy and has no material contradiction. |
| Stale | The verification aged beyond policy or crossed a defined boundary. Historical row verdicts remain intact, but new rows fail closed until re-confirmation. |
| Contradicted | New evidence materially conflicts with the basis of verification. New rows fail closed and the causally affected rows are re-evaluated. |

The user-facing answer should be request-specific, not a single universal
"context is verified" label. For example: "start detection verified; DraftKings
moneyline capture verified; benchmark unavailable; settlement manual." A
context summary may roll these up for navigation, but it must show the matrix
of relevant capabilities and may never authorize trust.

A capability is eligible to authorize automatic trust only when its
classification is Verified — or the requested row falls inside a Limited
classification's explicit constraints — and its health is Fresh. Discovered,
Blocked, Manual, and Retired classifications never authorize automatic trust.
Manual evidence may still establish trust for an individual row under Core
principle 2.

#### Transition governance

Naming classifications is not enough; each transition needs an authority.

| Transition | Authority |
|---|---|
| Unseen → Discovered | Automatic when durable intent exists |
| Discovered → Verified | Automatic, on meeting the evidence bar (§7) |
| Discovered → Limited | Automatic, when only explicitly bounded support is proven |
| Discovered → Blocked | Automatic, when investigation terminates without a viable automatic path |
| Blocked → Discovered | Automatic when a new source, mapping, policy version, or relevant event creates a real path to re-evaluation |
| Verified/Limited health → Stale | Automatic under the freshness policy (§8) |
| Verified/Limited health → Contradicted | Automatic when attributed negative evidence crosses the severity threshold (§8) |
| Stale/Contradicted → Fresh | Automatic only after the relevant evidence bar is met again |
| Any → Manual | User decision (system may recommend) |
| Any → Retired | User decision only |
| Manual/Retired → Discovered | User decision (e.g., a new source appears) |

Collection activity changes automatically as events enter and leave useful
observation windows. It does not change the trust classification by itself.
If new intent falls outside a Limited record's proven constraints, the
in-scope trust remains intact and the newly required narrower capability grain
is Discovered separately.

### 3. Detect early: at surfacing, not only at logging

The earliest useful signal is not the bet — it is the scanner surfacing an
opportunity in an unfamiliar context. Beginning cheap observation then can buy
back the lead time the cold-start race otherwise destroys. Scanner surfacing is
not assumed to be rare or durable, however:

- Repeated sightings of the same context, event, book, and market are
  deduplicated.
- A surfaced-only discovery expires if the opportunity disappears and no bet
  or useful evidence follows.
- A persistent onboarding case is created only when there is a bet, repeated
  intent, or material evidence worth retaining.
- Scanner-driven observation has an explicit cost and rate budget.

At bet-log time, if any relevant capability is not eligible to authorize
automatic trust:

- The bet is still recorded.
- The user sees the context's current onboarding **status** — which
  capabilities are verified, which are pending, and the consequence. Status is
  shown every time but is not an alert; only material classification/health
  changes or a newly required user action notify.
- The affected capabilities begin or continue evidence collection
  automatically.
- The row is captured provisionally.

If the bet appears to be at or after the real start, or no eligible pre-start
quote has been retained, the status says plainly that this row may be
unrepairable. The bet is still logged.

The explanation describes the relevant consequence, not merely "unsupported."
For example, when real-start timing is the missing capability:

> New context. The bet and available prices will be recorded, but its CLV
> stays provisional until real-start timing is verified. Verification is
> automatic; you'll be notified either way.

### 4. Observe only contexts connected to real intent

Evidence collection runs when there is an actual bet — or a scanner-surfaced
opportunity that passes the durability rules above — to protect. The system
does not continuously monitor every possible context.

While collecting evidence for a capability, the system conceptually retains:

- Event identity and matchup agreement across sources.
- Pregame observations, when the window existed.
- The first observed live or completed state.
- The last eligible sportsbook price before that transition.
- Quote freshness and source timestamps.
- Source errors, empty responses, and ambiguous event matches.
- The exact capability grain and policy version under which each observation
  was collected.

Observation must be able to begin mid-event: a first bet placed near or after
the true start yields little or no pregame window, and the design treats that
as the normal case, not an exception. Such cases route more weight to
post-event verification (§5), and their rows are expected to be repairable
less often — an accepted cost, stated up front.

### 5. Verify after the event using an authoritative source

After completion, the system seeks an authoritative actual-start fact and
compares it with whatever live observations exist.

Possible outcomes:

- **Agreement:** live detection and the authoritative start agree closely
  enough to establish a safe pre-event close.
- **Recoverable mismatch:** the authoritative start is known and an earlier
  stored price can be selected safely.
- **Unresolved:** no authoritative start exists, the event is ambiguous, or
  the relevant sportsbook market is unavailable.
- **Contradiction:** sources disagree enough that automatic promotion would be
  unsafe.

Agreement or safe recovery allows affected rows to be upgraded (see §9).
Unresolved and contradictory rows remain excluded. Negative evidence is
attributed to the capability that failed: an identity mismatch counts against
identity/matching, a start mismatch against that start source, a stale quote
against price freshness, and a missing market against that book and market
family. A normal sportsbook decision not to offer a market is a coverage
limitation, not evidence that the actual-start source is unreliable.

### 6. Use family evidence conservatively — as a prior, not inherited trust

New contexts often belong to a familiar family. Framed by the two evidence
types, the permitted use of family evidence becomes precise:

- **Source correctness supplies a prior.** If a results provider's soccer
  scoreboards have proven reliable across five competitions — including
  delayed kickoffs — that evidence can reduce the burden for a sixth
  competition on the same provider. It does not make the sixth competition
  trusted before its own path is exercised.
- **System correctness does not transfer.** Whether *our* identity mapping,
  event matching, and market parsing work for the new competition must be
  re-earned with at least one clean, exactly-matched event. This is cheap —
  one event — but never skippable.

Consequences:

- A standard soccer competition may receive a strong prior from general soccer
  source evidence while still requiring its own identity and matching
  confirmation.
- A new combat promotion receives no prior from UFC per-bout timing when its
  promotion or source behavior differs materially.
- A special event receives no automatic prior for either its parent sport's
  schedule behavior or its market behavior.

Family evidence reduces the *quantity* of new source evidence required for an
ordinary context on a proven provider. It never removes the requirement to
verify system correctness for the exact context and capability grain, and it
can be overridden by known provider, competition, or event-type exceptions.

### 7. Promote on evidence — quality and quantity

An endpoint returning HTTP 200 proves nothing about whether it reports the
true start at the correct time. Worse, one cleanly observed on-time game
satisfies naive criteria while proving nothing about the failure mode CLV
protection exists for.

Promotion policy therefore considers, per capability:

- **System correctness:** at least one event matched exactly and
  unambiguously, with the full path exercised at the capability record's exact
  grain. This verifies only the route, source, book, and market family that
  were actually tested.
- **Source correctness:** supported either by a strong family prior on the
  *same source* (§6) plus context-specific confirmation, or by evidence
  accumulated across enough events on that source.
- Whether an independent authoritative start agreed with live detection.
- Whether the correct sportsbook quote and exact market were present and fresh
  (per book and market family).
- Whether the event type has unusual timing, such as cards or staged contests
  (stricter bar).
- Negative as well as positive observations, weighted by attribution,
  ambiguity, severity, and recency.

Where the family prior is strong, promotion can follow the first clean
context-specific event if the defined policy says that is sufficient. For a
genuinely new family or source, verification may intentionally take several
events. An irregular event handled correctly is especially valuable, but the
absence of an irregular event must not leave ordinary contexts unclassifiable
forever; ongoing re-validation continues to test that risk after promotion.

The evidence bar must be explicit and versioned before automatic promotion is
enabled. It defines:

- Minimum positive evidence for each capability type and family-prior level.
- Which failures are high-severity enough for immediate contradiction.
- How ambiguous observations are quarantined rather than counted as failures.
- How negative evidence accumulates and decays.
- Which source, matcher, parser, or policy changes invalidate old evidence.

Changing the evidence policy does not silently reinterpret history. Capability
records retain the policy version under which they were classified, and rows
are re-evaluated only when the new policy identifies them as affected.

### 8. Keep verifying: demotion, freshness, and regression

Verification is not permanent, and "move backward if a source changes" needs a
detection mechanism, because dedicated observation stops after promotion:

- **Continuous cheap re-validation:** every routine capture on a verified
  capability doubles as a lightweight check. Each disagreement is attributed
  to the narrowest plausible capability. Missing market coverage does not
  count against event identity or actual-start reliability.
- **Severity and thresholds:** a high-confidence post-start capture or
  authoritative start contradiction may immediately set health to
  Contradicted. A single ambiguous match or transient empty response is
  quarantined and investigated; it does not automatically demote unrelated
  capabilities.
- **Freshness:** Verified and Limited records carry timestamps. A record idle
  past a defined age, across a season boundary, or beyond its source/policy
  version becomes Stale. Trusted history is kept, but new rows depending on
  that record remain provisional until re-confirmation.
- **Causal re-evaluation:** contradiction re-evaluates rows within the
  identified incident window — normally since the last known-good check or
  source/version change. It does not indiscriminately invalidate all history.
- A material health downgrade generates a notification; silent downgrades are
  as bad as silent upgrades.

### 9. Repair all affected rows with permanent provenance

When promotion or post-event recovery establishes a trustworthy close:

- **Every affected** provisional row is re-evaluated — not just the first or
  triggering row. "Affected" means the row depends on the same capability
  grain, evidence/policy version, and relevant event or incident window. It
  does not mean every row sharing only the broad context.
- Evidence and derived values are conceptually separate. Repair derives a new
  candidate from immutable preserved evidence; it never silently overwrites
  the original capture. Failure leaves the existing row verdict unchanged.
- Provenance is permanent: native capture, automatic reconstruction, and
  manual reconstruction remain distinguishable forever.
- Trust is judged independently. A reconstructed close backed by an
  authoritative actual start and an exact, fresh pre-start quote may be
  trusted; a native capture with weak timing evidence may remain provisional.
- Rows that cannot be repaired stay provisional/excluded and roll up into the
  unresolved case (§10).

### 10. Preserve one visible, consolidated case

If automatic onboarding cannot finish, the user receives one context-level
case with capability-level issues and the affected rows. Ten bets on one
unonboarded league are one case, not ten notifications; ten different missing
capabilities are child issues, not ten unrelated top-level cases. The
deduplication key includes the exact capability grain so distinct books or
market families are not incorrectly merged.

Each case identifies the specific missing fact:

- No actual-start source is available.
- The scores source did not expose a live transition.
- The event could not be matched unambiguously.
- The sportsbook did not retain the required market.
- No benchmark line exists for CLV comparison.
- A manual or book-specific source is required.

Each issue reaches one of five user-visible resolutions:

1. **Verified** — automation eventually succeeded.
2. **Limited** — automation proved explicitly bounded support and records what
   remains outside it.
3. **Blocked** — automation completed but current sources cannot establish
   trust. Rows remain excluded, the reason remains visible, and new evidence
   can reopen the issue automatically.
4. **Manual** — the user supplies or commits to supplying qualifying evidence.
   Manual evidence can make individual rows trustworthy but never promotes the
   context automatically.
5. **Retired** — the user explicitly accepts permanent exclusion.

An automatic investigation can therefore finish without pretending that the
capability works and without requiring the user to retire it. Blocked and
Limited issues leave a persistent limitation in the capability profile, while
the active investigation case may close. They reopen when a new source,
mapping, policy version, or qualifying opportunity creates a real path forward.

Manual evidence is an evidence package, not an unsupported assertion. It
records the fact being supplied (start, price, result, or other), its source or
supporting artifact, when it was observed, who attested it, and any confidence
or limitation. Evidence without adequate support remains visible but cannot
enter trusted aggregate CLV.

---

## Provisional CLV: what the user sees

Provisional CLV is **shown, visibly badged, and never aggregated** into
trusted statistics. Hiding it entirely would make later repair a surprise
retroactive change to history; showing it unmarked would invite anchoring on
an untrusted number. The badge (and the repaired mark after §9) makes every
number's trust level legible at a glance.

The display must distinguish two separate questions:

1. **Is the closing price trustworthy?**
2. **Is a compatible benchmark available, making CLV computable?**

A row may have a trusted closing price but no benchmark, or a benchmark may
exist while the user's closing price remains provisional. "Unbenchmarkable"
does not demote closing-price capture, settlement, or recovery. Benchmark
compatibility requires the same market definition, period, side, and point;
the system must not silently substitute a merely similar market.

---

## One-off and ephemeral contexts

The full observe → verify → promote pipeline produces reusable knowledge. A
one-night promotion or single special event produces none — the context will
never recur. The model therefore allows an **event-scoped** designation at
discovery time: the user, or a conservative user-confirmed heuristic, can
classify a context as ephemeral.

Ephemeral means "do not build reusable context trust," not "do not automate."
The system still collects useful event evidence, verifies individual rows
automatically when authoritative facts exist, and uses manual evidence only
where necessary. It simply avoids promoting the unrepeatable event into a
reusable context capability. An ephemeral context that turns out to recur can
be reopened as ordinary.

---

## Expected behavior by scenario

| Scenario | Expected behavior |
|---|---|
| Known context with all capabilities needed by this bet fresh and verified | Normal capture and trusted CLV; settlement proceeds independently; each capture doubles as cheap re-validation. |
| New context in a familiar family, same source | Log the bet, show status, collect evidence, verify post-event; a strong family prior can permit promotion after the first clean event if policy allows; re-evaluate all affected provisional rows. |
| New context in a new family or on a new source | As above, but promotion may take several events; interim rows stay provisional and badged. |
| First bet placed at/after the real start | Observation starts mid-event; verification leans on post-hoc sources; the row may be unrepairable — case stays visible either way. |
| New context with no live start signal | Capture available prices, keep CLV excluded, seek an authoritative post-event start. |
| New context with no authoritative start source | Retain evidence, classify the start capability Blocked, keep rows excluded, and record the missing fact in the consolidated case/profile. |
| Identity ambiguous (could be a known context) | Treat as new — never as known — until identity is positively resolved. |
| Manual or exotic market | Explain at log time that automatic CLV is unavailable and identify the required manual information. |
| One-off special event / promotion | Offer event-scoped classification; automatically verify rows where possible without creating reusable context trust. |
| Upcoming event | Pending, never a historical recovery failure. |
| Start-source behavior later changes | Set only the affected start capability to Contradicted, re-evaluate rows in the causal window, and notify the user. |
| Verified capability idle for a long gap / new season | Health becomes Stale; next relevant bet triggers re-confirmation before dependent new rows are trusted. |
| Closing price trusted but benchmark missing | Preserve the trusted close, report CLV as unbenchmarkable, and do not demote unrelated capabilities. |

---

## Reporting principles

Reports distinguish operational states instead of combining every row without
trusted CLV into one failure count. At minimum, reporting separates:

- Pending future events.
- Capture currently in progress.
- New-context observation in progress (provisional rows, badged).
- Completed rows that are automatically recoverable.
- Completed rows repaired after verification (provenance-marked).
- Completed rows blocked by a missing actual-start source or other stable
  automatic limitation.
- Rows with a trusted closing price but no compatible CLV benchmark.
- Manual or book-specific rows (including user-attested ones).
- Rows excluded by user decision (Retired contexts).
- Live bets, props, voids, and other rows excluded by design.

This prevents upcoming bets from inflating the historical recovery backlog and
makes every count map to a meaningful next action.

---

## Safety principles

1. Never substitute scheduled start for verified actual start when doing so
   could admit an in-play price.
2. Unknown capability always means provisional or excluded, never trusted by
   default.
3. Ambiguous context identity resolves toward "new," never toward "known" —
   trust must not be inheritable by alias collision.
4. Trust attaches to rows via evidence; context state only sets defaults.
   Promotion never retroactively blesses unexamined rows.
5. A bet must still be logged even when onboarding is incomplete.
6. Failure in onboarding must not interfere with settlement or unrelated bets.
7. Ambiguous event matches must fail toward review.
8. Automatic repair must preserve the original row, and repaired values remain
   permanently distinguishable from natively captured ones.
9. Promotion must be reversible; demotion is automatic and notifies the user.
10. Retirement (permanent exclusion) is a user decision, never a system one.
11. Manual evidence is identified clearly and never represented as
    automatically verified.
12. A missing benchmark never turns an otherwise trustworthy closing price
    into an untrustworthy one; it makes CLV uncomputable.
13. Negative evidence affects only the capability grains to which it can be
    confidently attributed.
14. If the authoritative capability profile is unavailable, consumers fail
    closed; no consumer falls back to a private support table.

---

## Success criteria

Vision: the user never needs to remember to request support for a new context.
Measured by:

- Every new-context first bet reaches a classified outcome (Verified, Limited,
  Blocked, Manual, or Retired) within a defined interval after the event and
  its evidence windows complete, without the user having to prompt an
  investigation. Upcoming events remain Pending and do not violate this
  measure.
- Among trusted rows, zero closing prices postdate the best available
  authoritative actual start. Every trusted row retains enough evidence to
  audit that verdict retrospectively.
- Exactly one store of capability knowledge; no consumer maintains a private
  copy of "what works."
- Provenance and trust are separately visible in every surface that displays
  closing price or CLV.
- Upcoming bets never appear in historical-failure counts.
- Adding an ordinary context in a known family on a proven source requires no
  code deployment.
- Every open unresolved case is visible in one place, with its affected rows
  and its specific missing fact.
- The first-bet provisional rate, automatic repair rate, median time to
  classification, unresolved-case age, false promotion/demotion rate, evidence
  collection cost, and number of required user interventions are measured.
- No ordinary sportsbook market-coverage miss demotes an unrelated identity,
  start, result, or benchmark capability.

---

## Non-goals

- Guaranteeing automatic CLV for every sportsbook, special event, prop, or
  combat promotion.
- Guaranteeing a CLV benchmark exists for every context (no sharp line listed
  → honestly reported as unbenchmarkable, not approximated).
- Treating settlement support as a prerequisite for trustworthy closing-price
  capture or CLV. These capabilities are reported independently.
- Treating any available timestamp as authoritative.
- Automatically trusting a context solely because another context in the same
  sport works.
- Reconstructing historical facts that no available source retained.
- Preventing the user from logging a legitimate bet merely because onboarding
  is incomplete.
- Serving as a general data-quality framework for already-verified contexts
  beyond the cheap re-validation described in §8.

---

## Decisions to make before implementation

The conceptual model leaves policy choices open:

1. Evidence bar: how many events, and which irregular-event observations,
   promote source correctness for (a) a context with a strong family prior on
   the same source, (b) a new family or new source? Which severe contradictions
   cause immediate fail-closed health changes?
2. Which capability and market-family boundaries are used initially, and how
   are overly broad records split without retroactively blessing rows?
3. Where the single authoritative capability profile lives, given that the
   odds tool and the results checker are separately deployed systems — and
   which one owns writes and transition decisions.
4. Which limitations block logging versus produce provisional capture.
5. Where onboarding status, provisional badges, and unresolved cases are
   displayed.
6. Freshness policy: how long verification lasts unexercised; what a season
   boundary means per sport, source, and capability.
7. Negative-evidence policy: severity, attribution, ambiguity quarantine,
   causal re-evaluation window, accumulation, and decay.
8. What counts as acceptable user-attested evidence for a row (start time,
   result, price), which supporting artifacts are required, and how confidence
   is represented.
9. When the ephemeral (event-scoped) designation is offered or suggested.
10. Evidence retention: how long observation evidence is kept for
     re-evaluation and audit, and at what storage cost.
11. Benchmark policy: compatible market definitions, timestamp policy,
    devigging, preferred and fallback sources, and what happens when the
    primary sharp reference does not list the market.
12. Scanner-intent policy: deduplication, expiry, persistence threshold, and
    API/credit budget for surfaced opportunities without bets.
13. Canonical identity policy: hierarchy, aliases, tournament editions,
    provider-key reuse, and how ambiguous identities are resolved or merged
    without inheriting trust.
14. Evidence and policy versioning: which source, matcher, parser, and policy
    changes require re-confirmation or row re-evaluation.
15. Case lifecycle: parent/child grouping, reopening, aging, notification
    severity, and when a Blocked investigation may close while its limitation
    remains visible.

These decisions should be resolved before choosing implementation mechanics.
