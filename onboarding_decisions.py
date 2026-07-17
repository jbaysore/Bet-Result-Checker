"""Consume user decisions queued by odds-tool (Phase 6).

Discovery Queue is append-only from odds-tool's side. The checker remains the
single writer of Context Registry and Capabilities: it applies user decisions
and durable scanner discoveries, then marks each row applied/expired/failed.
Reprocessing is idempotent.
"""

from __future__ import annotations

import json
import hashlib
from datetime import datetime, timezone

import onboarding_policy as policy
from capability_profile import CapabilityProfile, IllegalTransition, record_key
from scripts.create_onboarding_tabs import DISCOVERY_COLUMNS, DISCOVERY_TAB


def _targets(profile: CapabilityProfile, context_id: str, record_key_value: str,
             action: str):
    records = profile.records()
    if record_key_value:
        records = [r for r in records if r.record_key == record_key_value]
    else:
        records = [r for r in records if r.context_id == context_id]
    if action == "reopen":
        records = [r for r in records
                   if r.classification in (policy.MANUAL, policy.RETIRED, policy.BLOCKED,
                                           policy.DISCOVERED)]
    return records


def apply_decision(profile: CapabilityProfile, context_id: str, payload: dict) -> int:
    action = str(payload.get("action") or "").strip().lower()
    record_key_value = str(payload.get("recordKey") or "").strip()
    if action == "ephemeral":
        return apply_ephemeral(profile, context_id, payload)
    destination = {
        "manual": policy.MANUAL,
        "retire": policy.RETIRED,
        "reopen": policy.DISCOVERED,
    }.get(action)
    if destination is None:
        raise ValueError(f"unknown action {action!r}")

    records = _targets(profile, context_id, record_key_value, action)
    if not records:
        raise ValueError("no matching capability records")
    for record in records:
        if record.context_id != context_id:
            raise ValueError("recordKey does not belong to contextId")
        profile.transition(
            record.record_key, destination,
            str(payload.get("reason") or f"user decision: {action}"),
            authority=policy.AUTHORITY_USER,
        )
    return len(records)


def _scope_ephemeral_registry(context_id: str, ephemeral_id: str,
                              sport_key: str, event_id: str) -> None:
    from context_registry import REGISTRY_COLUMNS, REGISTRY_TAB
    from sheets_quota import call_with_sheets_retry
    from sheets_reader import _get_spreadsheet

    tab = call_with_sheets_retry(
        f"worksheet({REGISTRY_TAB})", _get_spreadsheet().worksheet, REGISTRY_TAB)
    values = call_with_sheets_retry(f"{REGISTRY_TAB} get_all_values", tab.get_all_values)
    headers = values[0] if values else REGISTRY_COLUMNS
    column = {name: headers.index(name) + 1 for name in headers}
    event_exists = False
    for row_idx, raw in enumerate(values[1:], start=2):
        row = dict(zip(headers, raw))
        alias_type = str(row.get("Alias Type") or "sport_key").strip()
        alias_value = str(row.get("Alias Value") or "").strip()
        row_context = str(row.get("Context ID") or "").strip()
        status = str(row.get("Status") or "active").strip().lower()
        if (alias_type == "event_id" and alias_value.casefold() == event_id.casefold()
                and row_context == ephemeral_id and status == "active"):
            event_exists = True
        if (alias_type == "sport_key" and alias_value.casefold() == sport_key.casefold()
                and row_context == context_id and status == "active"):
            call_with_sheets_retry(
                f"retire ephemeral sport alias row {row_idx}", tab.update_cell,
                row_idx, column["Status"], "retired")
    if not event_exists:
        row = {
            "Context ID": ephemeral_id, "Alias Type": "event_id", "Alias Value": event_id,
            "Edition Start": "", "Edition End": "", "Mapping Version": "1",
            "Status": "active", "Notes": f"ephemeral: user-designated from {context_id}",
        }
        call_with_sheets_retry("append ephemeral event alias", tab.append_row,
                               [row.get(item, "") for item in REGISTRY_COLUMNS])


