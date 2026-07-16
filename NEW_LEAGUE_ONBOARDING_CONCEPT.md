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
**sportsbook**, settlement varies by **market class** (moneyline, spread,
total, prop, …). "This league works" is therefore not a well-formed claim; the
well-formed claim is "this capability is verified at this grain."

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

Four ideas underpin everything below. They are stated once here so the rest of
the document can rely on them.

**1. Capabilities are verified at their natural grain.**
Identity, discovery, and start detection are roughly per-context. Price
capture and recovery are per-(context × sportsbook). Settlement is
per-(context × market class). The state machine in this document belongs to
each capability at its own grain. A single "context state" exists only as a
derived, display-level rollup — it is never the source of truth, and nothing
may consult the rollup to make a trust decision.

**2. Rows are the unit of trust.**
A bet row's CLV is trusted because of evidence attached to *that row*, not
because of the state of its context. A row captured while a context was under
observation stays provisional until the row itself is re-evaluated —
promotion never retroactively blesses rows. Conversely, user-attested manual
evidence can make an individual row trustworthy without making its context
verified. Context state only sets defaults and gates automation.

**3. Two kinds of evidence, two kinds of trust.**
- *System correctness*: did our matching, parsing, and capture work for this
  context? One clean, exactly-matched event genuinely verifies this.
- *Source correctness*: does this source report true starts and results,
  **including when events go sideways** (delays, postponements, card
  reshuffles)? This is a distributional claim. One on-time game proves almost
  nothing about it, because on-time games were never the risk.

Promotion policy, family inheritance, and evidence quantity requirements all
follow from keeping these separate.

**4. Fail closed, loudly.**
Unknown means provisional or excluded, never trusted by default — and every
exclusion must be visible and carry a path to resolution (automatic, manual,
or a deliberate user decision to stop).

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
7. Re-evaluate **all** affected provisional rows when promotion or recovery
   makes a trustworthy close establishable, marking repaired rows permanently
   as repaired.
8. Keep unresolved rows excluded and present one clear, consolidated action
   item per unresolved case.

The user should not have to remember that the context was new. An unresolved
onboarding case should remain visible until the system verifies it, the user
classifies it as manual, or the user accepts it as permanently excluded.

---

## Conceptual solution

### 1. Treat support as a capability profile

Each context has one authoritative capability profile: the set of capability
records at their natural grains (see Core principle 1), each carrying its own
state and evidence summary.

The profile conceptually answers:

- Is the context's identity resolved unambiguously across all sources?
- Can events and markets be discovered?
- Can the selected sportsbook be priced? (per book)
- Can the live start transition be trusted?
- Is an authoritative actual-start source available after the event?
- Can results be settled automatically? (per market class)
- Can missed closing prices be recovered historically? (per book)
- Does a benchmark line exist to measure CLV against?
- Are there known exceptions for this context or event type?

Every consumer — logging, live capture, settlement, recovery, reporting —
relies on the same profile, so all parts of the system agree. Because the
consumers span more than one deployed system, where this single profile lives
is an explicit pre-implementation decision, not a detail (see *Decisions*).

### 2. Give every capability an onboarding state

