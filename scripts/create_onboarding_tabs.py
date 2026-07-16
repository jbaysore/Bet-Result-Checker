"""Create the three New Context Onboarding tabs in the live sheet (plan Gate P0).

Idempotent: skips any tab that already exists, and only ever ADDS tabs — never
touches the Bets tab or any existing data. Writes the header row from the
column definitions the modules read by name.

  - Capabilities     (checker-written)  — capability_profile.CAPABILITY_COLUMNS
  - Context Registry (checker-written)  — context_registry.REGISTRY_COLUMNS
  - Discovery Queue  (odds-tool-written, append-only) — columns defined here

Run:  py scripts/create_onboarding_tabs.py
Then: py scripts/seed_capabilities.py --apply
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from capability_profile import CAPABILITIES_TAB, CAPABILITY_COLUMNS
from context_registry import REGISTRY_COLUMNS, REGISTRY_TAB

# Discovery Queue is odds-tool-written and append-only (plan §0.1). The checker
# consumes + compacts it from Phase 7; this header is provisional until then.
DISCOVERY_TAB = "Discovery Queue"
DISCOVERY_COLUMNS = [
    "Timestamp", "Kind", "Context ID", "Sport Key", "Book", "Market Family",
    "Payload", "Status",
]

TABS = [
    (CAPABILITIES_TAB, CAPABILITY_COLUMNS, 400),
    (REGISTRY_TAB, REGISTRY_COLUMNS, 120),
    (DISCOVERY_TAB, DISCOVERY_COLUMNS, 200),
]


def main() -> int:
    from sheets_reader import _get_spreadsheet
    from sheets_quota import call_with_sheets_retry

    ss = _get_spreadsheet()
    existing = {ws.title for ws in ss.worksheets()}
    print("Existing tabs:", ", ".join(sorted(existing)))

    for title, columns, nrows in TABS:
        if title in existing:
            print(f"  SKIP  {title!r} (already exists — untouched)")
            continue
        ws = call_with_sheets_retry(
            f"add_worksheet({title})", ss.add_worksheet,
            title=title, rows=nrows, cols=len(columns))
        call_with_sheets_retry(f"{title} header", ws.update, "A1", [columns])
        print(f"  CREATE {title!r} — {len(columns)} headers, {nrows} rows")
    print("Done. Next: py scripts/seed_capabilities.py --apply")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
