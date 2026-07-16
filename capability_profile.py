"""Typed read/write of the `Capabilities` tab — the authoritative capability
profile store (plan §0.1, §0.3, Phase 1). The checker is the single writer;
everyone reads.

Three operations matter:
  - `require(...)` / `require_clv(...)` → a TrustDecision. Fail-closed: a missing
    or unreadable tab yields "unverified" for every capability, never trusted
    (concept safety #14). This is what caps a row at PROVISIONAL in Phase 2.
  - `transition(...)` → move a record's classification, with the concept §2
    transition table enforced in code (illegal edges raise IllegalTransition).
  - `set_health(...)` → the orthogonal Fresh/Stale/Contradicted axis (concept §8).

The sheet I/O is injectable so the logic is unit-testable without a live sheet:
construct `CapabilityProfile(records=..., sink=...)`. `CapabilityProfile.load()`
wires the live tab (reads fail closed to an unreadable profile).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

import onboarding_policy as policy

CAPABILITIES_TAB = "Capabilities"

# §0.3 column order — the header row the seed script writes and this module reads.
CAPABILITY_COLUMNS = [
    "Record Key", "Context ID", "Capability", "Qualifier", "Classification",
    "Health", "Activity", "Policy Version", "Evidence Summary", "First Seen",
    "Last Verified", "Last Checked", "Constraints", "Notes",
]

# Qualifier conventions, kept in ONE place so the checker and the odds-tool
# parity fixture agree on exactly which record a bet depends on (plan §0.5).
IDENTITY_QUALIFIER = "toa"
START_LIVE_QUALIFIER = "toa_scores"
CAPTURE_GRANDFATHERED_BOOK = "any"   # book-agnostic seed from §0.4 grandfathering

# The CLV "start" requirement is satisfied by EITHER a trusted live-flip
# (start_live) OR a trusted authoritative actual-start (start_authoritative):
# a row is safely pre-start if we trust the live flip pre-margin OR we recovered
# a safe close against the authoritative start (concept §5). Labelled as one
# requirement in the trust matrix.
CLV_START = "start"


class IllegalTransition(Exception):
    """Raised when a classification edge is not permitted by concept §2."""


def _parse_utc(value) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt


def _parse_json(value) -> dict:
    raw = str(value or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except (ValueError, TypeError):
        return {}


def _parse_int(value, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (ValueError, TypeError):
        return default


def record_key(context_id: str, capability: str, qualifier: str) -> str:
    return f"{context_id}|{capability}|{qualifier}"


@dataclass
class CapabilityRecord:
    record_key: str
    context_id: str
    capability: str
    qualifier: str
    classification: str = policy.DISCOVERED
    health: str = policy.NOT_EVALUATED
    activity: str = policy.IDLE
    policy_version: int = policy.POLICY_VERSION
    evidence: dict = field(default_factory=dict)
    first_seen: datetime | None = None
    last_verified: datetime | None = None
    last_checked: datetime | None = None
    constraints: dict = field(default_factory=dict)
    notes: str = ""

    # ── serialization ────────────────────────────────────────────────────────
    @classmethod
    def from_row(cls, row: dict) -> "CapabilityRecord":
        return cls(
            record_key=str(row.get("Record Key", "")).strip(),
            context_id=str(row.get("Context ID", "")).strip(),
            capability=str(row.get("Capability", "")).strip(),
            qualifier=str(row.get("Qualifier", "")).strip(),
            classification=str(row.get("Classification", policy.DISCOVERED)).strip() or policy.DISCOVERED,
            health=str(row.get("Health", policy.NOT_EVALUATED)).strip() or policy.NOT_EVALUATED,
            activity=str(row.get("Activity", policy.IDLE)).strip() or policy.IDLE,
            policy_version=_parse_int(row.get("Policy Version"), policy.POLICY_VERSION),
            evidence=_parse_json(row.get("Evidence Summary")),
            first_seen=_parse_utc(row.get("First Seen")),
            last_verified=_parse_utc(row.get("Last Verified")),
            last_checked=_parse_utc(row.get("Last Checked")),
            constraints=_parse_json(row.get("Constraints")),
            notes=str(row.get("Notes", "")).strip(),
        )

    def to_row(self) -> dict:
        def iso(dt: datetime | None) -> str:
            if dt is None:
                return ""
            if dt.tzinfo is None:  # treat a naive timestamp as UTC, never local
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        return {
            "Record Key": self.record_key,
            "Context ID": self.context_id,
            "Capability": self.capability,
            "Qualifier": self.qualifier,
            "Classification": self.classification,
            "Health": self.health,
            "Activity": self.activity,
            "Policy Version": self.policy_version,
            "Evidence Summary": json.dumps(self.evidence, separators=(",", ":")) if self.evidence else "",
            "First Seen": iso(self.first_seen),
            "Last Verified": iso(self.last_verified),
            "Last Checked": iso(self.last_checked),
            "Constraints": json.dumps(self.constraints, separators=(",", ":")) if self.constraints else "",
            "Notes": self.notes,
        }

    def authorizes_trust(self, *, within_bounds: bool = True) -> bool:
        # A Limited record only authorizes trust for a row inside its bounds; the
        # caller decides bounds membership and passes within_bounds.
        limited_ok = within_bounds if self.classification == policy.LIMITED else False
        if self.classification == policy.LIMITED:
            return policy.authorizes_trust(self.classification, self.health,
                                           within_limited_bounds=limited_ok)
        return policy.authorizes_trust(self.classification, self.health)


@dataclass(frozen=True)
class Requirement:
    """One capability a bet's CLV depends on. Satisfied when EITHER an exact
    `candidate_keys` record OR a record in any `any_of` (context_id, capability)
    bucket authorizes trust. `candidate_keys` is preference-ordered (a specific
    book key first, a grandfathered fallback second); `any_of` matches any
    qualifier under a capability (used for identity and the start disjunction)."""
    capability: str
    candidate_keys: tuple[str, ...] = ()
    any_of: tuple[tuple[str, str], ...] = ()
    within_bounds: bool = True


@dataclass(frozen=True)
class TrustDecision:
    trusted: bool
    matrix: dict            # capability → {classification, health, authorized, record_key}
    unresolved: tuple[str, ...]
    reason: str = ""

    @property
    def provisional(self) -> bool:
        return not self.trusted


class CapabilityProfile:
    def __init__(self, records: list[CapabilityRecord] | list[dict] | None,
                 sink: "CapabilitySink | None" = None):
        # records is None → tab unreadable → fail closed.
        self._readable = records is not None
        self._records: dict[str, CapabilityRecord] = {}
        self._by_ctx_cap: dict[tuple[str, str], list[CapabilityRecord]] = {}
        for item in records or []:
            rec = item if isinstance(item, CapabilityRecord) else CapabilityRecord.from_row(item)
            if rec.record_key:
                self._index(rec)
        self._sink = sink

    def _index(self, rec: CapabilityRecord) -> None:
        self._records[rec.record_key] = rec
        bucket = self._by_ctx_cap.setdefault((rec.context_id, rec.capability), [])
        if rec not in bucket:
            bucket.append(rec)

    @property
    def readable(self) -> bool:
        return self._readable

    def get_record(self, key: str) -> CapabilityRecord | None:
        return self._records.get(key)

    def records(self) -> list[CapabilityRecord]:
        """All records (verifier + family-prior scans)."""
        return list(self._records.values())

    def records_for(self, context_id: str, capability: str) -> list[CapabilityRecord]:
        return list(self._by_ctx_cap.get((context_id, capability), []))

    def annotate(self, key: str, note: str) -> CapabilityRecord | None:
        """Append a Notes line to a record without changing its classification —
        used for the Phase 4 `proposed:` shadow markers. No-op if absent."""
        rec = self._records.get(key)
        if rec is None:
            return None
        rec.notes = _append_note(rec.notes, note)
        self._store(rec)
        return rec

    # ── trust evaluation ─────────────────────────────────────────────────────
    def require(self, requirements: list[Requirement]) -> TrustDecision:
        """Trusted only if EVERY requirement is met by at least one candidate
        record that authorizes trust — and the tab was readable at all."""
        if not self._readable:
            matrix = {r.capability: {"classification": None, "health": None,
                                     "authorized": False, "record_key": None}
                      for r in requirements}
            return TrustDecision(False, matrix, tuple(r.capability for r in requirements),
                                 reason="capability profile unreadable (fail closed)")

        matrix: dict[str, dict] = {}
        unresolved: list[str] = []
        for req in requirements:
            chosen = self._first_authorizing(req)
            if chosen is not None:
                matrix[req.capability] = {
                    "classification": chosen.classification, "health": chosen.health,
                    "authorized": True, "record_key": chosen.record_key,
                }
            else:
                # Report the best available candidate's state for the matrix, even
                # though it did not authorize trust.
                fallback = self._best_candidate(req)
                matrix[req.capability] = {
                    "classification": fallback.classification if fallback else None,
                    "health": fallback.health if fallback else None,
                    "authorized": False,
                    "record_key": fallback.record_key if fallback else None,
                }
                unresolved.append(req.capability)

        trusted = not unresolved
        reason = "" if trusted else "unverified: " + ", ".join(unresolved)
        return TrustDecision(trusted, matrix, tuple(unresolved), reason=reason)

    def _first_authorizing(self, req: Requirement) -> CapabilityRecord | None:
        """The first record that authorizes trust: exact candidate keys in order,
        then any record under an `any_of` (context, capability) bucket."""
        for key in req.candidate_keys:
            rec = self._records.get(key)
            if rec is not None and rec.authorizes_trust(within_bounds=req.within_bounds):
                return rec
        for ctx_cap in req.any_of:
            for rec in self._by_ctx_cap.get(ctx_cap, []):
                if rec.authorizes_trust(within_bounds=req.within_bounds):
                    return rec
        return None

    def _best_candidate(self, req: Requirement) -> CapabilityRecord | None:
        """Any existing record for this requirement (for the matrix display when
        none authorized trust)."""
        for key in req.candidate_keys:
            rec = self._records.get(key)
            if rec is not None:
                return rec
        for ctx_cap in req.any_of:
            bucket = self._by_ctx_cap.get(ctx_cap, [])
            if bucket:
                return bucket[0]
        return None

    def require_clv(self, context_id: str, book: str, market_family: str) -> TrustDecision:
        return self.require(clv_requirements(context_id, book, market_family))

    # ── mutation (checker is the single writer) ─────────────────────────────
    def transition(self, key: str, to_classification: str, reason: str,
                   policy_version: int = policy.POLICY_VERSION,
                   *, authority: str = policy.AUTHORITY_AUTO,
                   context_id: str = "", capability: str = "", qualifier: str = "") -> CapabilityRecord:
        """Move a record's classification, enforcing the concept §2 table. A
        missing record is the virtual `Unseen` state: only Unseen→Discovered
        (record creation) is legal from there."""
        rec = self._records.get(key)
        if rec is None:
            if to_classification != policy.DISCOVERED:
                raise IllegalTransition(
                    f"no record {key!r}; only Unseen→Discovered may create one "
                    f"(requested →{to_classification})")
            rec = CapabilityRecord(
                record_key=key, context_id=context_id, capability=capability,
                qualifier=qualifier, classification=policy.DISCOVERED,
                health=policy.NOT_EVALUATED, activity=policy.IDLE,
                policy_version=policy_version, first_seen=policy.now_utc())
            rec.notes = _append_note(rec.notes, reason)
            self._store(rec)
            return rec

        if not policy.is_legal_classification_transition(rec.classification, to_classification, authority):
            raise IllegalTransition(
                f"illegal {rec.classification}→{to_classification} "
                f"(authority={authority}) for {key!r}")

        rec.classification = to_classification
        rec.policy_version = policy_version
        # Promotion to a trust-bearing classification sets Fresh + Last Verified;
        # a move back to Discovered resets health to NotEvaluated (re-earn).
        if to_classification in (policy.VERIFIED, policy.LIMITED):
            rec.health = policy.FRESH
            rec.last_verified = policy.now_utc()
        elif to_classification == policy.DISCOVERED:
            rec.health = policy.NOT_EVALUATED
        rec.notes = _append_note(rec.notes, reason)
        self._store(rec)
        return rec

    def set_health(self, key: str, health: str, reason: str) -> CapabilityRecord:
        """The orthogonal health axis (concept §8). Does not change
        classification: a Verified record with Stale health keeps its history,
        but new dependent rows fail closed until re-confirmation."""
        rec = self._records.get(key)
        if rec is None:
            raise IllegalTransition(f"no record {key!r} to set health on")
        if health not in policy.HEALTHS:
            raise ValueError(f"unknown health {health!r}")
        rec.health = health
        if health == policy.FRESH:
            rec.last_verified = policy.now_utc()
        rec.notes = _append_note(rec.notes, reason)
        self._store(rec)
        return rec

    def _store(self, rec: CapabilityRecord) -> None:
        self._index(rec)
        if self._sink is not None:
            self._sink.upsert(rec)

    @classmethod
    def load(cls, sink: "CapabilitySink | None" = None) -> "CapabilityProfile":
        rows = _load_capability_rows()
        if sink is None and rows is not None:
            sink = _LiveSink()
        return cls(rows, sink=sink)


def _append_note(existing: str, reason: str) -> str:
    reason = (reason or "").strip()
    if not reason:
        return existing
    stamp = policy.now_utc().date().isoformat()
    line = f"{stamp} {reason}"
    return f"{existing}\n{line}".strip() if existing else line


# ── CLV requirement builder (plan §0.5) ──────────────────────────────────────
def clv_requirements(context_id: str, book: str, market_family: str) -> list[Requirement]:
    """The records a bet's CLV trust depends on (concept §1 grain):

      - identity   — any resolved identity record for the context;
      - start      — a trusted live-flip (start_live) OR authoritative actual-start
                     (start_authoritative) for the context;
      - capture    — the specific `book|family` record OR the grandfathered
                     `any|family` seed, so seeded contexts stay trusted (parity).
    """
    book_key = str(book or "").strip().lower()
    specific_capture = record_key(context_id, policy.CAP_CAPTURE, f"{book_key}|{market_family}")
    grandfathered_capture = record_key(
        context_id, policy.CAP_CAPTURE, f"{CAPTURE_GRANDFATHERED_BOOK}|{market_family}")
    return [
        Requirement(policy.CAP_IDENTITY, any_of=((context_id, policy.CAP_IDENTITY),)),
        Requirement(CLV_START, any_of=((context_id, policy.CAP_START_LIVE),
                                       (context_id, policy.CAP_START_AUTHORITATIVE))),
        Requirement(policy.CAP_CAPTURE, candidate_keys=(specific_capture, grandfathered_capture)),
    ]


# ── Live sheet I/O (fail-closed reads, checker-only writes) ──────────────────
def _load_capability_rows() -> list[dict] | None:
    """Read the Capabilities tab as header-keyed dicts, or None on ANY failure
    (missing tab, quota, parse) — the fail-closed sentinel."""
    try:
        import gspread  # noqa: F401 — presence check; keeps this a soft dependency
        from sheets_reader import _get_spreadsheet
        from sheets_quota import call_with_sheets_retry

        tab = call_with_sheets_retry(
            f"worksheet({CAPABILITIES_TAB})", _get_spreadsheet().worksheet, CAPABILITIES_TAB)
        values = call_with_sheets_retry(
            f"{CAPABILITIES_TAB} get_all_values", tab.get_all_values)
    except Exception:
        return None
    if not values:
        return []
    headers = values[0]
    return [dict(zip(headers, row)) for row in values[1:]]


class CapabilitySink:
    """Write interface for capability mutations (checker is the single writer)."""

    def upsert(self, record: CapabilityRecord) -> None:  # pragma: no cover - interface
        raise NotImplementedError


class _LiveSink(CapabilitySink):
    """Upsert a record into the live Capabilities tab by Record Key. Best-effort:
    a write failure is logged, never raised onto the settlement/capture path."""

    def upsert(self, record: CapabilityRecord) -> None:
        try:
            from sheets_reader import _get_spreadsheet
            from sheets_quota import call_with_sheets_retry

            tab = call_with_sheets_retry(
                f"worksheet({CAPABILITIES_TAB})", _get_spreadsheet().worksheet, CAPABILITIES_TAB)
            values = call_with_sheets_retry(
                f"{CAPABILITIES_TAB} get_all_values", tab.get_all_values)
            headers = values[0] if values else CAPABILITY_COLUMNS
            key_col = headers.index("Record Key")
            row_values = [record.to_row().get(h, "") for h in headers]
            target_row = None
            for i, row in enumerate(values[1:], start=2):
                if len(row) > key_col and row[key_col] == record.record_key:
                    target_row = i
                    break
            if target_row is None:
                call_with_sheets_retry("append_row", tab.append_row, row_values)
            else:
                end_col = chr(ord("A") + len(headers) - 1)
                call_with_sheets_retry(
                    "update", tab.update, f"A{target_row}:{end_col}{target_row}", [row_values])
        except Exception as exc:  # pragma: no cover - live-only path
            print(f"[capability_profile] ⚠ upsert failed for {record.record_key}: {exc}")