def apply_ephemeral(profile: CapabilityProfile, context_id: str, payload: dict) -> int:
    """Scope a user-confirmed one-off to one provider event.

    Both the original candidate grains and the event copies are Retired under
    the user's authority. The event copies still collect evidence, but can never
    become a reusable Verified profile; row-level verification handles the bet.
    """
    event_id = str(payload.get("eventId") or "").strip()
    sport_key = str(payload.get("sportKey") or "").strip()
    if not event_id or not sport_key:
        raise ValueError("ephemeral action requires eventId and sportKey")
    originals = [record for record in profile.records() if record.context_id == context_id]
    if not originals:
        raise ValueError("no matching capability records")
    token = hashlib.sha256(event_id.encode("utf-8")).hexdigest()[:12]
    ephemeral_id = f"{context_id}/event-{token}"
    _scope_ephemeral_registry(context_id, ephemeral_id, sport_key, event_id)
    changed = 0
    for original in originals:
        if original.classification != policy.RETIRED:
            profile.transition(original.record_key, policy.RETIRED,
                               "user designated context as one-off",
                               authority=policy.AUTHORITY_USER)
            changed += 1
        key = record_key(ephemeral_id, original.capability, original.qualifier)
        event_record = profile.get_record(key)
        if event_record is None:
            event_record = profile.transition(
                key, policy.DISCOVERED, "event-scoped evidence only",
                context_id=ephemeral_id, capability=original.capability,
                qualifier=original.qualifier)
        event_record.constraints = {"ephemeral": True, "event_id": event_id,
                                    "source_context": context_id}
        event_record.activity = policy.AWAITING_POST_EVENT
        profile._store(event_record)
        if event_record.classification != policy.RETIRED:
            profile.transition(event_record.record_key, policy.RETIRED,
                               "event-scoped: never reusable trust",
                               authority=policy.AUTHORITY_USER)
            changed += 1
    return changed


def _parse_utc(value) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _ensure_registry_alias(context_id: str, sport_key: str) -> bool:
    """Persist the scanner-proposed sport-key identity, refusing collisions."""
    from context_registry import REGISTRY_COLUMNS, REGISTRY_TAB
    from sheets_quota import call_with_sheets_retry
    from sheets_reader import _get_spreadsheet

    tab = call_with_sheets_retry(
        f"worksheet({REGISTRY_TAB})", _get_spreadsheet().worksheet, REGISTRY_TAB)
    values = call_with_sheets_retry(f"{REGISTRY_TAB} get_all_values", tab.get_all_values)
    headers = values[0] if values else REGISTRY_COLUMNS
    objects = [dict(zip(headers, row)) for row in values[1:]]
    matches = [row for row in objects
               if str(row.get("Alias Type") or "sport_key").strip() == "sport_key"
               and str(row.get("Alias Value") or "").strip().casefold() == sport_key.casefold()
               and str(row.get("Status") or "active").strip().lower() == "active"]
    contexts = {str(row.get("Context ID") or "").strip() for row in matches}
    contexts.discard("")
    if contexts == {context_id}:
        return False
    if contexts:
        raise ValueError(f"sport key already maps to {', '.join(sorted(contexts))}")
    row = {
        "Context ID": context_id, "Alias Type": "sport_key", "Alias Value": sport_key,
        "Edition Start": "", "Edition End": "", "Mapping Version": "1",
        "Status": "active", "Notes": "discovered: scanner durable intent",
    }
    call_with_sheets_retry("Context Registry scanner append", tab.append_row,
                           [row.get(column, "") for column in REGISTRY_COLUMNS])
    return True


