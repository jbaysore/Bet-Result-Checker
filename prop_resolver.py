"""
MLB player-prop settlement from an official boxscore (Phase 3). PURE given a
boxscore dict — the poller owns fetching + the Final/re-verify two-pass.

Scope guard (accuracy first): v1 parses ONLY machine-generated selections,
"{Player} {Over|Under} {line} {StatLabel}". Anything else → PropManualReview.
Stat labels are kept in lockstep with odds-tool's MARKET_LABEL.

DNP policy (RATIFIED): player entirely absent from the boxscore → VOID; present
but with zero qualifying appearance (batter 0 PA, pitcher 0 BF) → manual
(ambiguous); present with a real appearance and a 0 stat → a genuine 0, settles.
"""

import json
import re
from datetime import datetime, timedelta, timezone

from config import RESULT_WIN, RESULT_LOSS, RESULT_PUSH, RESULT_VOID
from sources.mlb_statsapi import find_player, find_game, is_clean_final, game_pk

# Internal orchestration value — a leg that must go to a human. NEVER written to
# the sheet Result column; the poller turns it into NEEDS_REVIEW / manual.
RESULT_MANUAL = "MANUAL"


class PropManualReview(ValueError):
    """A prop leg that can't be auto-settled (unparseable, unknown stat, or an
    ambiguous zero-appearance DNP). Subclasses ValueError so the poller's single-
    bet ValueError→manual net catches it; the pick'em orchestrator catches it by
    type to route the whole entry to manual."""


# StatLabel (lowercased) → (stat category, extractor over that category's stats).
# Field names verified against a real boxscore (sources/mlb_statsapi docstring).
def _g(stats, key):
    return stats.get(key, 0) or 0


STAT_LABELS = {
    "hits":         ("batting", lambda b: _g(b, "hits")),
    "total bases":  ("batting", lambda b: _g(b, "totalBases")),
    "home runs":    ("batting", lambda b: _g(b, "homeRuns")),
    "rbis":         ("batting", lambda b: _g(b, "rbi")),
    "runs":         ("batting", lambda b: _g(b, "runs")),
    # Singles = H − 2B − 3B − HR (no direct field).
    "singles":      ("batting", lambda b: _g(b, "hits") - _g(b, "doubles") - _g(b, "triples") - _g(b, "homeRuns")),
    "walks":        ("batting", lambda b: _g(b, "baseOnBalls")),
    "h+r+rbi":      ("batting", lambda b: _g(b, "hits") + _g(b, "runs") + _g(b, "rbi")),
    "stolen bases": ("batting", lambda b: _g(b, "stolenBases")),
    "strikeouts":   ("pitching", lambda p: _g(p, "strikeOuts")),
    "outs":         ("pitching", lambda p: _g(p, "outs")),
    "hits allowed": ("pitching", lambda p: _g(p, "hits")),
    "earned runs":  ("pitching", lambda p: _g(p, "earnedRuns")),
}

# Appearance-count field per category — the DNP "did they qualify?" gate.
_APPEARANCE_FIELD = {"batting": "plateAppearances", "pitching": "battersFaced"}

_PROP_RE = re.compile(r"^(.+?)\s+(Over|Under)\s+([\d.]+)\s+(.+)$", re.IGNORECASE)


def parse_prop_selection(selection: str):
    """"{Player} {Over|Under} {line} {StatLabel}" → (player, side, line, label)
    or None when it isn't a machine-generated prop selection."""
    m = _PROP_RE.match((selection or "").strip())
    if not m:
        return None
    return m.group(1).strip(), m.group(2).strip().lower(), float(m.group(3)), m.group(4).strip().lower()


