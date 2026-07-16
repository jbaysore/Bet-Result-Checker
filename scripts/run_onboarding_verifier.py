"""Post-event onboarding verifier pass (plan Phase 4).

Reads settled+captured Bets rows, derives an Observation per row, and folds the
evidence into the Capabilities profile — promoting Discovered grains that meet
the bar and demoting Verified grains contradicted by fresh evidence. Meant to run
in the checker's cadence slot where recovery runs (wire into the worker loop once
reviewed); until then it is a manual/cron entrypoint.

Flags interact with config:
  - dry run (default): loads the profile with NO sink → reports proposals,
    writes nothing.
  - --apply: writes. Evidence counters + `proposed:` markers always persist;
    promotion TRANSITIONS apply only when ONBOARDING_PROMOTE_SHADOW is False.
    Demotions always apply (never shadowed).

Run:  py scripts/run_onboarding_verifier.py                 # dry run
      py scripts/run_onboarding_verifier.py --apply --limit-days 14
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import config
import capability_profile
import onboarding_policy as policy
import onboarding_verifier as verifier
from capability_profile import CapabilityProfile
from context_registry import ContextRegistry


def _load_profile(dry_run: bool) -> CapabilityProfile:
    rows = capability_profile._load_capability_rows()
    sink = None if dry_run else capability_profile._LiveSink()
    return CapabilityProfile(rows, sink=sink)


def _recent_completed_rows(limit_days: int) -> list[dict]:
    from sheets_reader import _get_spreadsheet
    from config import SHEET_TAB

    values = _get_spreadsheet().worksheet(SHEET_TAB).get_all_values()
    if not values:
        return []
    headers = values[0]
    cutoff = policy.now_utc() - __import__("datetime").timedelta(days=limit_days)
    rows = []
    for raw in values[1:]:
        row = dict(zip(headers, raw))
        if not str(row.get("Closing Quality") or "").strip():
            continue  # not captured yet — nothing to verify
        observed = policy.parse_utc_datetime(row.get("Closing Observed At"))
        if observed is not None and observed < cutoff:
            continue
        rows.append(row)
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write evidence/markers (and transitions when not in promote-shadow)")
    parser.add_argument("--limit-days", type=int, default=14, help="only consider rows captured within N days")
    args = parser.parse_args(argv)

    profile = _load_profile(dry_run=not args.apply)
    if not profile.readable:
        print("Capabilities tab unreadable — nothing to do (fail closed).", file=sys.stderr)
        return 1
    registry = ContextRegistry.load()

    observations = []
    for row in _recent_completed_rows(args.limit_days):
        resolution = registry.resolve(row.get("Sport", ""), game_date=row.get("Game Date"))
        obs = verifier.observation_from_bet(row, resolution)
        if obs is not None:
            observations.append(obs)

    # apply transitions only when NOT in promote-shadow; demotions always apply.
    apply_transitions = args.apply and config.ONBOARDING_ENFORCE and not config.ONBOARDING_PROMOTE_SHADOW
    proposals = verifier.run_verification(
        profile, observations, apply=apply_transitions, log_fn=verifier_log)

    print(f"Observations: {len(observations)} | proposals: {len(proposals)} "
          f"(apply={args.apply}, promote_shadow={config.ONBOARDING_PROMOTE_SHADOW})")
    for p in proposals:
        verb = "APPLIED" if p.applied else "proposed"
        print(f"  {verb}: {p.record_key} {p.from_classification}->{p.to_classification} : {p.reason}")
    return 0


def verifier_log(event: str, payload: dict) -> None:
    print(f"[verifier] {event}: {payload}")


if __name__ == "__main__":
    raise SystemExit(main())