def apply_discovery(profile: CapabilityProfile, row: dict, payload: dict,
                    *, now: datetime | None = None) -> int:
    """Turn a durable scanner intent into collecting grains before bet time."""
    now = now or datetime.now(timezone.utc)
    context_id = str(row.get("Context ID") or "").strip()
    sport_key = str(row.get("Sport Key") or "").strip()
    book = str(row.get("Book") or "").strip().lower()
    family = str(row.get("Market Family") or "").strip().lower()
    expires = _parse_utc(payload.get("expiresAt"))
    if expires is not None and expires < now:
        raise TimeoutError("scanner discovery expired before consumption")
    if str(payload.get("intent") or "").lower() != "durable" \
            or int(payload.get("sightings") or 0) < 2:
        raise ValueError("scanner discovery lacks durable repeated intent")
    if not all((context_id, sport_key, book, family)) or family == policy.MF_UNKNOWN:
        raise ValueError("scanner discovery grain is incomplete")

    _ensure_registry_alias(context_id, sport_key)
    grains = [
        (policy.CAP_IDENTITY, "toa"),
        (policy.CAP_START_LIVE, "toa_scores"),
        (policy.CAP_CAPTURE, f"{book}|{family}"),
        (policy.CAP_BENCHMARK, f"{policy.BENCHMARK_SOURCE}|{family}"),
    ]
    changed = 0
    first_seen = _parse_utc(payload.get("firstSeenAt")) or now
    for capability, qualifier in grains:
        key = record_key(context_id, capability, qualifier)
        record = profile.get_record(key)
        if record is None:
            record = profile.transition(
                key, policy.DISCOVERED, "scanner durable intent (pre-bet)",
                context_id=context_id, capability=capability, qualifier=qualifier)
            changed += 1
        if record.classification == policy.DISCOVERED:
            record.activity = policy.COLLECTING
            record.first_seen = min(filter(None, (record.first_seen, first_seen)))
            record.evidence.setdefault("scanner_discoveries", 1)
            profile._store(record)
    try:
        from onboarding_gate import log_shadow
        log_shadow("scanner_durable_intent", {
            "context_id": context_id, "sport": sport_key, "book": book,
            "market_family": family, "event_id": payload.get("eventId"),
            "first_seen_at": payload.get("firstSeenAt"),
            "quote_observed_at": payload.get("softQuoteObservedAt"),
        })
    except Exception:
        pass
    return changed


def consume_pending_decisions(profile: CapabilityProfile | None = None) -> dict:
    from sheets_quota import call_with_sheets_retry
    from sheets_reader import _get_spreadsheet

    profile = profile or CapabilityProfile.load()
    if not profile.readable:
        raise RuntimeError("Capabilities tab unreadable (decision consumer fail closed)")
    tab = call_with_sheets_retry(
        f"worksheet({DISCOVERY_TAB})", _get_spreadsheet().worksheet, DISCOVERY_TAB)
    values = call_with_sheets_retry(
        f"{DISCOVERY_TAB} get_all_values", tab.get_all_values)
    if not values:
        return {"applied": 0, "failed": 0}
    headers = values[0]
    missing = [h for h in DISCOVERY_COLUMNS if h not in headers]
    if missing:
        raise RuntimeError(f"{DISCOVERY_TAB} missing columns: {', '.join(missing)}")
    col = {name: headers.index(name) for name in headers}
    status_col = col["Status"] + 1
    summary = {"applied": 0, "expired": 0, "failed": 0}
    expired_rows = []
    for row_idx, raw in enumerate(values[1:], start=2):
        row = dict(zip(headers, raw))
        kind = str(row.get("Kind") or "").strip().lower()
        if kind not in {"decision", "discovery"}:
            continue
        if str(row.get("Status") or "").strip().lower() != "pending":
            continue
        try:
            payload = json.loads(str(row.get("Payload") or "{}"))
            count = (apply_decision(profile, str(row.get("Context ID") or "").strip(), payload)
                     if kind == "decision" else apply_discovery(profile, row, payload))
            status = f"applied:{count}"
            summary["applied"] += 1
        except TimeoutError as exc:
            status = f"expired:{str(exc)[:120]}"
            summary["expired"] += 1
            expired_rows.append(row_idx)
        except (ValueError, TypeError, json.JSONDecodeError, IllegalTransition) as exc:
            status = f"failed:{str(exc)[:120]}"
            summary["failed"] += 1
        call_with_sheets_retry(
            f"{DISCOVERY_TAB} status row {row_idx}", tab.update_cell,
            row_idx, status_col, status)
    # Surfaced-only discoveries have no durable value after their intent
    # window. Delete them bottom-up after status writes so row indices from the
    # snapshot remain valid; decision/applied rows retain their audit trail.
    for row_idx in sorted(expired_rows, reverse=True):
        call_with_sheets_retry(
            f"{DISCOVERY_TAB} drop expired row {row_idx}", tab.delete_rows, row_idx)
    return summary
