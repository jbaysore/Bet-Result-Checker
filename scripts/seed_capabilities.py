"""Seed the `Capabilities` and `Context Registry` tabs from the Phase 0
inventory (plan Phase 1). Idempotent and protective:

  - appends only seed rows whose Record Key / alias is not already present;
  - REFUSES to overwrite any Capabilities row whose Notes is not a `seeded:`
    marker (that would be real, earned evidence — never clobbered);
  - re-running after a partial seed fills only the gaps.

Defaults to a dry run. `--apply` performs the writes; it needs the three tabs to
already exist (Gate P0, Josh's manual step).

Run:  py scripts/seed_capabilities.py                 # dry run (report only)
      py scripts/seed_capabilities.py --apply         # write to the live sheet
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from capability_profile import CAPABILITIES_TAB, CAPABILITY_COLUMNS
from context_registry import ALIAS_SPORT_KEY, REGISTRY_COLUMNS, REGISTRY_TAB
from scripts.onboarding_inventory import ODDS_TOOL_SCANNER_CONFIG, build_seed_rows

SEED_NOTE_PREFIX = "seeded:"


# ── Pure planning (unit-tested) ──────────────────────────────────────────────
def build_registry_rows(seed_rows: list[dict]) -> list[dict]:
    """Derive Context Registry alias rows from the seed rows' recorded aliases.

    Every seed row carries `alias=<sport_key>` in its Notes; each distinct
    (context_id, sport_key) becomes one active sport_key alias row. Aliases carry
    no trust — they only map a provider key onto a canonical context.
    """
    seen: set[tuple[str, str]] = set()
    rows: list[dict] = []
    for seed in seed_rows:
        context_id = seed["Context ID"]
        notes = seed.get("Notes", "")
        alias_value = ""
        if "alias=" in notes:
            alias_value = notes.split("alias=", 1)[1].split(";", 1)[0].strip()
        if not alias_value:
            continue
        key = (context_id, alias_value)
        if key in seen:
            continue
        seen.add(key)
        rows.append({
            "Context ID": context_id, "Alias Type": ALIAS_SPORT_KEY,
            "Alias Value": alias_value, "Edition Start": "", "Edition End": "",
            "Mapping Version": "1", "Status": "active", "Notes": SEED_NOTE_PREFIX,
        })
    return rows


def plan_capability_writes(existing_rows: list[dict], seed_rows: list[dict]) -> dict:
    """Diff seed rows against what's already in the tab.

    Returns dict with `append` (new seed rows), `skip` (record keys already
    seeded), and `refuse` (record keys present but NOT seed-marked — real
    evidence we must not overwrite)."""
    by_key: dict[str, dict] = {}
    for row in existing_rows:
        key = str(row.get("Record Key", "")).strip()
        if key:
            by_key[key] = row

    append, skip, refuse = [], [], []
    for seed in seed_rows:
        key = seed["Record Key"]
        existing = by_key.get(key)
        if existing is None:
            append.append(seed)
        elif str(existing.get("Notes", "")).strip().startswith(SEED_NOTE_PREFIX):
            skip.append(key)
        else:
            refuse.append(key)
    return {"append": append, "skip": skip, "refuse": refuse}


def plan_registry_writes(existing_rows: list[dict], registry_rows: list[dict]) -> dict:
    existing = {(str(r.get("Context ID", "")).strip(), str(r.get("Alias Value", "")).strip())
                for r in existing_rows}
    append = [r for r in registry_rows
              if (r["Context ID"], r["Alias Value"]) not in existing]
    return {"append": append, "skip": len(registry_rows) - len(append)}


# ── Live sheet access (only touched by --apply) ─────────────────────────────
def _read_tab_rows(tab_name: str) -> list[dict] | None:
    try:
        from sheets_reader import _get_spreadsheet
        from sheets_quota import call_with_sheets_retry

        tab = call_with_sheets_retry(
            f"worksheet({tab_name})", _get_spreadsheet().worksheet, tab_name)
        values = call_with_sheets_retry(f"{tab_name} get_all_values", tab.get_all_values)
    except Exception as exc:
        print(f"[seed_capabilities] ⚠ could not read {tab_name}: {exc}", file=sys.stderr)
        return None
    if not values:
        return []
    headers = values[0]
    return [dict(zip(headers, row)) for row in values[1:]]


def _append_rows(tab_name: str, columns: list[str], rows: list[dict]) -> None:
    from sheets_reader import _get_spreadsheet
    from sheets_quota import call_with_sheets_retry

    tab = call_with_sheets_retry(
        f"worksheet({tab_name})", _get_spreadsheet().worksheet, tab_name)
    payload = [[row.get(col, "") for col in columns] for row in rows]
    if payload:
        call_with_sheets_retry(f"{tab_name} append_rows", tab.append_rows, payload)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write to the live sheet")
    parser.add_argument("--scanner-config", default=str(ODDS_TOOL_SCANNER_CONFIG))
    args = parser.parse_args(argv)

    seed_rows = build_seed_rows(Path(args.scanner_config))
    registry_rows = build_registry_rows(seed_rows)
    print(f"Built {len(seed_rows)} Capabilities seeds, {len(registry_rows)} registry aliases.")

    if not args.apply:
        print("Dry run - pass --apply to write. Nothing changed.")
        print(f"  Capabilities tab: {CAPABILITIES_TAB}  ({len(CAPABILITY_COLUMNS)} cols)")
        print(f"  Registry tab:     {REGISTRY_TAB}  ({len(REGISTRY_COLUMNS)} cols)")
        return 0

    existing_caps = _read_tab_rows(CAPABILITIES_TAB)
    existing_reg = _read_tab_rows(REGISTRY_TAB)
    if existing_caps is None or existing_reg is None:
        print("Aborting: a target tab is missing/unreadable. Create the tabs first "
              "(Gate P0).", file=sys.stderr)
        return 1

    cap_plan = plan_capability_writes(existing_caps, seed_rows)
    reg_plan = plan_registry_writes(existing_reg, registry_rows)

    if cap_plan["refuse"]:
        print(f"Aborting: {len(cap_plan['refuse'])} Capabilities rows exist with real "
              f"(non-seed) evidence and would be shadowed. Investigate:", file=sys.stderr)
        for key in cap_plan["refuse"][:20]:
            print(f"  refuse: {key}", file=sys.stderr)
        return 1

    _append_rows(CAPABILITIES_TAB, CAPABILITY_COLUMNS, cap_plan["append"])
    _append_rows(REGISTRY_TAB, REGISTRY_COLUMNS, reg_plan["append"])
    print(f"Applied: +{len(cap_plan['append'])} capability rows "
          f"({len(cap_plan['skip'])} already seeded), "
          f"+{len(reg_plan['append'])} registry aliases ({reg_plan['skip']} already present).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
