#!/usr/bin/env python3
"""Rebuild onboarding evidence from authoritative, distinct sporting events.

Dry-run is the default. `--apply` writes a timestamped local backup and then
batch-replaces the derived Evidence Summary/health fields in Capabilities.
Classifications and user decisions are preserved; ordinary automatic promotion
and benchmark reopening are re-evaluated from the rebuilt evidence.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import capability_profile
import onboarding_policy as policy
import onboarding_verifier as verifier
from capability_profile import CapabilityProfile
from context_registry import ContextRegistry
from scripts.run_onboarding_verifier import _recent_completed_rows


def _state(record) -> tuple[str, str]:
    return record.classification, record.health


def rebuild_profile(profile: CapabilityProfile, observations: list,
                    *, now: datetime) -> dict:
    """Pure/injectable rebuild core; the profile's sink controls persistence."""
    before = {record.record_key: _state(record) for record in profile.records()}
    previous_health = {record.record_key: record.health for record in profile.records()}
    for record in profile.records():
        verifier.reset_verifier_evidence(record)
        profile._store(record)

    ordered = sorted(observations, key=lambda obs: obs.observed_at or now)
    touched = set()
    for obs in ordered:
        touched.update(record.record_key for record in verifier.accumulate(profile, obs, now=now))

    for record in profile.records():
        verifier.reconcile_rebuilt_health(
            record, previous_health.get(record.record_key, record.health), now=now)
        profile._store(record)

    proposals = []
    for record in list(profile.records()):
        proposal = verifier.evaluate_promotion(profile, record, now=now) \
            or verifier.evaluate_block(record)
        if proposal is None:
            continue
        profile.transition(proposal.record_key, proposal.to_classification,
                           proposal.reason, authority=policy.AUTHORITY_AUTO)
        proposal.applied = True
        proposals.append(proposal)
    verifier.narrow_grandfathered_bridges(profile)

    after = {record.record_key: _state(record) for record in profile.records()}
    changed = {key: {"before": before.get(key), "after": state}
               for key, state in after.items() if before.get(key) != state}
    return {
        "observations": len(ordered), "touched": len(touched),
        "proposals": proposals, "changed": changed,
        "before": Counter(before.values()), "after": Counter(after.values()),
    }


def _observations(rows: list[dict], registry: ContextRegistry) -> list:
    out = []
    for row in rows:
        resolution = registry.resolve(row.get("Sport", ""), game_date=row.get("Game Date"),
                                      event_id=row.get("Event ID", ""))
        observation = verifier.observation_from_bet(row, resolution)
        if observation is not None:
            out.append(observation)
    return out


def _backup(rows: list[dict]) -> Path:
    output = REPO_ROOT / "local-run-outputs"
    output.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = output / f"capabilities-before-evidence-rebuild-{stamp}.json"
    path.write_text(json.dumps(rows, indent=2, default=str), encoding="utf-8")
    return path


def run(*, apply: bool, limit_days: int = 14) -> dict:
    rows = capability_profile._load_capability_rows()
    if rows is None:
        raise RuntimeError("Capabilities tab unreadable (fail closed)")
    backup = _backup(rows) if apply else None
    sink = capability_profile._BatchLiveSink() if apply else None
    profile = CapabilityProfile(rows, sink=sink)
    registry = ContextRegistry.load()
    bets = _recent_completed_rows(limit_days)
    result = rebuild_profile(profile, _observations(bets, registry), now=policy.now_utc())
    result["writes"] = sink.flush() if sink is not None else 0
    result["backup"] = str(backup) if backup else None
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write rebuilt evidence")
    parser.add_argument("--limit-days", type=int, default=14)
    args = parser.parse_args(argv)
    try:
        result = run(apply=args.apply, limit_days=args.limit_days)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"Evidence rebuild: observations={result['observations']} "
          f"touched={result['touched']} changed_states={len(result['changed'])} "
          f"writes={result['writes']} apply={args.apply}")
    print("Before:", dict(result["before"]))
    print("After: ", dict(result["after"]))
    for key, change in sorted(result["changed"].items()):
        print(f"  {key}: {change['before']} -> {change['after']}")
    if result["backup"]:
        print(f"Backup: {result['backup']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
