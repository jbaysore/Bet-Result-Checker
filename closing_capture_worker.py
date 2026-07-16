"""Always-on, actual-start-aware closing-odds capture worker for Railway."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone

import gspread

from closing_odds import (
    _clv_from_decimals,
    _fetch_live_events,
    fetch_live_closing_odds,
    find_event,
    live_credit_total,
    needs_manual_closing_odds,
    parse_selection,
    to_decimal_odds,
)
from closing_provenance import (
    BOOK_STALE_SECONDS,
    CAP_AFTER_COMMENCE_SECONDS,
    MAX_SAMPLE_AGE_SECONDS,
    QUALITY_EARLY,
    QUALITY_PROVISIONAL,
    QUALITY_STALE,
    QUALITY_VERIFIED,
    SAFETY_MARGIN_SECONDS,
    SAMPLE_CADENCE_SECONDS,
    SAMPLE_SLOTS,
    START_UNKNOWN,
    START_UNVERIFIED,
    START_VERIFIED,
    TRUSTED_FLIP_SPORTS,
    ClosingSample,
    parse_utc,
    quote_is_fresh,
    quality_for_detected_live,
    select_pre_margin_sample,
)
from config import SHEET_ID, SHEET_TAB
from poller import _parse_game_datetime
from sheets_reader import _get_spreadsheet
from sheets_writer import upsert_notes_line, write_closing_odds
from sources.scores_live import COMPLETED, LIVE, PREGAME, event_live_state, fetch_scores_live
from pinnacle_closing import fetch_pinnacle_featured, pinnacle_quote_for_bet
import onboarding_gate


QUEUE_TAB = os.getenv("CLOSING_CAPTURE_TAB", "ClosingCapture")
MIGRATIONS_TAB = "SchemaMigrations"
MIGRATION_KEY = "clv-actual-start-v1"
POLL_SECONDS = max(10, int(os.getenv("CLOSING_CAPTURE_POLL_SECONDS", str(SAMPLE_CADENCE_SECONDS))))
RECONCILE_SECONDS = max(300, int(os.getenv("CLOSING_CAPTURE_RECONCILE_SECONDS", "900")))
DAILY_SOFT_BUDGET = max(1, int(os.getenv("CLOSING_DAILY_SOFT_BUDGET", "2500")))
SLOW_BUDGET_FRACTION = min(1.0, max(0.1, float(os.getenv("CLOSING_SLOW_BUDGET_FRACTION", "0.70"))))
TRACK_BEFORE_COMMENCE_SECONDS = max(
    60, int(os.getenv("CLOSING_TRACK_BEFORE_COMMENCE_SECONDS", "900"))
)
UNKNOWN_GRACE_SECONDS = max(0, int(os.getenv("CLOSING_UNKNOWN_GRACE_SECONDS", "300")))
CRITICAL_WINDOW_SECONDS = max(60, int(os.getenv("CLOSING_CRITICAL_WINDOW_SECONDS", "300")))
_budget_day = ""
_estimated_daily_credits = 0

# The first 25 columns remain immutable during the staged deployment. Both
# repositories accept this required prefix and discover every extension by name.
LEGACY_QUEUE_HEADERS = [
    "BetID", "Commence UTC", "Sport", "Book", "Team 1", "Team 2",
    "Selection", "Bet Type", "OddsTaken", "Market Key", "Status",
    "T-10 Price", "T-10 At", "T-10 Error",
    "T-5 Price", "T-5 At", "T-5 Error",
    "T-1 Price", "T-1 At", "T-1 Error",
    "Final Price", "Finalized At", "Last Error", "Created At", "Updated At",
]

SAMPLE_HEADERS = [
    value
    for slot in range(1, SAMPLE_SLOTS + 1)
    for value in (
        f"Sample {slot} Price", f"Sample {slot} Fetched At",
        f"Sample {slot} Book Last Update", f"Sample {slot} Start State",
        f"Sample {slot} Error",
    )
]
QUEUE_EXTENSION_HEADERS = [
    "Event ID", *SAMPLE_HEADERS, "Start Detected At", "Start Status",
    "Closing Quality", "Closing Source", "Per-Leg/Pinnacle Audit JSON",
]
QUEUE_HEADERS = [*LEGACY_QUEUE_HEADERS, *QUEUE_EXTENSION_HEADERS]

FINAL_STATUSES = {
    "COMPLETED", "SKIPPED_EXISTING", "FALLBACK", "UNSUPPORTED", "INVALID",
    "STALE", "SAFE_BUT_EARLY", "PROVISIONAL",
}
AUTOMATED_BET_TYPES = {"Moneyline", "Spread", "Total", "Draw"}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def active_slot(now: datetime, commence: datetime) -> str | None:
    """Legacy helper retained for old rows/tests; new capture uses rolling slots."""
    remaining = commence - now
    if timedelta(minutes=5) < remaining <= timedelta(minutes=10):
        return "T-10"
    if timedelta(minutes=1) < remaining <= timedelta(minutes=5):
        return "T-5"
    if timedelta(0) < remaining <= timedelta(minutes=1):
        return "T-1"
    return None


def latest_sample(record: dict) -> str | None:
    samples = samples_from_record(record)
    if samples:
        return max(samples, key=lambda sample: sample.fetched_at).price
    for slot in ("T-1", "T-5", "T-10"):
        price = str(record.get(f"{slot} Price", "")).strip()
        if price:
            return price
    return None


def queue_headers_are_compatible(headers: list[str]) -> bool:
    return headers[:len(LEGACY_QUEUE_HEADERS)] == LEGACY_QUEUE_HEADERS


def ensure_queue_tab():
    spreadsheet = _get_spreadsheet()
    try:
        tab = spreadsheet.worksheet(QUEUE_TAB)
    except gspread.WorksheetNotFound:
        tab = spreadsheet.add_worksheet(title=QUEUE_TAB, rows=1000, cols=len(QUEUE_HEADERS))
    headers = tab.row_values(1)
    if not headers:
        tab.update([QUEUE_HEADERS], "A1")
    elif not queue_headers_are_compatible(headers):
        raise RuntimeError(f"{QUEUE_TAB} required 25-column prefix does not match the worker schema")
    elif headers != QUEUE_HEADERS:
        # Staged migration: append/repair only the optional suffix; never rename
        # the tab or disturb the durable legacy prefix.
        if getattr(tab, "col_count", len(headers)) < len(QUEUE_HEADERS):
            tab.resize(cols=len(QUEUE_HEADERS))
        tab.update([QUEUE_HEADERS], "A1")
    return tab


def require_migration_marker():
    """Fail closed until the Bets provenance migration has completed."""
    spreadsheet = _get_spreadsheet()
    try:
        marker = spreadsheet.worksheet(MIGRATIONS_TAB)
    except gspread.WorksheetNotFound as exc:
        raise RuntimeError(
            "Run scripts/migrate_clv_provenance.py before enabling closing capture"
        ) from exc
    applied = {row[0].strip() for row in marker.get_all_values()[1:] if row and row[0].strip()}
    if MIGRATION_KEY not in applied:
        raise RuntimeError(
            f"Required schema migration marker is missing: {MIGRATION_KEY}"
        )


def row_record(headers: list[str], row: list[str]) -> dict:
    padded = row + [""] * max(0, len(headers) - len(row))
    return dict(zip(headers, padded))


def update_queue_row(tab, row_idx: int, changes: dict, headers: list[str] | None = None):
    headers = headers or tab.row_values(1)
    indices = {name: i + 1 for i, name in enumerate(headers)}
    payload = {**changes, "Updated At": iso(utc_now())}
    missing = [key for key in payload if key not in indices]
    if missing:
        raise RuntimeError(f"{QUEUE_TAB} missing required extension columns: {', '.join(missing)}")
    tab.update_cells([gspread.Cell(row_idx, indices[key], value) for key, value in payload.items()])


def samples_from_record(record: dict) -> list[ClosingSample]:
    samples: list[ClosingSample] = []
    for slot in range(1, SAMPLE_SLOTS + 1):
        price = str(record.get(f"Sample {slot} Price", "")).strip()
        fetched_at = parse_utc(record.get(f"Sample {slot} Fetched At"))
        if not price or fetched_at is None:
            continue
        samples.append(ClosingSample(
            price=price,
            fetched_at=fetched_at,
            book_last_update=parse_utc(record.get(f"Sample {slot} Book Last Update")),
            start_state=str(record.get(f"Sample {slot} Start State", "") or "UNKNOWN"),
            slot=slot,
        ))
    return samples


def next_sample_slot(record: dict) -> int:
    samples = samples_from_record(record)
    used = {sample.slot for sample in samples}
    for slot in range(1, SAMPLE_SLOTS + 1):
        if slot not in used:
            return slot
    return min(samples, key=lambda sample: sample.fetched_at).slot or 1


def _record_credits(value, fallback: int = 0) -> None:
    global _budget_day, _estimated_daily_credits
    day = utc_now().date().isoformat()
    if day != _budget_day:
        _budget_day = day
        _estimated_daily_credits = 0
    try:
        charged = max(0, int(value))
    except (TypeError, ValueError):
        charged = max(0, fallback)
    _estimated_daily_credits += charged


def credit_mode() -> str:
    _record_credits(0)
    if _estimated_daily_credits >= DAILY_SOFT_BUDGET:
        # Preserve the core same-book close inside the final five minutes.  At
        # the soft ceiling we drop Pinnacle and non-critical samples, not the
        # actual closing capture the worker exists to obtain.
        return "critical"
    if _estimated_daily_credits >= DAILY_SOFT_BUDGET * SLOW_BUDGET_FRACTION:
        return "slow"
    return "full"


def sample_due(record: dict, now: datetime, cadence_seconds: int | None = None) -> bool:
    samples = samples_from_record(record)
    if not samples:
        return True
    newest = max(samples, key=lambda sample: sample.fetched_at)
    cadence = cadence_seconds or SAMPLE_CADENCE_SECONDS
    return (now - newest.fetched_at).total_seconds() >= cadence


def in_tracking_window(now: datetime, commence: datetime) -> bool:
    return (
        commence - timedelta(seconds=TRACK_BEFORE_COMMENCE_SECONDS)
        <= now
        <= commence + timedelta(seconds=CAP_AFTER_COMMENCE_SECONDS)
    )


def critical_sample_allowed(now: datetime, commence: datetime, start_state: str) -> bool:
    if now >= commence and start_state == "VERIFIED_PREGAME":
        return True
    return now >= commence - timedelta(seconds=CRITICAL_WINDOW_SECONDS)


def unknown_finalize_due(now: datetime, commence: datetime) -> bool:
    return now >= commence + timedelta(seconds=UNKNOWN_GRACE_SECONDS)


def find_bet_row(bet_id: str) -> tuple[int | None, dict | None]:
    rows = _get_spreadsheet().worksheet(SHEET_TAB).get_all_values()
    if not rows or "BetID" not in rows[0]:
        return None, None
    headers = rows[0]
    idx = headers.index("BetID")
    matches = [(i, row) for i, row in enumerate(rows[1:], start=2)
               if idx < len(row) and row[idx].strip() == str(bet_id).strip()]
    if len(matches) != 1:
        return None, None
    row_idx, row = matches[0]
    return row_idx, row_record(headers, row)


def _sample_from_price(record: dict, price: str | None) -> ClosingSample | None:
    if not price:
        return None
    matching = [sample for sample in samples_from_record(record) if sample.price == price]
    return max(matching, key=lambda sample: sample.fetched_at, default=None)


def finalize(
    tab,
    queue_row_idx: int,
    record: dict,
    price: str | None,
    *,
    start_status: str = START_UNKNOWN,
    quality: str = QUALITY_PROVISIONAL,
    start_detected_at: datetime | None = None,
    sample: ClosingSample | None = None,
    headers: list[str] | None = None,
) -> str:
    bet_id = record["BetID"]
    bet_row_idx, bet = find_bet_row(bet_id)
    finalized_at = utc_now()
    if bet_row_idx is None:
        update_queue_row(tab, queue_row_idx, {
            "Status": "INVALID", "Last Error": "BetID missing or duplicated on Bets tab",
            "Finalized At": iso(finalized_at),
        }, headers)
        return "INVALID"
    if str(bet.get("ClosingOdds", "")).strip():
        update_queue_row(tab, queue_row_idx, {
            "Status": "SKIPPED_EXISTING", "Finalized At": iso(finalized_at),
            "Last Error": "ClosingOdds was already populated",
        }, headers)
        return "SKIPPED_EXISTING"
    if not price:
        update_queue_row(tab, queue_row_idx, {
            "Status": "FALLBACK", "Finalized At": iso(finalized_at),
            "Start Detected At": iso(start_detected_at) if start_detected_at else "",
            "Start Status": start_status, "Closing Quality": quality,
            "Closing Source": "worker-live",
            "Last Error": record.get("Last Error") or "No safely pregame sample succeeded",
        }, headers)
        return "FALLBACK"

    # New-context onboarding gate: a would-be VERIFIED_CLOSE whose capability
    # grain is not trusted is shadow-logged and (under ONBOARDING_ENFORCE) capped
    # at PROVISIONAL, with an `onboarding:` marker written below. Never raises.
    quality, onboarding_marker = onboarding_gate.gate_finalize_quality(record, quality)

    sample = sample or _sample_from_price(record, price)
    decimal_closing = to_decimal_odds(price)
    try:
        decimal_taken = float(str(bet.get("DecimalOddsTaken", "")).strip())
    except ValueError:
        decimal_taken = to_decimal_odds(bet.get("OddsTaken"))
    clv = _clv_from_decimals(decimal_taken, decimal_closing)
    pinnacle_close = ""
    pinnacle_clv = ""
    try:
        audit_entries = json.loads(record.get("Per-Leg/Pinnacle Audit JSON") or "[]")
    except (TypeError, ValueError):
        audit_entries = []
    pinnacle_entry = next((entry for entry in reversed(audit_entries)
                           if sample and entry.get("sample_slot") == sample.slot and entry.get("pinnacle")), None)
    if pinnacle_entry:
        quote = pinnacle_entry["pinnacle"]
        fair_decimal = quote.get("fair_decimal")
        fair_american = quote.get("fair_american")
        pinnacle_sample = ClosingSample(
            price=str(quote.get("selected_price") or ""),
            fetched_at=parse_utc(pinnacle_entry.get("fetched_at")) or sample.fetched_at,
            book_last_update=parse_utc(quote.get("book_last_update")),
        )
        if (quote_is_fresh(pinnacle_sample)
                and fair_american is not None and fair_decimal and decimal_taken):
            pinnacle_close = f"+{fair_american}" if fair_american > 0 else str(fair_american)
            pinnacle_clv = round(decimal_taken / float(fair_decimal) - 1, 6)
    provenance = {
        "start_status": start_status,
        "closing_quality": quality,
        "closing_source": "worker-live",
        "closing_observed_at": iso(sample.fetched_at) if sample else iso(finalized_at),
        "start_detected_at": iso(start_detected_at) if start_detected_at else "",
        "actual_start": "",
        "actual_start_source": "scores-detected" if start_detected_at else "",
        "actual_start_confidence": "DETECTED" if start_detected_at else "",
        "pinnacle_close": pinnacle_close,
        "pinnacle_clv": pinnacle_clv,
    }
    wrote = write_closing_odds(
        bet_row_idx, bet_id, price, decimal_closing, clv,
        overwrite_errors=False, provenance=provenance,
    )
    if wrote and onboarding_marker:
        upsert_notes_line(bet_row_idx, bet_id,
                          onboarding_gate.ONBOARDING_MARKER_PREFIX, onboarding_marker)
    status = quality if wrote and quality != QUALITY_VERIFIED else ("COMPLETED" if wrote else "SKIPPED_EXISTING")
    update_queue_row(tab, queue_row_idx, {
        "Status": status, "Final Price": price, "Finalized At": iso(finalized_at),
        "Start Detected At": iso(start_detected_at) if start_detected_at else "",
        "Start Status": start_status, "Closing Quality": quality,
        "Closing Source": "worker-live",
        "Last Error": "" if wrote else "ClosingOdds changed or provenance columns are unavailable",
    }, headers)
    return status


def capture_record(tab, row_idx: int, record: dict, start_state: str,
                   now: datetime, headers: list[str], pinnacle_events: list | None = None,
                   cadence_seconds: int | None = None):
    if not sample_due(record, now, cadence_seconds):
        return
    credits_before = live_credit_total()
    result = fetch_live_closing_odds({
        "bet_id": record["BetID"], "sport": record["Sport"],
        "book": record["Book"], "team1": record["Team 1"],
        "team2": record["Team 2"], "selection": record["Selection"],
        "bet_type": record["Bet Type"], "market_key": record["Market Key"],
        "commence_utc": record.get("Commence UTC", ""),
    })
    _record_credits(live_credit_total() - credits_before)
    fetched_at = parse_utc(result.get("fetched_at")) or now
    slot = next_sample_slot(record)
    price = result.get("closing_odds") or ""
    error = result.get("error") or ""
    changes = {
        f"Sample {slot} Price": price,
        f"Sample {slot} Fetched At": iso(fetched_at),
        f"Sample {slot} Book Last Update": result.get("book_last_update") or "",
        f"Sample {slot} Start State": start_state,
        f"Sample {slot} Error": error,
        "Event ID": record.get("Event ID") or result.get("event_id") or "",
        "Last Error": error,
    }
    if pinnacle_events is not None:
        quote = pinnacle_quote_for_bet(pinnacle_events, {
            "event_id": changes["Event ID"], "bet_type": record["Bet Type"],
            "selection": record["Selection"], "team1": record["Team 1"],
            "team2": record["Team 2"], "commence_utc": record.get("Commence UTC", ""),
        })
        try:
            audit_entries = json.loads(record.get("Per-Leg/Pinnacle Audit JSON") or "[]")
        except (TypeError, ValueError):
            audit_entries = []
        audit_entries.append({
            "sample_slot": slot, "fetched_at": iso(fetched_at), "pinnacle": quote,
        })
        changes["Per-Leg/Pinnacle Audit JSON"] = json.dumps(audit_entries[-SAMPLE_SLOTS:], separators=(",", ":"))
    update_queue_row(tab, row_idx, changes, headers)
    record.update(changes)


def _resolve_event_id(record: dict, games: list, commence: datetime,
                      roster_cache: dict[str, list]) -> str | None:
    event_id = str(record.get("Event ID", "")).strip()
    if event_id:
        return event_id
    # Event-id backfill is deliberately stricter than price lookup.  A repeated
    # matchup (notably an MLB doubleheader) is ambiguous without the original
    # Odds API id, even when one commence time looks closer, so never guess.
    event = find_event(games, record.get("Team 1", ""), record.get("Team 2", ""))
    if event and event.get("id"):
        return event["id"]
    sport = record.get("Sport", "")
    if sport not in roster_cache:
        try:
            roster_cache[sport] = _fetch_live_events(sport)
        except RuntimeError:
            roster_cache[sport] = []
    event = find_event(roster_cache[sport], record.get("Team 1", ""), record.get("Team 2", ""))
    return event.get("id") if event else None


def process_queue(tab, now: datetime | None = None):
    # This is an always-on Railway process. Refresh lazy onboarding reads once
    # per capture cycle so registry edits and verifier transitions take effect
    # without a worker restart.
    onboarding_gate.reset_caches()
    now = now or utc_now()
    rows = tab.get_all_values()
    if not rows:
        return
    headers = rows[0]
    if not queue_headers_are_compatible(headers):
        raise RuntimeError(f"{QUEUE_TAB} required prefix mismatch")
    records = [(idx, row_record(headers, row)) for idx, row in enumerate(rows[1:], start=2)]
    active = [(idx, rec) for idx, rec in records
              if rec.get("BetID") and rec.get("Status") not in FINAL_STATUSES]
    parsed_active = []
    for row_idx, record in active:
        commence = parse_utc(record.get("Commence UTC", ""))
        if commence is None:
            update_queue_row(
                tab, row_idx,
                {"Status": "INVALID", "Last Error": "Invalid Commence UTC"}, headers,
            )
            continue
        parsed_active.append((row_idx, record, commence))

    # H1: rows can enter the queue days in advance, but no paid polling begins
    # until T-15.  Rows already beyond the cap are finalized locally below.
    tracking = [item for item in parsed_active if in_tracking_window(now, item[2])]
    scores_by_sport = {}
    for sport in {rec.get("Sport", "") for _, rec, _ in tracking}:
        if not sport:
            continue
        scores_by_sport[sport] = fetch_scores_live(sport)
        score_credits = scores_by_sport[sport].credits
        _record_credits(score_credits.get("last"), fallback=1)
        print(f"[closing-capture] scores {sport} credits: last={score_credits.get('last')} "
              f"used={score_credits.get('used')} remaining={score_credits.get('remaining')}")
    mode = credit_mode()
    cadence_seconds = SAMPLE_CADENCE_SECONDS * (2 if mode in {"slow", "critical"} else 1)
    pinnacle_by_sport = {}
    if mode != "critical":
        for sport in scores_by_sport:
            pinnacle_by_sport[sport] = fetch_pinnacle_featured(sport)
            pinnacle_credits = pinnacle_by_sport[sport].get("credits", {})
            _record_credits(pinnacle_credits.get("last"), fallback=1)
            print(f"[closing-capture] Pinnacle {sport} credits: last={pinnacle_credits.get('last')} "
                  f"used={pinnacle_credits.get('used')} remaining={pinnacle_credits.get('remaining')}")
    print(f"[closing-capture] credit mode={mode}; estimated daily credits="
          f"{_estimated_daily_credits}/{DAILY_SOFT_BUDGET}; cadence={cadence_seconds}s")
    roster_cache: dict[str, list] = {}

    for row_idx, record, commence in parsed_active:
        if now < commence - timedelta(seconds=TRACK_BEFORE_COMMENCE_SECONDS):
            continue
        if now > commence + timedelta(seconds=CAP_AFTER_COMMENCE_SECONDS):
            samples = samples_from_record(record)
            verified = max(
                (sample for sample in samples if sample.start_state == "VERIFIED_PREGAME"),
                key=lambda item: item.fetched_at, default=None,
            )
            sample = verified or max(samples, key=lambda item: item.fetched_at, default=None)
            trusted = record.get("Sport", "") in TRUSTED_FLIP_SPORTS
            finalize(
                tab, row_idx, record, sample.price if sample else None,
                start_status=(START_VERIFIED if verified and trusted else (
                    START_UNVERIFIED if sample else START_UNKNOWN
                )),
                quality=(QUALITY_EARLY if verified and trusted else QUALITY_PROVISIONAL),
                sample=sample, headers=headers,
            )
            continue
        sport = record.get("Sport", "")
        scores = scores_by_sport.get(sport)
        games = scores.games if scores and scores.ok else []
        event_id = _resolve_event_id(record, games, commence, roster_cache)
        if event_id and event_id != record.get("Event ID"):
            update_queue_row(tab, row_idx, {"Event ID": event_id}, headers)
            record["Event ID"] = event_id
        game = next((candidate for candidate in games if str(candidate.get("id")) == str(event_id)), None)
        trusted = sport in TRUSTED_FLIP_SPORTS
        live_state = event_live_state(game) if game is not None and scores and scores.ok else None
        if live_state == PREGAME and trusted:
            sample_state = "VERIFIED_PREGAME"
        elif live_state == PREGAME:
            sample_state = "UNVERIFIED_PREGAME"
        elif live_state in {LIVE, COMPLETED}:
            sample_state = "VERIFIED_LIVE" if trusted else "UNVERIFIED_LIVE"
        else:
            sample_state = "UNKNOWN"

        if live_state in {LIVE, COMPLETED}:
            detected_at = now
            samples = samples_from_record(record)
            if trusted:
                sample = select_pre_margin_sample(samples, detected_at)
                quality = quality_for_detected_live(sample, detected_at)
                finalize(tab, row_idx, record, sample.price if sample else None,
                         start_status=START_VERIFIED, quality=quality or QUALITY_PROVISIONAL,
                         start_detected_at=detected_at, sample=sample, headers=headers)
            else:
                sample = max(samples, key=lambda item: item.fetched_at, default=None)
                finalize(tab, row_idx, record, sample.price if sample else None,
                         start_status=START_UNVERIFIED, quality=QUALITY_PROVISIONAL,
                         start_detected_at=detected_at, sample=sample, headers=headers)
            continue

        if now >= commence + timedelta(seconds=CAP_AFTER_COMMENCE_SECONDS) and live_state == PREGAME:
            samples = samples_from_record(record)
            sample = max(samples, key=lambda item: item.fetched_at, default=None)
            finalize(tab, row_idx, record, sample.price if sample else None,
                     start_status=START_VERIFIED if trusted else START_UNVERIFIED,
                     quality=QUALITY_EARLY if trusted else QUALITY_PROVISIONAL,
                     sample=sample, headers=headers)
            continue

        if unknown_finalize_due(now, commence) and sample_state == "UNKNOWN":
            samples = samples_from_record(record)
            sample = max(samples, key=lambda item: item.fetched_at, default=None)
            finalize(tab, row_idx, record, sample.price if sample else None,
                     start_status=START_UNKNOWN, quality=QUALITY_PROVISIONAL,
                     sample=sample, headers=headers)
            continue

        # Keep sampling after scheduled commence only when a fresh, trusted
        # scores response explicitly proves the event is still pregame.
        may_sample = now < commence or sample_state == "VERIFIED_PREGAME"
        if (may_sample and (
                mode != "critical" or critical_sample_allowed(now, commence, sample_state)
        )):
            pinnacle = pinnacle_by_sport.get(sport) or {}
            capture_record(tab, row_idx, record, sample_state, now, headers,
                           pinnacle.get("events") if pinnacle.get("ok") else None,
                           cadence_seconds=cadence_seconds)


def reconcile_bets(tab):
    """Bootstrap manually-added/fallback Bets rows without frequent full-sheet reads."""
    queue_rows = tab.get_all_values()
    queue_headers = queue_rows[0] if queue_rows else QUEUE_HEADERS
    queued_ids = {row[0].strip() for row in queue_rows[1:] if row}
    bets_rows = _get_spreadsheet().worksheet(SHEET_TAB).get_all_values()
    if not bets_rows:
        return 0
    headers = bets_rows[0]
    now = utc_now()
    appended = []
    for row in bets_rows[1:]:
        bet = row_record(headers, row)
        bet_id = bet.get("BetID", "").strip()
        if not bet_id or bet_id in queued_ids or bet.get("ClosingOdds", "").strip():
            continue
        if bet.get("Bet Type") not in AUTOMATED_BET_TYPES:
            continue
        if bet.get("Live Bet", "").strip().upper() == "TRUE":
            continue
        if needs_manual_closing_odds(bet.get("Book", "")):
            continue
        if parse_selection(bet.get("Bet Type", ""), bet.get("Selection", "")) is None:
            continue
        commence = _parse_game_datetime(bet.get("Game Date", ""), bet.get("Game Start Time", ""))
        if commence is None or commence.astimezone(timezone.utc) <= now:
            continue
        created = iso(now)
        values = {
            "BetID": bet_id, "Commence UTC": iso(commence), "Sport": bet.get("Sport", ""),
            "Book": bet.get("Book", ""), "Team 1": bet.get("Team 1", ""),
            "Team 2": bet.get("Team 2", ""), "Selection": bet.get("Selection", ""),
            "Bet Type": bet.get("Bet Type", ""), "OddsTaken": bet.get("OddsTaken", ""),
            "Market Key": bet.get("Market Key", ""), "Event ID": bet.get("Event ID", ""),
            "Status": "PENDING", "Created At": created, "Updated At": created,
        }
        appended.append([values.get(header, "") for header in queue_headers])
    if appended:
        tab.append_rows(appended, value_input_option="RAW")
    return len(appended)


def main():
    if not SHEET_ID:
        raise RuntimeError("SHEET_ID is required")
    require_migration_marker()
    tab = ensure_queue_tab()
    added = reconcile_bets(tab)
    print(f"[closing-capture] worker started; poll={POLL_SECONDS}s; reconciled={added}; "
          f"margin={SAFETY_MARGIN_SECONDS}s slots={SAMPLE_SLOTS} max_age={MAX_SAMPLE_AGE_SECONDS}s "
          f"book_stale={BOOK_STALE_SECONDS}s")
    next_reconcile = time.monotonic() + RECONCILE_SECONDS
    while True:
        try:
            process_queue(tab)
            if time.monotonic() >= next_reconcile:
                added = reconcile_bets(tab)
                print(f"[closing-capture] reconciliation added {added} row(s)")
                next_reconcile = time.monotonic() + RECONCILE_SECONDS
        except Exception as exc:
            print(f"[closing-capture] loop error: {exc}")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