def resolve_prop_leg(boxscore: dict, selection: str) -> str:
    """
    WIN / LOSS / PUSH / VOID for one prop leg, or raise PropManualReview.
      - unparseable / unknown stat label → manual
      - player absent from the boxscore → VOID (clean DNP)
      - player present, zero qualifying appearance → manual (ambiguous)
      - player present with a real appearance → settle (a 0 stat is a genuine 0)
    """
    parsed = parse_prop_selection(selection)
    if parsed is None:
        raise PropManualReview(f"unparseable prop selection: '{selection}'")
    player, side, line, label = parsed
    if label not in STAT_LABELS:
        raise PropManualReview(f"unknown stat label '{label}' in '{selection}'")
    category, extract = STAT_LABELS[label]

    entry = find_player(boxscore, player)
    if entry is None:
        return RESULT_VOID                       # entirely absent → clean DNP void

    cat_stats = (entry.get("stats", {}) or {}).get(category, {}) or {}
    appearances = cat_stats.get(_APPEARANCE_FIELD[category], 0) or 0
    if appearances == 0:
        # Present but never qualified in this category (e.g. a position player
        # bet as a pitcher, or a batter who never came up) → ambiguous → manual.
        raise PropManualReview(
            f"'{player}' present but 0 {category} appearances in '{selection}'"
        )

    value = extract(cat_stats)
    if value == line:
        return RESULT_PUSH                       # integer line landed exactly
    if side == "over":
        return RESULT_WIN if value > line else RESULT_LOSS
    return RESULT_WIN if value < line else RESULT_LOSS   # under


def evaluate_leg(boxscore: dict, selection: str) -> str:
    """Non-raising wrapper: WIN/LOSS/PUSH/VOID or RESULT_MANUAL. Used to build the
    props-observed marker (which must record a value for every leg)."""
    try:
        return resolve_prop_leg(boxscore, selection)
    except PropManualReview:
        return RESULT_MANUAL


def evaluate_legs(boxscore: dict, selections: list[str]) -> dict:
    """{selection: WIN/LOSS/PUSH/VOID/MANUAL} for every leg (order-independent)."""
    return {sel: evaluate_leg(boxscore, sel) for sel in selections}


# ── props-observed Notes marker (stateless re-verify state) ─────────────────
# The re-verify state lives IN the sheet row (Railway has no durable disk): the
# first Final run stamps this marker and does NOT settle; a later run ≥60 min on
# re-compares. Format: `props-observed: <json {sel:result}> @ <iso>`.
_MARKER_PREFIX = "props-observed:"
_MARKER_RE = re.compile(r"props-observed:\s*(\{.*\})\s*@\s*(\S+)")


def format_props_marker(leg_results: dict, iso: str) -> str:
    return f"{_MARKER_PREFIX} {json.dumps(leg_results, sort_keys=True, ensure_ascii=False)} @ {iso}"


def parse_props_marker(notes: str):
    """(leg_results dict, iso) from a Notes cell, or None when no valid marker."""
    m = _MARKER_RE.search(notes or "")
    if not m:
        return None
    try:
        results = json.loads(m.group(1))
    except (ValueError, TypeError):
        return None
    if not isinstance(results, dict):
        return None
    return results, m.group(2)


def _parse_iso(iso: str):
    try:
        s = iso.replace("Z", "+00:00") if iso.endswith("Z") else iso
        dt = datetime.fromisoformat(s)
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    except (ValueError, TypeError, AttributeError):
        return None


def plan_settlement(now_dt, prior_marker, current_results, reverify_minutes=60):
    """
    The Final+re-verify two-pass state machine (PURE). Given the current run's
    box-score results and the prior props-observed marker (or None):
      - no prior marker            → ('observe', None)  — stamp marker, DON'T settle
      - marker younger than window → ('wait', None)     — not enough time elapsed
      - window elapsed, unchanged  → ('settle', current_results)
      - window elapsed, DIFFERENT  → ('changed', (prior, current)) — manual + notify
    A marker with an unparseable timestamp → ('manual', None): we won't settle on
    a corrupted marker (trap #3), and re-observing could loop, so hand to a human.
    """
    if prior_marker is None:
        return ("observe", None)
    prior_results, prior_iso = prior_marker
    marked_at = _parse_iso(prior_iso)
    if marked_at is None:
        return ("manual", None)
    if now_dt < marked_at + timedelta(minutes=reverify_minutes):
        return ("wait", None)
    if current_results == prior_results:
        return ("settle", current_results)
    return ("changed", (prior_results, current_results))


# ── Pick'em entry combination (mode from the "Pickem {mode} {n}-pick" Notes) ──
_PICKEM_RE = re.compile(r"Pickem\s+([A-Za-z]+)\s+(\d+)-pick", re.IGNORECASE)


