"""Unit tests for context_registry — canonical identity resolution.

The single invariant under test: ambiguity or no match resolves toward NEW,
never toward a known context (concept safety #3).
"""

from datetime import date

from context_registry import (
    ALIAS_SPORT_KEY, CONF_KNOWN, CONF_NEW, STATUS_ACTIVE, STATUS_RETIRED,
    ContextRegistry,
)


def alias(context_id, value, *, atype=ALIAS_SPORT_KEY, start="", end="", status=STATUS_ACTIVE):
    return {
        "Context ID": context_id, "Alias Type": atype, "Alias Value": value,
        "Edition Start": start, "Edition End": end, "Mapping Version": "1",
        "Status": status, "Notes": "",
    }


def test_alias_hit_resolves_known():
    reg = ContextRegistry([alias("baseball/mlb", "baseball_mlb")])
    res = reg.resolve("baseball_mlb")
    assert res.is_known
    assert res.context_id == "baseball/mlb"
    assert res.confidence == CONF_KNOWN
    assert res.via_alias == "baseball_mlb"


def test_alias_miss_is_new():
    reg = ContextRegistry([alias("baseball/mlb", "baseball_mlb")])
    res = reg.resolve("cricket_ipl")
    assert res.confidence == CONF_NEW
    assert res.context_id is None


def test_ambiguous_two_contexts_is_new():
    # Same key mapped to two active, unbounded contexts must NOT guess one.
    reg = ContextRegistry([
        alias("soccer/league_a", "soccer_x"),
        alias("soccer/league_b", "soccer_x"),
    ])
    res = reg.resolve("soccer_x")
    assert res.confidence == CONF_NEW
    assert "ambiguous" in res.reason


def test_empty_alias_is_new():
    reg = ContextRegistry([alias("baseball/mlb", "baseball_mlb")])
    assert reg.resolve("").confidence == CONF_NEW


# ── Editions (§0.2) ──────────────────────────────────────────────────────────
def test_edition_resolved_by_game_date_in_window():
    reg = ContextRegistry([
        alias("tennis/atp_wimbledon/2026", "tennis_atp_wimbledon",
              start="2026-06-29", end="2026-07-12"),
    ])
    res = reg.resolve("tennis_atp_wimbledon", game_date=date(2026, 7, 1))
    assert res.is_known
    assert res.context_id == "tennis/atp_wimbledon/2026"


def test_edition_outside_window_is_new():
    reg = ContextRegistry([
        alias("tennis/atp_wimbledon/2026", "tennis_atp_wimbledon",
              start="2026-06-29", end="2026-07-12"),
    ])
    assert reg.resolve("tennis_atp_wimbledon", game_date=date(2027, 7, 1)).confidence == CONF_NEW


def test_edition_bounded_without_game_date_is_new():
    # Cannot safely pick an edition with no date to place the event.
    reg = ContextRegistry([
        alias("tennis/atp_wimbledon/2026", "tennis_atp_wimbledon",
              start="2026-06-29", end="2026-07-12"),
    ])
    res = reg.resolve("tennis_atp_wimbledon")
    assert res.confidence == CONF_NEW
    assert "no game date" in res.reason


# ── Provider-key reuse (§0.2: old evidence must not be inherited) ────────────
def test_reused_provider_key_resolves_to_current_via_status():
    reg = ContextRegistry([
        alias("soccer/old_promotion", "soccer_reused", status=STATUS_RETIRED),
        alias("soccer/new_promotion", "soccer_reused", status=STATUS_ACTIVE),
    ])
    res = reg.resolve("soccer_reused")
    assert res.is_known
    assert res.context_id == "soccer/new_promotion"  # retired mapping excluded


def test_reused_provider_key_resolves_by_date_window():
    reg = ContextRegistry([
        alias("mma/promo_2025", "mma_reused", end="2025-12-31"),
        alias("mma/promo_2026", "mma_reused", start="2026-01-01"),
    ])
    old = reg.resolve("mma_reused", game_date=date(2025, 5, 1))
    new = reg.resolve("mma_reused", game_date=date(2026, 5, 1))
    assert old.context_id == "mma/promo_2025"
    assert new.context_id == "mma/promo_2026"
    assert old.context_id != new.context_id  # distinct records → no inherited trust


# ── Fail-closed ──────────────────────────────────────────────────────────────
def test_unreadable_registry_fails_closed_to_new():
    reg = ContextRegistry(None)  # tab could not be read
    assert not reg.readable
    res = reg.resolve("baseball_mlb")
    assert res.confidence == CONF_NEW
    assert "fail closed" in res.reason
