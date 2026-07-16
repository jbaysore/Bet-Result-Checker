"""Canonical identity registry for New Context Onboarding (plan §0.2, Phase 1).

Resolves the several vocabularies a bet is described in (TOA sport key, sheet
display name, ESPN route tuple) onto ONE canonical `context_id` of the form
`family/competition[/edition]`. Aliases point at a context_id; they never carry
trust (concept: identity hierarchy). The single safety rule dominates every
branch: **ambiguity or no match resolves toward NEW, never toward a known
context** (concept safety #3 — alias collision must not inherit trust).

The registry lives in the `Context Registry` sheet tab (checker owns writes,
everyone reads). This module never writes; it loads rows and resolves. If the
tab cannot be read, resolution fails closed to NEW (concept safety #14).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

REGISTRY_TAB = "Context Registry"

# One row per alias. `Status`=retired excludes a stale provider-key mapping so a
# reused TOA key resolves to its CURRENT context, not the old one. Edition
# Start/End bound an alias→context mapping in time (tournament editions, and the
# window during which a reused provider key mapped to a given context).
REGISTRY_COLUMNS = [
    "Context ID", "Alias Type", "Alias Value", "Edition Start", "Edition End",
    "Mapping Version", "Status", "Notes",
]

# Alias types recognised at resolve time. Extendable; v1 resolves primarily by
# the TOA sport key (the value in the Bets `Sport` column).
ALIAS_SPORT_KEY = "sport_key"
ALIAS_DISPLAY_NAME = "display_name"
ALIAS_ESPN_ROUTE = "espn_route"

STATUS_ACTIVE = "active"
STATUS_RETIRED = "retired"

# Resolution confidence. Only KNOWN authorizes reuse of an existing context's
# capability records; NEW forces onboarding.
CONF_KNOWN = "KNOWN"
CONF_NEW = "NEW"


@dataclass(frozen=True)
class ContextResolution:
    context_id: str | None
    confidence: str            # CONF_KNOWN | CONF_NEW
    via_alias: str = ""        # the alias value that matched (for provenance)
    reason: str = ""           # why NEW, when NEW

    @property
    def is_known(self) -> bool:
        return self.confidence == CONF_KNOWN and self.context_id is not None


def _norm(value) -> str:
    return str(value or "").strip().casefold()


def _parse_date(value) -> date | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    # ISO datetime with time component.
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _coerce_game_date(game_date) -> date | None:
    if isinstance(game_date, datetime):
        return game_date.date()
    if isinstance(game_date, date):
        return game_date
    return _parse_date(game_date)


@dataclass(frozen=True)
class _Alias:
    context_id: str
    alias_type: str
    alias_value: str
    edition_start: date | None
    edition_end: date | None
    status: str

    def active_on(self, game_date: date | None) -> bool:
        if self.status != STATUS_ACTIVE:
            return False
        if self.edition_start and (game_date is None or game_date < self.edition_start):
            return False
        if self.edition_end and (game_date is None or game_date > self.edition_end):
            return False
        return True

    @property
    def is_edition_bounded(self) -> bool:
        return self.edition_start is not None or self.edition_end is not None


class ContextRegistry:
    """In-memory index over registry rows. Construct directly from rows for
    tests; `ContextRegistry.load()` builds one from the live tab (fail-closed)."""

    def __init__(self, rows: list[dict] | None):
        # rows is None → the tab could not be read → fail closed: everything NEW.
        self._readable = rows is not None
        self._aliases: list[_Alias] = []
        for row in rows or []:
            context_id = str(row.get("Context ID", "")).strip()
            alias_value = str(row.get("Alias Value", "")).strip()
            if not context_id or not alias_value:
                continue
            self._aliases.append(_Alias(
                context_id=context_id,
                alias_type=str(row.get("Alias Type", ALIAS_SPORT_KEY)).strip() or ALIAS_SPORT_KEY,
                alias_value=alias_value,
                edition_start=_parse_date(row.get("Edition Start")),
                edition_end=_parse_date(row.get("Edition End")),
                status=(str(row.get("Status", STATUS_ACTIVE)).strip().lower() or STATUS_ACTIVE),
            ))

    @property
    def readable(self) -> bool:
        return self._readable

    def resolve(self, sport_key: str, team1: str = "", team2: str = "",
                game_date=None, *, alias_type: str = ALIAS_SPORT_KEY) -> ContextResolution:
        """Resolve an alias (default: the TOA sport key) to a canonical context.

        Returns CONF_KNOWN only when exactly one active context matches; any
        ambiguity (a key mapped to two contexts with overlapping validity) or no
        match returns CONF_NEW. team1/team2 are accepted for future event-level
        disambiguation; v1 resolves by key + edition window.
        """
        if not self._readable:
            return ContextResolution(None, CONF_NEW, reason="registry unreadable (fail closed)")

        key = _norm(sport_key)
        if not key:
            return ContextResolution(None, CONF_NEW, reason="empty alias")

        gd = _coerce_game_date(game_date)
        matches = [a for a in self._aliases
                   if a.alias_type == alias_type and _norm(a.alias_value) == key]
        if not matches:
            return ContextResolution(None, CONF_NEW, via_alias=sport_key, reason="no alias match")

        active = [a for a in matches if a.active_on(gd)]
        # An unbounded alias with no game_date is still active; but if a key has
        # edition-bounded mappings and we have no date to place the event, we
        # cannot safely pick an edition → NEW.
        if not active:
            if gd is None and any(a.is_edition_bounded for a in matches):
                return ContextResolution(None, CONF_NEW, via_alias=sport_key,
                                         reason="edition-bounded alias, no game date")
            return ContextResolution(None, CONF_NEW, via_alias=sport_key,
                                     reason="no active alias for date")

        distinct = {a.context_id for a in active}
        if len(distinct) == 1:
            return ContextResolution(active[0].context_id, CONF_KNOWN, via_alias=sport_key)
        return ContextResolution(None, CONF_NEW, via_alias=sport_key,
                                 reason=f"ambiguous: {len(distinct)} contexts match")

    @classmethod
    def load(cls) -> "ContextRegistry":
        """Build a registry from the live Context Registry tab. On any read
        failure (missing tab, quota, parse) returns an UNREADABLE registry so
        every resolve fails closed to NEW."""
        return cls(_load_registry_rows())


# ── Live sheet loading (fail-closed) ─────────────────────────────────────────
def _load_registry_rows() -> list[dict] | None:
    """Read the Context Registry tab as a list of header-keyed dicts, or None on
    ANY failure. None is the fail-closed sentinel (concept safety #14)."""
    try:
        import gspread  # local import: the resolver must not hard-depend on gspread
        from sheets_reader import _get_spreadsheet
        from sheets_quota import call_with_sheets_retry

        tab = call_with_sheets_retry(
            f"worksheet({REGISTRY_TAB})", _get_spreadsheet().worksheet, REGISTRY_TAB)
        values = call_with_sheets_retry(
            f"{REGISTRY_TAB} get_all_values", tab.get_all_values)
    except Exception:  # gspread.WorksheetNotFound, quota, network, import — all fail closed
        return None
    if not values:
        return []
    headers = values[0]
    return [dict(zip(headers, row)) for row in values[1:]]


# Module-level cached registry for hot-path callers; refreshed by clearing.
_cached: ContextRegistry | None = None


def get_registry(refresh: bool = False) -> ContextRegistry:
    global _cached
    if _cached is None or refresh:
        _cached = ContextRegistry.load()
    return _cached


def resolve(sport_key: str, team1: str = "", team2: str = "", game_date=None) -> ContextResolution:
    """Convenience over the cached live registry."""
    return get_registry().resolve(sport_key, team1, team2, game_date)