def parse_pickem_marker(notes: str):
    """(mode_lower, pick_count) from a row's Notes, or None when not a pick'em row."""
    m = _PICKEM_RE.search(notes or "")
    if not m:
        return None
    return m.group(1).lower(), int(m.group(2))


def combine_pickem(mode: str, leg_results: dict) -> dict:
    """
    Decide a pick'em entry's disposition from its per-leg results (RATIFIED §4):
      - any leg still MANUAL (unresolved)         → route 'manual' (human review)
      - flex mode, OR any void/push (reduced)     → route 'manual_payout'
        (reduced/flex payouts come from the site's tables — app-truth)
      - power/standard, all legs WIN              → route 'settle_win'
      - power/standard, a loss and no voids       → route 'settle_loss'
    PUSH is treated as a reduction (a leg landing on its line drops a pick), same
    as VOID. `hits` is the count of winning legs, for the manual-payout note.
    """
    vals = list(leg_results.values())
    n = len(vals)
    hits = sum(1 for v in vals if v == RESULT_WIN)
    base = {"hits": hits, "n": n, "legs": leg_results}

    if RESULT_MANUAL in vals:
        return {"route": "manual", "reason": "a prop leg needs manual review", **base}

    reduced = any(v in (RESULT_VOID, RESULT_PUSH) for v in vals)
    if (mode or "").lower() == "flex" or reduced:
        return {"route": "manual_payout", **base}
    if hits == n:
        return {"route": "settle_win", **base}
    return {"route": "settle_loss", **base}


def resolve_prop_entry(legs, now_dt, prior_marker, schedule_provider,
                       boxscore_provider, reverify_minutes=60):
    """
    Orchestrate one prop ENTRY (a single-prop bet = 1 leg, or a pick'em parlay =
    N legs) through the Final+re-verify two-pass. PURE — the two fetchers are
    injected (schedule_provider(game_date)->games|None, boxscore_provider(gamePk)
    ->box|None) so this is unit-tested without HTTP.

    legs: [{'selection', 'team1', 'team2', 'game_date'}, ...].
    Returns a decision dict; the poller does the I/O it implies:
      {'action': 'retry'}        — a fetch failed transiently → try next run
      {'action': 'wait_final'}   — a leg's game isn't a clean Final yet
      {'action': 'manual', ...}  — a leg's game not found / ambiguous (doubleheader)
      {'action': 'observe', 'current': {...}}  — stamp marker, DON'T settle (pass 1)
      {'action': 'wait', 'current': {...}}     — marker too young, do nothing
      {'action': 'changed', 'prior': {...}, 'current': {...}} — stats moved → manual
      {'action': 'settle', 'current': {...}}   — confirmed; caller applies result(s)
    """
    sched_cache, box_cache, current = {}, {}, {}
    for leg in legs:
        gd = leg["game_date"]
        if gd not in sched_cache:
            sched_cache[gd] = schedule_provider(gd)
        games = sched_cache[gd]
        if games is None:
            return {"action": "retry", "reason": f"schedule fetch failed for {gd}"}
        game = find_game(games, leg["team1"], leg["team2"])
        if game is None:
            return {"action": "manual",
                    "reason": f"game not found/ambiguous: {leg['team1']} vs {leg['team2']} on {gd}"}
        if not is_clean_final(game):
            return {"action": "wait_final", "reason": "a leg game is not a clean Final yet"}
        pk = game_pk(game)
        if pk not in box_cache:
            box_cache[pk] = boxscore_provider(pk)
        box = box_cache[pk]
        if box is None:
            return {"action": "retry", "reason": f"boxscore fetch failed for gamePk {pk}"}
        current[leg["selection"]] = evaluate_leg(box, leg["selection"])

    action, payload = plan_settlement(now_dt, prior_marker, current, reverify_minutes)
    if action == "changed":
        return {"action": "changed", "prior": payload[0], "current": payload[1]}
    if action == "manual":
        return {"action": "manual", "reason": "corrupt props-observed marker"}
    if action == "settle":
        return {"action": "settle", "current": current}
    return {"action": action, "current": current}   # 'observe' | 'wait'
