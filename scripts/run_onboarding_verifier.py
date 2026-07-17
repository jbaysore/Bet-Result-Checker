"""Post-event onboarding verifier pass (plan Phase 4).

Reads settled+captured Bets rows, derives an Observation per row, and folds the
evidence into the Capabilities profile — promoting Discovered grains that meet
the bar and demoting Verified grains contradicted by fresh evidence. `trigger.py`
runs this pass on the normal scheduled checker cadence; this file also remains a
manual dry-run/apply entrypoint.

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
from datetime import datetime, timezone
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
from closing_provenance import SAFETY_MARGIN_SECONDS


def _load_profile(dry_run: bool) -> tuple[CapabilityProfile, object | None]:
    rows = capability_profile._load_capability_rows()
    sink = None if dry_run else capability_profile._BatchLiveSink()
    return CapabilityProfile(rows, sink=sink), sink


def _recent_completed_rows(limit_days: int | None) -> list[dict]:
    from sheets_reader import _get_spreadsheet
    from config import SHEET_TAB

    values = _get_spreadsheet().worksheet(SHEET_TAB).get_all_values()
    if not values:
        return []
    headers = values[0]
    cutoff = (policy.now_utc() - __import__("datetime").timedelta(days=limit_days)
              if limit_days is not None else None)
    rows = []
    for row_idx, raw in enumerate(values[1:], start=2):
        row = dict(zip(headers, raw))
        row["__row_idx"] = row_idx
        result = str(row.get("Result") or "").strip().upper()
        if not result or result in {"PENDING", "NEEDS_REVIEW"}:
            continue  # evidence is post-event; do not repeatedly judge live rows
        if not str(row.get("Closing Quality") or "").strip():
            continue  # not captured yet — nothing to verify
        observed = policy.parse_utc_datetime(row.get("Closing Observed At"))
        if cutoff is not None and observed is not None and observed < cutoff:
            continue
        rows.append(row)
    return rows


def _record_matches_row(record, row: dict, registry: ContextRegistry) -> bool:
    context_id = str(row.get("Context ID") or "").strip()
    if not context_id:
        resolution = registry.resolve(row.get("Sport", ""), game_date=row.get("Game Date"),
                                      event_id=row.get("Event ID", ""))
        context_id = resolution.context_id if resolution.is_known else ""
    if context_id != record.context_id:
        return False
    if record.capability != policy.CAP_CAPTURE:
        return True
    book, _, family = record.qualifier.partition("|")
    row_family = policy.market_family_for(row.get("Market Key"), row.get("Bet Type"))
    return str(row.get("Book") or "").strip().lower() == book and row_family == family


def _hydrate_actual_starts(rows: list[dict], *, apply: bool) -> int:
    """Resolve missing authoritative starts once; batch-stamp successful facts."""
    from actual_start import resolve_actual_start

    cache = {}
    updates = []
    for row in rows:
        if policy.parse_utc_datetime(row.get("Actual Start")) is not None:
            continue
        bet = {
            "sport": row.get("Sport", ""), "team1": row.get("Team 1", ""),
            "team2": row.get("Team 2", ""), "game_date": row.get("Game Date", ""),
            "game_start": row.get("Game Start Time", ""),
            "event_id": row.get("Event ID", ""),
        }
        key = (bet["sport"], bet["event_id"], bet["game_date"],
               bet["team1"], bet["team2"])
        if key not in cache:
            try:
                cache[key] = resolve_actual_start(bet)
            except Exception as exc:  # provider failure is unresolved evidence
                cache[key] = None
                row["__actual_start_error"] = str(exc)
        resolution = cache[key]
        row["__actual_start_attempted"] = True
        error = str(getattr(resolution, "error", "") or row.get("__actual_start_error") or "")
        row["__actual_start_route_missing"] = error.startswith("no actual-start resolver")
        actual = getattr(resolution, "actual_start", None)
        if actual is None:
            continue
        row["Actual Start"] = actual.isoformat().replace("+00:00", "Z")
        row["Actual Start Source"] = getattr(resolution, "source", "")
        row["Actual Start Confidence"] = getattr(resolution, "confidence", "")
        if not str(row.get("Event ID") or "").strip() and getattr(resolution, "event_id", ""):
            row["Event ID"] = resolution.event_id
        updates.append(row)

    if apply and updates:
        from config import SHEET_TAB
        from sheets_quota import call_with_sheets_retry
        from sheets_reader import _get_spreadsheet

        tab = _get_spreadsheet().worksheet(SHEET_TAB)
        values = tab.get_all_values()
        headers = values[0]
        data = []
        for row in updates:
            for header in ("Actual Start", "Actual Start Source",
                           "Actual Start Confidence", "Event ID"):
                if header not in headers or not str(row.get(header) or "").strip():
                    continue
                col = headers.index(header) + 1
                # gspread helper handles columns beyond Z.
                from gspread.utils import rowcol_to_a1
                data.append({"range": rowcol_to_a1(row["__row_idx"], col),
                             "values": [[row[header]]]})
        if data:
            call_with_sheets_retry(
                "Bets authoritative-start batch", tab.batch_update,
                data, value_input_option="RAW")
    return len(updates)


def _causal_demotions(profile: CapabilityProfile, before: dict) -> list[tuple]:
    out = []
    for record in profile.records():
        prior = before.get(record.record_key)
        if prior is None or prior[0] == record.health:
            continue
        causal = record.health == policy.CONTRADICTED
        if record.health == policy.STALE:
            causal = bool(record.evidence.get("quarantine_events"))
        if causal:
            since = prior[1] or record.last_verified or datetime.min.replace(tzinfo=timezone.utc)
            out.append((record, since))
    return out


def _reflag_causal_rows(rows: list[dict], registry: ContextRegistry,
                        demotions: list[tuple]) -> int:
    from sheets_writer import reflag_demoted_clv_row

    affected: dict[int, dict] = {}
    for record, since in demotions:
        for row in verifier.rows_in_causal_window(rows, since):
            if str(row.get("Closing Quality") or "").strip().upper() != "VERIFIED_CLOSE":
                continue
            if not _record_matches_row(record, row, registry):
                continue
            slot = affected.setdefault(row["__row_idx"], {
                "bet_id": str(row.get("BetID") or ""), "keys": []})
            slot["keys"].append(record.record_key)
    for row_idx, item in affected.items():
        if not reflag_demoted_clv_row(row_idx, item["bet_id"], item["keys"]):
            raise RuntimeError(f"failed causal CLV reflag for row {row_idx}")
    return len(affected)


def ephemeral_row_is_verifiable(row: dict, registry: ContextRegistry) -> bool:
    """A one-off row may be certified directly, but only from its own facts."""
    event_id = str(row.get("Event ID") or "").strip()
    if not event_id or str(row.get("Closing Quality") or "").strip().upper() != "PROVISIONAL":
        return False
    notes = str(row.get("Notes") or "")
    if "onboarding:" not in notes or "onboarding: demoted" in notes:
        return False
    resolution = registry.resolve(row.get("Sport", ""), game_date=row.get("Game Date"),
                                  event_id=event_id)
    if not resolution.is_known or not resolution.event_scoped:
        return False
    observed = policy.parse_utc_datetime(row.get("Closing Observed At"))
    actual = policy.parse_utc_datetime(row.get("Actual Start"))
    if str(row.get("Actual Start Confidence") or "").strip().upper() != "CONFIDENT":
        return False
    return bool(observed and actual
                and (actual - observed).total_seconds() >= SAFETY_MARGIN_SECONDS
                and str(row.get("ClosingOdds") or row.get("Closing Odds") or "").strip())


def _verify_ephemeral_rows(rows: list[dict], registry: ContextRegistry, *, apply: bool) -> int:
    eligible = [row for row in rows if ephemeral_row_is_verifiable(row, registry)]
    if not apply or not eligible:
        return len(eligible)
    from config import SHEET_TAB
    from gspread.utils import rowcol_to_a1
    from sheets_quota import call_with_sheets_retry
    from sheets_reader import _get_spreadsheet

    tab = _get_spreadsheet().worksheet(SHEET_TAB)
    values = tab.get_all_values()
    headers = values[0]
    quality_col = headers.index("Closing Quality") + 1
    notes_col = headers.index("Notes") + 1
    updates = []
    for row in eligible:
        kept = [line for line in str(row.get("Notes") or "").splitlines()
                if line.strip() and not line.strip().startswith("onboarding:")]
        kept.append(f"ephemeral-verified: event={row.get('Event ID')} row evidence only")
        updates.extend([
            {"range": rowcol_to_a1(row["__row_idx"], quality_col),
             "values": [["VERIFIED_CLOSE"]]},
            {"range": rowcol_to_a1(row["__row_idx"], notes_col),
             "values": [["\n".join(kept)]]},
        ])
    call_with_sheets_retry("Bets ephemeral verification", tab.batch_update,
                           updates, value_input_option="RAW")
    return len(eligible)


def _reconcile_pending_bet_intents(profile: CapabilityProfile,
                                   registry: ContextRegistry, *, apply: bool) -> dict:
    """Bets itself is the durable fallback if the at-log queue append failed.

    The onboarding-specific reader includes future games and manually settled
    bet types, so this runs on the next normal checker invocation rather than
    waiting until trigger.py's start-time filter calls poll_bet. The odds-tool
    queue remains the lower-latency path.
    """
    summary = {"examined": 0, "needed": 0, "created": 0, "failed": 0}
    if not apply or not config.ONBOARDING_ENFORCE:
        return summary
    from onboarding_decisions import apply_discovery
    from scripts.onboarding_inventory import context_id_for_sport_key
    from sheets_reader import load_onboarding_bet_intents

    for parent in load_onboarding_bet_intents(config.SHEET_TAB):
        units = parent.get("legs") if parent.get("legs") else [parent]
        for unit in units:
            summary["examined"] += 1
            sport = str(unit.get("sport") or "").strip()
            book = str(unit.get("book") or parent.get("book") or "").strip().lower()
            family = policy.market_family_for(unit.get("market_key"), unit.get("bet_type"))
            if not sport or not book or family == policy.MF_UNKNOWN:
                continue
            resolution = registry.resolve(
                sport, unit.get("team1", ""), unit.get("team2", ""),
                unit.get("game_date"), unit.get("event_id", ""))
            context_id = (resolution.context_id if resolution.is_known
                          else context_id_for_sport_key(sport))
            if resolution.is_known and profile.require_clv(context_id, book, family).trusted:
                continue
            summary["needed"] += 1
            try:
                summary["created"] += apply_discovery(profile, {
                    "Context ID": context_id, "Sport Key": sport,
                    "Book": book, "Market Family": family,
                }, {
                    "intent": "bet", "betId": str(parent.get("bet_id") or ""),
                    "eventId": str(unit.get("event_id") or ""),
                    "loggedAt": policy.now_utc().isoformat(),
                })
            except Exception as exc:
                summary["failed"] += 1
                print(f"[onboarding] pending-bet reconciliation failed for "
                      f"BetID {parent.get('bet_id')}: {exc}")
    if summary["failed"]:
        raise RuntimeError(
            f"{summary['failed']} pending-bet onboarding intent(s) failed; "
            "scheduled run must not report success"
        )
    return summary


def run_once(*, apply: bool, limit_days: int = 14) -> dict:
    """Reusable scheduled pass. Raises on an authoritative write failure."""
    decisions = {"applied": 0, "failed": 0}
    if apply:
        from onboarding_decisions import consume_pending_decisions
        decisions = consume_pending_decisions()
    profile, sink = _load_profile(dry_run=not apply)
    if not profile.readable:
        raise RuntimeError("Capabilities tab unreadable (fail closed)")
    registry = ContextRegistry.load()
    pending_intents = _reconcile_pending_bet_intents(profile, registry, apply=apply)
    if pending_intents["created"]:
        registry = ContextRegistry.load()
    causal_rows = _recent_completed_rows(None)
    cutoff = policy.now_utc() - __import__("datetime").timedelta(days=limit_days)
    rows = [row for row in causal_rows
            if policy.parse_utc_datetime(row.get("Closing Observed At")) is not None
            and policy.parse_utc_datetime(row.get("Closing Observed At")) >= cutoff]
    starts_hydrated = _hydrate_actual_starts(rows, apply=apply)
    ephemeral_verified = _verify_ephemeral_rows(rows, registry, apply=apply)
    before = {record.record_key: (record.health, record.last_checked)
              for record in profile.records()}

    observations = []
    for row in rows:
        resolution = registry.resolve(row.get("Sport", ""), game_date=row.get("Game Date"),
                                      event_id=row.get("Event ID", ""))
        obs = verifier.observation_from_bet(row, resolution)
        if obs is not None:
            observations.append(obs)

    apply_transitions = apply and config.ONBOARDING_ENFORCE and not config.ONBOARDING_PROMOTE_SHADOW
    proposals = verifier.run_verification(
        profile, observations, apply=apply_transitions, log_fn=verifier_log)
    capability_writes = sink.flush() if sink is not None else 0
    reflagged = _reflag_causal_rows(causal_rows, registry, _causal_demotions(profile, before)) \
        if apply else 0
    return {"observations": len(observations), "proposals": proposals,
            "decisions": decisions,
            "reflagged": reflagged,
            "capability_writes": capability_writes,
            "starts_hydrated": starts_hydrated,
            "ephemeral_verified": ephemeral_verified,
            "pending_intents": pending_intents,
            "apply": apply, "promote_shadow": config.ONBOARDING_PROMOTE_SHADOW}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write evidence/markers (and transitions when not in promote-shadow)")
    parser.add_argument("--limit-days", type=int, default=14, help="only consider rows captured within N days")
    args = parser.parse_args(argv)

    try:
        result = run_once(apply=args.apply, limit_days=args.limit_days)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    proposals = result["proposals"]

    print(f"Observations: {result['observations']} | proposals: {len(proposals)} "
          f"(apply={args.apply}, promote_shadow={config.ONBOARDING_PROMOTE_SHADOW})")
    if args.apply:
        print(f"Decisions: {result['decisions']['applied']} applied, "
              f"{result['decisions']['failed']} failed")
        print(f"Causal rows re-flagged: {result['reflagged']}")
    for p in proposals:
        verb = "APPLIED" if p.applied else "proposed"
        print(f"  {verb}: {p.record_key} {p.from_classification}->{p.to_classification} : {p.reason}")
    return 0


def verifier_log(event: str, payload: dict) -> None:
    print(f"[verifier] {event}: {payload}")


if __name__ == "__main__":
    raise SystemExit(main())
