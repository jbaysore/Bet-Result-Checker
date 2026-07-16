"""Consume user decisions queued by odds-tool (Phase 6).

Discovery Queue is append-only from odds-tool's side. The checker remains the
single writer of Capabilities: it applies Manual/Retired/reopen transitions and
marks each decision row applied or failed. Reprocessing is idempotent.
"""

from __future__ import annotations

import json

import onboarding_policy as policy
from capability_profile import CapabilityProfile, IllegalTransition
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
        raise ValueError("ephemeral contexts are Phase 7 and are not accepted yet")
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
    summary = {"applied": 0, "failed": 0}
    for row_idx, raw in enumerate(values[1:], start=2):
        row = dict(zip(headers, raw))
        if str(row.get("Kind") or "").strip().lower() != "decision":
            continue
        if str(row.get("Status") or "").strip().lower() != "pending":
            continue
        try:
            payload = json.loads(str(row.get("Payload") or "{}"))
            count = apply_decision(profile, str(row.get("Context ID") or "").strip(), payload)
            status = f"applied:{count}"
            summary["applied"] += 1
        except (ValueError, TypeError, json.JSONDecodeError, IllegalTransition) as exc:
            status = f"failed:{str(exc)[:120]}"
            summary["failed"] += 1
        call_with_sheets_retry(
            f"{DISCOVERY_TAB} status row {row_idx}", tab.update_cell,
            row_idx, status_col, status)
    return summary

