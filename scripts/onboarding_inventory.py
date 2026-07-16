"""Phase 0 inventory: turn today's implicit support tables into proposed seed
`Capabilities` rows (NEW_CONTEXT_ONBOARDING_PLAN.md §0.4, Phase 0 deliverable 1).

This script IMPORTS the live support tables (never copies them) so the proposal
can never drift from what the code actually does:

  - actual_start.ESPN_ROUTES / TENNIS_ROUTES / COMBAT_ROUTES → start_authoritative
  - closing_provenance.TRUSTED_FLIP_SPORTS                    → start_live
  - resolver.AUTOMATED_BET_TYPES (market classes)            → settlement
  - pinnacle_closing.FEATURED_MARKETS                        → benchmark
  - odds-tool scannerConfig.supportedSports + marketsBySportClass → discovery, capture

Every emitted row is Classification=Verified, Health=Fresh, Policy Version=1,
Notes="seeded: <source table>" — grandfathering current behavior exactly so no
existing row's trust changes when seeded (plan §0.4, Gate P1).

Run:  py scripts/onboarding_inventory.py            # human table to stdout
      py scripts/onboarding_inventory.py --json out.json
      py scripts/onboarding_inventory.py --format json    # JSON to stdout
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import onboarding_policy as policy
from actual_start import COMBAT_ROUTES, ESPN_ROUTES, TENNIS_ROUTES
from closing_provenance import TRUSTED_FLIP_SPORTS
from pinnacle_closing import FEATURED_MARKETS

# odds-tool is a sibling repo; the scanner config is JS, so we read it as text
# rather than import it (no copy: this parses the live file each run).
ODDS_TOOL_SCANNER_CONFIG = REPO_ROOT.parent / "odds-tool" / "scannerConfig.js"


# ── Canonical identity (plan §0.2) ───────────────────────────────────────────
# family/competition[/edition], lowercase, dot-free slug segments. Recurring
# leagues carry no edition. PROPOSED — Josh ratifies the identity scheme at
# Gate P0; this map is the starting point, not a settled registry.
_FAMILY_BY_PREFIX = {
    "baseball_": "baseball",
    "basketball_": "basketball",
    "americanfootball_": "football",
    "icehockey_": "hockey",
    "soccer_": "soccer",
    "mma_": "mma",
    "boxing_": "combat",
    "tennis_": "tennis",
    "manual_": "manual",
}

# Sport keys whose natural competition slug we spell explicitly (dots become
# underscores; a country/league split reads better than the raw TOA key).
_COMPETITION_OVERRIDES = {
    "americanfootball_ncaaf": "ncaaf",
    "americanfootball_cfl": "cfl",
    "soccer_usa_mls": "usa.mls",
    "soccer_fifa_world_cup": "fifa.world_cup",
    "soccer_england_efl_cup": "eng.efl_cup",
    "soccer_epl": "eng.premier_league",
    "soccer_uefa_champs_league": "uefa.champions_league",
    "soccer_spain_la_liga": "esp.la_liga",
    "soccer_germany_bundesliga": "ger.bundesliga",
    "soccer_italy_serie_a": "ita.serie_a",
    "soccer_france_ligue_one": "fra.ligue_1",
    "mma_mixed_martial_arts": "mixed_martial_arts",
    "boxing_boxing": "boxing",
}


def context_id_for_sport_key(sport_key: str) -> str:
    """Derive a proposed canonical context_id from a TOA sport key.

    This is a heuristic seed only. It never guesses an edition (trust resets are
    ratified by Josh, not inferred), and it keeps the TOA key recorded as an
    alias so identity resolution can map back to it.
    """
    key = str(sport_key or "").strip()
    family = next((fam for pre, fam in _FAMILY_BY_PREFIX.items() if key.startswith(pre)), "other")
    if key in _COMPETITION_OVERRIDES:
        competition = _COMPETITION_OVERRIDES[key]
    else:
        # Strip the family prefix; fall back to the whole key.
        prefix = next((pre for pre in _FAMILY_BY_PREFIX if key.startswith(pre)), "")
        competition = key[len(prefix):] if prefix else key
    return f"{family}/{competition}"


# ── Seed row construction (plan §0.3 columns) ────────────────────────────────
SEED_COLUMNS = [
    "Record Key", "Context ID", "Capability", "Qualifier", "Classification",
    "Health", "Activity", "Policy Version", "Evidence Summary", "First Seen",
    "Last Verified", "Last Checked", "Constraints", "Notes",
]


def record_key(context_id: str, capability: str, qualifier: str) -> str:
    """The §0.3 grain key: context_id | capability | qualifier."""
    return f"{context_id}|{capability}|{qualifier}"


def _seed_row(context_id: str, capability: str, qualifier: str, source_table: str,
              *, aliases: str = "", constraints: str = "") -> dict:
    return {
        "Record Key": record_key(context_id, capability, qualifier),
        "Context ID": context_id,
        "Capability": capability,
        "Qualifier": qualifier,
        "Classification": policy.VERIFIED,
        "Health": policy.FRESH,
        "Activity": policy.IDLE,
        "Policy Version": policy.POLICY_VERSION,
        "Evidence Summary": json.dumps({"clean": 0, "irregular_ok": 0, "neg": 0,
                                        "quarantined": 0, "seeded": True},
                                       separators=(",", ":")),
        "First Seen": "",       # seed rows carry no observation timestamps
        "Last Verified": "",    # seeded, not verified from an event
        "Last Checked": "",
        "Constraints": constraints,
        "Notes": f"seeded: {source_table}" + (f"; alias={aliases}" if aliases else ""),
    }


# ── odds-tool scannerConfig.js parsing (read live, no copy) ──────────────────
def _parse_scanner_config(path: Path) -> tuple[list[str], dict[str, list[str]]]:
    """Extract supportedSports (list) and marketsBySportClass (dict) from the JS
    config. Tolerant of comments/trailing commas; returns ([], {}) if missing."""
    if not path.exists():
        return [], {}
    text = path.read_text(encoding="utf-8")

    supported: list[str] = []
    m = re.search(r"supportedSports\s*:\s*\[(.*?)\]", text, re.DOTALL)
    if m:
        supported = re.findall(r"['\"]([a-z0-9_]+)['\"]", m.group(1))

    markets: dict[str, list[str]] = {}
    m = re.search(r"marketsBySportClass\s*:\s*\{(.*?)\n\s*\}", text, re.DOTALL)
    if m:
        body = m.group(1)
        for line in body.splitlines():
            line = line.split("//", 1)[0].strip()
            entry = re.match(r"([A-Za-z0-9_]+)\s*:\s*\[([^\]]*)\]", line)
            if not entry:
                continue
            key = entry.group(1)
            families = re.findall(r"['\"]([a-z0-9_]+)['\"]", entry.group(2))
            markets[key] = families
    return supported, markets


def _market_families_for(sport_key: str, markets_by_class: dict[str, list[str]]) -> list[str]:
    """The market families the scanner fetches for a sport, collapsed to the
    onboarding vocabulary. Falls back to the _default profile (h2h only)."""
    raw = markets_by_class.get(sport_key) or markets_by_class.get("_default") or ["h2h"]
    families: list[str] = []
    for market in raw:
        family = policy.market_family_for(market)
        if family not in families:
            families.append(family)
    return families


# Market families the checker's closing-capture path can retain a line for
# today, independent of what the scanner fetches for +EV discovery. Team sports
# add team totals; combat/tennis/one-offs are moneyline-only in practice.
CAPTURABLE_FAMILIES_TEAM = (policy.MF_H2H, policy.MF_FEATURED, policy.MF_TEAM_TOTAL)
CAPTURABLE_FAMILIES_OTHER = (policy.MF_H2H,)
_TEAM_FAMILIES = {"baseball", "basketball", "football", "hockey", "soccer"}


def _capturable_families(context_id: str) -> tuple[str, ...]:
    family = context_id.split("/", 1)[0]
    return CAPTURABLE_FAMILIES_TEAM if family in _TEAM_FAMILIES else CAPTURABLE_FAMILIES_OTHER


# ── Inventory build ──────────────────────────────────────────────────────────
def build_seed_rows(scanner_config_path: Path = ODDS_TOOL_SCANNER_CONFIG) -> list[dict]:
    """The full proposed seed set, deduplicated by Record Key (first writer wins
    so a more specific source_table note is not clobbered)."""
    rows: dict[str, dict] = {}

    def add(row: dict) -> None:
        rows.setdefault(row["Record Key"], row)

    # start_authoritative — from actual_start route dicts. Qualifier = source.
    for sport_key in ESPN_ROUTES:
        cid = context_id_for_sport_key(sport_key)
        add(_seed_row(cid, policy.CAP_START_AUTHORITATIVE, "espn",
                      "actual_start.ESPN_ROUTES", aliases=sport_key))
    for sport_key in TENNIS_ROUTES:
        cid = context_id_for_sport_key(sport_key)
        add(_seed_row(cid, policy.CAP_START_AUTHORITATIVE, "espn_tennis",
                      "actual_start.TENNIS_ROUTES", aliases=sport_key))
    for sport_key in COMBAT_ROUTES:
        cid = context_id_for_sport_key(sport_key)
        add(_seed_row(cid, policy.CAP_START_AUTHORITATIVE, "espn_fights",
                      "actual_start.COMBAT_ROUTES", aliases=sport_key))
    # MLB actual-start is resolved directly in actual_start.resolve_actual_start
    # (baseball_mlb → mlb_statsapi.firstPitch), not via a route dict.
    add(_seed_row(context_id_for_sport_key("baseball_mlb"), policy.CAP_START_AUTHORITATIVE,
                  "mlb_statsapi", "actual_start.resolve_mlb_actual_start", aliases="baseball_mlb"))

    # start_live — from the trusted live-flip sport allow-list. Qualifier =
    # toa_scores (the /scores pregame→live transition the checker trusts today).
    for sport_key in sorted(TRUSTED_FLIP_SPORTS):
        cid = context_id_for_sport_key(sport_key)
        add(_seed_row(cid, policy.CAP_START_LIVE, "toa_scores",
                      "closing_provenance.TRUSTED_FLIP_SPORTS", aliases=sport_key))

    # ── Known-context set ────────────────────────────────────────────────────
    # The checker resolves + captures for every context that appears in ANY
    # implicit table — not only the scanner's +EV allow-list. identity + capture
    # are grandfathered for all of them so a route-only context (tennis, MMA)
    # that reaches VERIFIED_CLOSE via authoritative start is not falsely
    # downgraded (parity finding, 2026-07-16).
    supported, markets_by_class = _parse_scanner_config(scanner_config_path)
    known: dict[str, str] = {}   # sport_key → provenance label (first writer wins)

    def register(sport_key: str, provenance: str) -> None:
        known.setdefault(sport_key, provenance)

    for sport_key in supported:
        register(sport_key, "odds-tool.scannerConfig.supportedSports")
    for sport_key in ESPN_ROUTES:
        register(sport_key, "actual_start.ESPN_ROUTES")
    for sport_key in TENNIS_ROUTES:
        register(sport_key, "actual_start.TENNIS_ROUTES")
    for sport_key in COMBAT_ROUTES:
        register(sport_key, "actual_start.COMBAT_ROUTES")
    register("baseball_mlb", "actual_start.resolve_mlb_actual_start")
    for sport_key in sorted(TRUSTED_FLIP_SPORTS):
        register(sport_key, "closing_provenance.TRUSTED_FLIP_SPORTS")

    for sport_key, provenance in known.items():
        cid = context_id_for_sport_key(sport_key)
        # identity — resolvable today for every known context (qualifier=toa).
        add(_seed_row(cid, policy.CAP_IDENTITY, "toa", provenance, aliases=sport_key))
        # capture — book-agnostic grandfather at market-family grain; Phase 1
        # narrows to per-book records lazily on encounter.
        for family in _capturable_families(cid):
            add(_seed_row(cid, policy.CAP_CAPTURE, f"any|{family}",
                          "checker closing-capture (book-agnostic)", aliases=sport_key,
                          constraints=json.dumps({"book": "any", "grandfathered": True},
                                                 separators=(",", ":"))))

    # discovery + benchmark + settlement — scoped to the scanner registry (the
    # set of contexts the system actively discovers / benchmarks / auto-settles).
    featured_markets = [m.strip() for m in FEATURED_MARKETS.split(",") if m.strip()]
    pinnacle_families = sorted({policy.market_family_for(m) for m in featured_markets})

    for sport_key in supported:
        cid = context_id_for_sport_key(sport_key)
        add(_seed_row(cid, policy.CAP_DISCOVERY, "toa",
                      "odds-tool.scannerConfig.supportedSports", aliases=sport_key))
        scanned = set(_market_families_for(sport_key, markets_by_class))
        for family in pinnacle_families:
            if family in scanned:
                add(_seed_row(cid, policy.CAP_BENCHMARK, f"{policy.BENCHMARK_SOURCE}|{family}",
                              "pinnacle_closing.FEATURED_MARKETS", aliases=sport_key))
        for market_class in ("moneyline", "spread", "total"):
            add(_seed_row(cid, policy.CAP_SETTLEMENT, f"{market_class}|toa_scores",
                          "resolver.AUTOMATED_BET_TYPES", aliases=sport_key))

    return list(rows.values())


# ── Reporting ────────────────────────────────────────────────────────────────
def _human_table(rows: list[dict]) -> str:
    by_cap: dict[str, list[dict]] = {}
    for row in rows:
        by_cap.setdefault(row["Capability"], []).append(row)

    lines: list[str] = []
    lines.append(f"Proposed seed Capabilities rows: {len(rows)} "
                 f"(POLICY_VERSION={policy.POLICY_VERSION}, all Verified/Fresh)")
    lines.append("=" * 78)
    for capability in sorted(by_cap):
        cap_rows = sorted(by_cap[capability], key=lambda r: (r["Context ID"], r["Qualifier"]))
        lines.append(f"\n{capability}  ({len(cap_rows)} rows)")
        lines.append("-" * 78)
        for row in cap_rows:
            note = row["Notes"].replace("seeded: ", "")
            lines.append(f"  {row['Context ID']:<28} {row['Qualifier']:<22} {note}")
    contexts = sorted({row["Context ID"] for row in rows})
    lines.append("\n" + "=" * 78)
    lines.append(f"Distinct contexts: {len(contexts)}")
    lines.append("  " + ", ".join(contexts))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", metavar="PATH", help="write the seed rows as JSON to PATH")
    parser.add_argument("--format", choices=("table", "json"), default="table",
                        help="stdout format (default: table)")
    parser.add_argument("--scanner-config", default=str(ODDS_TOOL_SCANNER_CONFIG),
                        help="path to odds-tool scannerConfig.js")
    args = parser.parse_args(argv)

    rows = build_seed_rows(Path(args.scanner_config))

    if args.json:
        Path(args.json).write_text(json.dumps(rows, indent=2), encoding="utf-8")
        print(f"[onboarding_inventory] wrote {len(rows)} seed rows → {args.json}", file=sys.stderr)

    if args.format == "json":
        print(json.dumps(rows, indent=2))
    else:
        print(_human_table(rows))
        if not Path(args.scanner_config).exists():
            print(f"\n⚠  odds-tool scannerConfig.js not found at {args.scanner_config} — "
                  "discovery/capture/benchmark/settlement/identity seeds were skipped.",
                  file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