States apply per capability record. The context-level rollup shown to the user
is derived (e.g., "Verified" only if all capabilities relevant to the user's
books and markets are verified; otherwise the rollup names what's missing).

| State | Meaning |
|---|---|
| Discovered | A bet or scanner-surfaced opportunity introduced this capability need. No evaluation yet. (There is no stored "Unseen" state — absence of a record *is* unseen.) |
| Observing | Evidence collection is actively in progress for one or more live cases. |
| Limited | Evaluation concluded with partial results; nothing is currently being collected. Distinguished from Observing by whether collection is active. |
| Verified | Sufficient evidence of both system and source correctness. Carries a freshness timestamp — verification ages (see §8). |
| Manual | The user has classified this capability as requiring user-provided or book-specific information. A user decision, recorded with its reason. |
| Retired | The user has accepted permanent exclusion — "we agreed to stop trying." Always user-ratified, never self-assigned by the system, so cases cannot quietly disappear. |

#### Transition governance

Naming states is not enough; each transition needs an authority.

| Transition | Authority |
|---|---|
| Discovered → Observing | Automatic (a real bet or surfaced opportunity exists) |
| Observing → Verified | Automatic, on meeting the evidence bar (§7) |
| Observing → Limited | Automatic, when collection ends without sufficient evidence |
| Limited → Observing | Automatic, on the next relevant bet/opportunity |
| Verified → Observing (demotion) | Automatic — fail closed on contradiction or staleness (§8) |
| Any → Manual | User decision (system may recommend) |
| Any → Retired | User decision only |
| Manual/Retired → Observing | User decision (e.g., a new source appears) |

### 3. Detect early: at surfacing, not only at logging

The earliest useful signal is not the bet — it is the scanner surfacing an
opportunity in an unfamiliar context. That set is small and high-intent, so
beginning observation at surfacing time does not violate the "don't monitor
everything" principle, and it buys back the lead time the cold-start race
otherwise destroys.

At bet-log time, if any relevant capability is not verified:

- The bet is still recorded.
- The user sees the context's current onboarding **status** — which
  capabilities are verified, which are pending, and the consequence. Status is
  shown every time but is not an alert; only state *changes* notify.
- The affected capabilities enter or continue observation automatically.
- The row is captured provisionally.

The explanation describes the consequence, not merely "unsupported":

> New context. The bet and available prices will be recorded, but its CLV
> stays provisional until real-start timing is verified. Verification is
> automatic; you'll be notified either way.

### 4. Observe only contexts connected to real intent

Evidence collection runs when there is an actual bet — or a scanner-surfaced
opportunity — to protect. The system does not continuously monitor every
possible context.

For an observing capability, the system conceptually retains:

- Event identity and matchup agreement across sources.
- Pregame observations, when the window existed.
- The first observed live or completed state.
- The last eligible sportsbook price before that transition.
- Quote freshness and source timestamps.
- Source errors, empty responses, and ambiguous event matches.

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
Unresolved and contradictory cases remain excluded, and contradiction also
counts as negative evidence against the source (§7, §8).

### 6. Use family inheritance conservatively — and know what it transfers

New contexts often belong to a familiar family. Framed by the two evidence
types, inheritance becomes precise:

- **Source correctness transfers.** If a results provider's soccer scoreboards
  have proven reliable across five competitions — including delayed kickoffs —
  a sixth competition on the same provider inherits most of that
  distributional trust.
- **System correctness does not transfer.** Whether *our* identity mapping,
  event matching, and market parsing work for the new competition must be
  re-earned with at least one clean, exactly-matched event. This is cheap —
  one event — but never skippable.

Consequences:

- A standard soccer competition inherits general soccer source-trust while
  still requiring its own identity and matching confirmation.
- A new combat promotion inherits nothing from UFC per-bout timing: different
  promotion, different source behavior — the source-correctness prior does not
  apply.
- A special event inherits neither its parent sport's schedule behavior nor
  its market behavior.

Inheritance reduces the *quantity* of evidence required for an ordinary new
context in a proven family. It never reduces the requirement to verify system
correctness for the specific context.

### 7. Promote on evidence — quality and quantity

An endpoint returning HTTP 200 proves nothing about whether it reports the
true start at the correct time. Worse, one cleanly observed on-time game
satisfies naive criteria while proving nothing about the failure mode CLV
protection exists for.

Promotion policy therefore considers, per capability:

- **System correctness:** at least one event matched exactly and
  unambiguously, with the full capture path exercised.
- **Source correctness:** either inherited from a proven family on the *same
  source* (§6), or accumulated across enough events — with extra weight given
  to any observed irregular event (delay, reschedule) handled correctly.
- Whether an independent authoritative start agreed with live detection.
- Whether the correct sportsbook quote was present and fresh (per book).
- Whether the event type has unusual timing, such as cards or staged contests
  (stricter bar).

Where family inheritance covers source correctness, promotion can follow the
first clean event. For a genuinely new family or source, verification may
intentionally take several events — and the interim state is Limited or
Observing, honestly displayed, not a premature Verified.

### 8. Keep verifying: demotion, freshness, and regression

Verification is not permanent, and "move backward if a source changes" needs a
detection mechanism, because dedicated observation stops after promotion:

- **Continuous cheap re-validation:** every routine capture on a verified
  context doubles as a lightweight check. Disagreements (start mismatch, event
  match failure, missing market) accumulate against the capability and trigger
  automatic demotion to Observing — fail closed — with affected recent rows
  re-flagged as provisional.
- **Freshness:** Verified carries a timestamp. A context idle past a defined
  age (or across a season boundary) degrades to "verified-stale": trusted
  history is kept, but the next bet triggers re-confirmation before new rows
  are admitted as trusted.
- Demotion generates a notification; silent downgrades are as bad as silent
  upgrades.

### 9. Repair rows with provenance — all of them

When promotion or post-event recovery establishes a trustworthy close:

- **Every** provisional row of that context/capability is re-evaluated — not
  just the first or the triggering one. A context can accumulate several bets
  across multiple days before verification completes.
- Repair recomputes from preserved evidence; it never silently mutates. The
  original captured values remain recoverable, and failure of a repair leaves
  the row exactly as it was.
- Repaired CLV is **permanently marked as repaired** — distinguishable from
  natively captured CLV forever, because it was assembled under weaker
  guarantees. Reports may aggregate it into trusted statistics, but the
  provenance mark never disappears.
- Rows that cannot be repaired stay provisional/excluded and roll up into the
  unresolved case (§10).

### 10. Preserve one visible, consolidated unresolved action

If automatic onboarding cannot finish, the user receives **one case per
unresolved context-capability**, carrying the list of affected rows — ten bets
on one unonboarded league are one case, not ten notifications.

Each case identifies the specific missing fact:

- No actual-start source is available.
- The scores source did not expose a live transition.
- The event could not be matched unambiguously.
- The sportsbook did not retain the required market.
- No benchmark line exists for CLV comparison.
- A manual or book-specific source is required.

And each case terminates only in one of three user-visible resolutions:

1. **Verified** — automation eventually succeeded.
2. **Manual** — the user supplies or commits to supplying the evidence.
   User-attested facts (e.g., "the fight actually started at 22:41") make the
   *rows* trustworthy, marked as manually evidenced; they never make the
   *context* verified.
3. **Retired** — the user explicitly accepts permanent exclusion.

The default, absent any decision, is continued exclusion with the case held
open. Cases never auto-close unresolved.

---

## Provisional CLV: what the user sees

Provisional CLV is **shown, visibly badged, and never aggregated** into
trusted statistics. Hiding it entirely would make later repair a surprise
retroactive change to history; showing it unmarked would invite anchoring on
an untrusted number. The badge (and the repaired mark after §9) makes every
number's trust level legible at a glance.

---

## One-off and ephemeral contexts

The full observe → verify → promote pipeline produces reusable knowledge. A
one-night promotion or single special event produces none — the context will
never recur. The model therefore allows an **event-scoped** designation at
discovery time: the user (or a conservative heuristic, user-confirmed) can
classify a context as ephemeral, which routes it directly to the Manual /
Retired decision with row-level evidence handling, instead of spending the
observation machinery on unrepeatable verification. An ephemeral context that
turns out to recur can be re-opened as ordinary.

---

## Expected behavior by scenario

| Scenario | Expected behavior |
|---|---|
| Known and verified context | Normal capture, settlement, recovery, trusted CLV — each capture doubling as cheap re-validation. |
| New context in a familiar family, same source | Log the bet, show status, observe, verify post-event; promote after the first clean event (source trust inherited); repair all provisional rows. |
| New context in a new family or on a new source | As above, but promotion may take several events; interim rows stay provisional and badged. |
| First bet placed at/after the real start | Observation starts mid-event; verification leans on post-hoc sources; the row may be unrepairable — case stays visible either way. |
| New context with no live start signal | Capture available prices, keep CLV excluded, seek an authoritative post-event start. |
| New context with no authoritative start source | Retain evidence, classify Limited, present one consolidated unresolved case. |
| Identity ambiguous (could be a known context) | Treat as new — never as known — until identity is positively resolved. |
| Manual or exotic market | Explain at log time that automatic CLV is unavailable and identify the required manual information. |
| One-off special event / promotion | Offer event-scoped classification; row-level manual evidence instead of context onboarding. |
| Upcoming event | Pending, never a historical recovery failure. |
| Source behavior later changes | Automatic demotion to Observing, recent rows re-flagged, user notified. |
| Verified context idle for a long gap / new season | Verified-stale; next bet triggers re-confirmation before new trusted rows. |

---

## Reporting principles

Reports distinguish operational states instead of combining every row without
trusted CLV into one failure count. At minimum, reporting separates:

- Pending future events.
- Capture currently in progress.
- New-context observation in progress (provisional rows, badged).
- Completed rows that are automatically recoverable.
- Completed rows repaired after verification (provenance-marked).
- Completed rows blocked by a missing actual-start source.
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

---

## Success criteria

Vision: the user never needs to remember to request support for a new context.
Measured by:

- Every new-context first bet reaches a terminal classification (Verified /
  Manual / Retired) within a defined number of days without the user having to
  prompt an investigation.
- Zero rows in trusted CLV whose closing price postdates the authoritative
  actual start — auditable retrospectively at any time.
- Exactly one store of capability knowledge; no consumer maintains a private
  copy of "what works."
- All provisional and repaired rows are visually distinguishable in every
  surface that displays CLV.
- Upcoming bets never appear in historical-failure counts.
- Adding an ordinary context in a known family on a proven source requires no
  code deployment.
- Every open unresolved case is visible in one place, with its affected rows
  and its specific missing fact.

---

## Non-goals

- Guaranteeing automatic CLV for every sportsbook, special event, prop, or
  combat promotion.
- Guaranteeing a CLV benchmark exists for every context (no sharp line listed
  → honestly reported as unbenchmarkable, not approximated).
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
   promote source correctness for (a) a context in a proven family on the same
   source, (b) a new family or new source?
2. Which capability grains matter enough to track separately at first
   (context × book for capture? context × market class for settlement?), and
   which can start coarse?
3. Where the single authoritative capability profile lives, given that the
   odds tool and the results checker are separately deployed systems — and
   which one owns writes.
4. Which limitations block logging versus produce provisional capture.
5. Where onboarding status, provisional badges, and unresolved cases are
   displayed.
6. Freshness policy: how long Verified lasts unexercised; what a season
   boundary means per sport.
7. Demotion thresholds: how many routine-capture disagreements trigger
   automatic demotion.
8. What counts as acceptable user-attested evidence for a row (start time,
   result, price), and how it is recorded.
9. When the ephemeral (event-scoped) designation is offered or suggested.
10. Evidence retention: how long observation evidence is kept for
    re-evaluation and audit, and at what storage cost.
11. Benchmark policy: what CLV is measured against in contexts the primary
    sharp reference does not list, if anything.

These decisions should be resolved before choosing implementation mechanics.
