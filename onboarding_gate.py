"""Hot-path façade for New Context Onboarding enforcement (plan Phase 1/2).

One thin entry point each for the two places the checker touches capability
trust, so `closing_capture_worker`, `closing_odds`, and `poller` need only a
one-line call:

  - `gate_finalize_quality(record, quality)` — on the closing-capture / recovery
    finalize path: if a line WOULD finalize VERIFIED_CLOSE but the bet's
    capability grain is not trusted, shadow-log it and (under ONBOARDING_ENFORCE)
    cap it at PROVISIONAL with an `onboarding:` Notes marker. All other qualities
    pass through untouched — only VERIFIED_CLOSE feeds pooled CLV, so it is the
    only one worth gating.
  - `discover_for_bet(bet)` — on the settlement path: lazily create Discovered
    capability records (and, for a genuinely new context, a Context Registry
    alias) so onboarding starts automatically. Best-effort; never raises, never
    blocks settlement (concept non-goal #3 / safety #6).

Reads fail closed (concept safety #14): an unreadable profile/registry yields
"not trusted", which under enforce means PROVISIONAL — never a silent trust.

The profile + registry are loaded once per process and cached; `set_caches()` /
`reset_caches()` exist for tests and for a forced refresh.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone

import config
import context_registry
import onboarding_policy as policy
from capability_profile import (
    CLV_START, CapabilityProfile, IllegalTransition, record_key,
)
from closing_provenance import QUALITY_PROVISIONAL, QUALITY_VERIFIED, parse_utc

ONBOARDING_MARKER_PREFIX = "onboarding:"
_SHADOW_LOG_PATH = os.getenv("ONBOARDING_SHADOW_LOG_PATH", "shadow_logs/onboarding.jsonl")

# Map the CLV requirement labels to the concrete Discovered grain to create when
# a capability is missing (the "start" requirement's default observation channel
# is the live flip; post-event verification may add start_authoritative later).
_START_LIVE_QUALIFIER = "toa_scores"
_IDENTITY_QUALIFIER = "toa"


# ── Cached profile + registry ────────────────────────────────────────────────
_profile: CapabilityProfile | None = None
_registry: "context_registry.ContextRegistry | None" = None


def get_profile(refresh: bool = False) -> CapabilityProfile:
    global _profile
    if _profile is None or refresh:
        _profile = CapabilityProfile.load()
    return _profile


def get_registry(refresh: bool = False) -> "context_registry.ContextRegistry":
    global _registry
    if _registry is None or refresh:
        _registry = context_registry.ContextRegistry.load()
    return _registry


def set_caches(profile: CapabilityProfile | None = None, registry=None) -> None:
    """Inject profile/registry (tests, or a caller that already loaded them)."""
    global _profile, _registry
    if profile is not None:
        _profile = profile
    if registry is not None:
        _registry = registry


def reset_caches() -> None:
    global _profile, _registry
    _profile = None
    _registry = None


# ── Evaluation ───────────────────────────────────────────────────────────────
@dataclass
class GateResult:
    trusted: bool
    context_id: str | None
    market_family: str
    unresolved: tuple[str, ...] = ()
    marker: str = ""
    reason: str = ""
    context_new: bool = False


def _game_date(record: dict):
    dt = parse_utc(record.get("Commence UTC") or record.get("Game Date") or record.get("game_date"))
    return dt.date() if dt else None


def _field(record: dict, *names: str) -> str:
    for name in names:
        value = record.get(name)
        if value:
            return str(value).strip()
    return ""


def evaluate_capture(record: dict) -> GateResult:
    """Resolve the bet's context and ask the profile whether its CLV grain is
    trusted. Never raises."""
    sport = _field(record, "Sport", "sport")
    book = _field(record, "Book", "book")
    family = policy.market_family_for(
        record.get("Market Key") or record.get("market_key"),
        record.get("Bet Type") or record.get("bet_type"))
    resolution = get_registry().resolve(
        sport, _field(record, "Team 1", "team1"), _field(record, "Team 2", "team2"),
        _game_date(record), _field(record, "Event ID", "event_id"))
    if not resolution.is_known:
        return GateResult(False, None, family,
                          (policy.CAP_IDENTITY, CLV_START, policy.CAP_CAPTURE),
                          marker=f"{ONBOARDING_MARKER_PREFIX} NEW context {sport!r} ({resolution.reason})",
                          reason=resolution.reason, context_new=True)
    decision = get_profile().require_clv(resolution.context_id, book, family)
    return GateResult(
        trusted=decision.trusted, context_id=resolution.context_id, market_family=family,
        unresolved=decision.unresolved, marker=_marker(resolution.context_id, family, decision),
        reason=decision.reason)


def _marker(context_id: str, family: str, decision) -> str:
    cells = []
    for capability in decision.unresolved:
        classification = (decision.matrix.get(capability) or {}).get("classification") or "unseen"
        cells.append(f"{capability}={classification}")
    return f"{ONBOARDING_MARKER_PREFIX} {context_id}|{family} " + " ".join(cells)


# ── Finalize gate (closing_capture_worker + closing_odds) ────────────────────
def gate_finalize_quality(record: dict, quality: str) -> tuple[str, str | None]:
    """Return the (possibly capped) quality and an `onboarding:` marker to write,
    or (quality, None) when nothing changes. Only VERIFIED_CLOSE is gated."""
    if quality != QUALITY_VERIFIED:
        return quality, None
    if not (config.ONBOARDING_SHADOW_MODE or config.ONBOARDING_ENFORCE):
        return quality, None

    result = evaluate_capture(record)
    if result.trusted:
        return quality, None

    log_shadow("would_downgrade", {
        "bet_id": record.get("BetID") or record.get("bet_id"),
        "sport": _field(record, "Sport", "sport"),
        "book": _field(record, "Book", "book"),
        "market_family": result.market_family,
        "context_id": result.context_id,
        "context_new": result.context_new,
        "unresolved": list(result.unresolved),
        "enforced": config.ONBOARDING_ENFORCE,
    })
    if config.ONBOARDING_ENFORCE:
        return QUALITY_PROVISIONAL, result.marker
    return quality, None


# ── Settlement-path discovery (poller) ───────────────────────────────────────
def discover_for_bet(bet: dict) -> dict:
    """Ensure onboarding has started for a bet's grain. Idempotent, best-effort,
    never raises — settlement must proceed regardless (concept safety #6).

    Returns a small summary dict for logging/testing.
    """
    if not (config.ONBOARDING_SHADOW_MODE or config.ONBOARDING_ENFORCE):
        return {"action": "disabled"}
    try:
        result = evaluate_capture(bet)
        if result.trusted:
            # Grandfathered any|family trust is only a bridge. Mint the exact
            # per-book grain on first encounter; it shadows the fallback from
            # the next evaluation onward while it earns its own evidence.
            created = []
            if config.ONBOARDING_ENFORCE and result.context_id:
                qualifier = f"{_field(bet, 'Book', 'book').lower()}|{result.market_family}"
                if _create_discovered(result.context_id, policy.CAP_CAPTURE, qualifier,
                                      "first bet on exact book/market grain"):
                    created.append(f"{policy.CAP_CAPTURE}|{qualifier}")
            return {"action": "trusted", "context_id": result.context_id, "created": created}

        # Shadow mode never mutates the sheet — log what discovery WOULD create
        # and stop. Record/alias creation happens only under enforce.
        if not config.ONBOARDING_ENFORCE:
            log_shadow("would_discover", {
                "bet_id": bet.get("BetID") or bet.get("bet_id"),
                "sport": _field(bet, "Sport", "sport"),
                "context_id": result.context_id, "context_new": result.context_new,
                "unresolved": list(result.unresolved)})
            return {"action": "would_discover", "context_id": result.context_id,
                    "context_new": result.context_new}

        book = _field(bet, "Book", "book")
        if result.context_new:
            context_id = _propose_context_id(_field(bet, "Sport", "sport"))
            created = _discover_new_context(context_id, _field(bet, "Sport", "sport"),
                                            book, result.market_family)
            log_shadow("context_discovered", {
                "sport": _field(bet, "Sport", "sport"), "context_id": context_id,
                "created": created, "enforced": config.ONBOARDING_ENFORCE})
            return {"action": "new_context", "context_id": context_id, "created": created}

        created = _discover_missing_grains(result, book)
        if created:
            log_shadow("grains_discovered", {
                "context_id": result.context_id, "created": created,
                "enforced": config.ONBOARDING_ENFORCE})
        return {"action": "known_gap", "context_id": result.context_id, "created": created}
    except Exception as exc:  # never let discovery touch settlement
        log_shadow("discover_error", {"bet_id": bet.get("BetID") or bet.get("bet_id"),
                                      "error": str(exc)})
        return {"action": "error", "error": str(exc)}


def _grains_for(context_id: str, book: str, market_family: str) -> list[tuple[str, str]]:
    """The (capability, qualifier) grains a bet's CLV depends on."""
    return [
        (policy.CAP_IDENTITY, _IDENTITY_QUALIFIER),
        (policy.CAP_START_LIVE, _START_LIVE_QUALIFIER),
        (policy.CAP_CAPTURE, f"{book.lower()}|{market_family}"),
    ]


def _create_discovered(context_id: str, capability: str, qualifier: str, reason: str) -> bool:
    key = record_key(context_id, capability, qualifier)
    profile = get_profile()
    if profile.get_record(key) is not None:
        return False
    try:
        profile.transition(key, policy.DISCOVERED, reason, context_id=context_id,
                           capability=capability, qualifier=qualifier)
        return True
    except IllegalTransition:
        return False


def _discover_new_context(context_id: str, sport_key: str, book: str, market_family: str) -> list[str]:
    if not _append_registry_alias(context_id, sport_key):
        raise RuntimeError(f"could not persist registry alias for {sport_key}")
    created = []
    for capability, qualifier in _grains_for(context_id, book, market_family):
        if _create_discovered(context_id, capability, qualifier, f"first bet: new context {sport_key}"):
            created.append(f"{capability}|{qualifier}")
    return created


def _discover_missing_grains(result: GateResult, book: str) -> list[str]:
    # Map the unresolved CLV requirement labels onto concrete grains.
    wanted = {
        policy.CAP_IDENTITY: (policy.CAP_IDENTITY, _IDENTITY_QUALIFIER),
        CLV_START: (policy.CAP_START_LIVE, _START_LIVE_QUALIFIER),
        policy.CAP_CAPTURE: (policy.CAP_CAPTURE, f"{book.lower()}|{result.market_family}"),
    }
    created = []
    for label in result.unresolved:
        grain = wanted.get(label)
        if grain and _create_discovered(result.context_id, grain[0], grain[1],
                                        "bet on unverified grain"):
            created.append(f"{grain[0]}|{grain[1]}")
    return created


def _propose_context_id(sport_key: str) -> str:
    from scripts.onboarding_inventory import context_id_for_sport_key
    return context_id_for_sport_key(sport_key)


def _append_registry_alias(context_id: str, sport_key: str) -> bool:
    """Append a Discovered sport_key alias to Context Registry (checker owns it).
    Idempotent-ish: the seed set never contains a NEW context, so this only fires
    for genuinely unseen keys; a duplicate is harmless (resolve dedups by
    context_id)."""
    try:
        from context_registry import REGISTRY_COLUMNS, REGISTRY_TAB
        from sheets_quota import call_with_sheets_retry
        from sheets_reader import _get_spreadsheet

        tab = call_with_sheets_retry(
            f"worksheet({REGISTRY_TAB})", _get_spreadsheet().worksheet, REGISTRY_TAB)
        row = {
            "Context ID": context_id, "Alias Type": "sport_key", "Alias Value": sport_key,
            "Edition Start": "", "Edition End": "", "Mapping Version": "1",
            "Status": "active", "Notes": "discovered: first-bet",
        }
        call_with_sheets_retry("Context Registry append", tab.append_row,
                               [row.get(col, "") for col in REGISTRY_COLUMNS])
        get_registry(refresh=True)  # so a later bet this run resolves the new context
        return True
    except Exception as exc:
        log_shadow("registry_append_error", {"context_id": context_id, "error": str(exc)})
        return False


# ── Shadow logging ───────────────────────────────────────────────────────────
def log_shadow(event: str, payload: dict) -> None:
    entry = {"ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
             "event": event, **payload}
    line = json.dumps(entry, default=str)
    print(f"[onboarding] {event}: {line}")
    try:
        directory = os.path.dirname(_SHADOW_LOG_PATH)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(_SHADOW_LOG_PATH, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass  # Railway has no durable disk; stdout is the durable record there
