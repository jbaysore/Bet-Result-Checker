from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import closing_capture_worker as worker


NOW = datetime(2026, 7, 10, 18, 0, tzinfo=timezone.utc)


def test_active_slot_boundaries():
    assert worker.active_slot(NOW, NOW + timedelta(minutes=10)) == "T-10"
    assert worker.active_slot(NOW, NOW + timedelta(minutes=5)) == "T-5"
    assert worker.active_slot(NOW, NOW + timedelta(minutes=1)) == "T-1"
    assert worker.active_slot(NOW, NOW + timedelta(minutes=11)) is None
    assert worker.active_slot(NOW, NOW) is None


def test_latest_sample_prefers_most_recent_success():
    assert worker.latest_sample({"T-10 Price": "+120", "T-5 Price": "-105", "T-1 Price": ""}) == "-105"
    assert worker.latest_sample({"T-10 Price": "+120", "T-5 Price": "-105", "T-1 Price": "-110"}) == "-110"
    assert worker.latest_sample({}) is None


def test_parse_utc_accepts_z_and_rejects_invalid():
    assert worker.parse_utc("2026-07-10T18:00:00Z") == NOW
    assert worker.parse_utc("not-a-date") is None


def test_queue_schema_preserves_legacy_prefix_and_accepts_extensions():
    assert worker.QUEUE_HEADERS[:len(worker.LEGACY_QUEUE_HEADERS)] == worker.LEGACY_QUEUE_HEADERS
    assert worker.queue_headers_are_compatible(worker.LEGACY_QUEUE_HEADERS)
    assert worker.queue_headers_are_compatible([*worker.QUEUE_HEADERS, "Future Extension"])
    assert not worker.queue_headers_are_compatible(["Changed", *worker.LEGACY_QUEUE_HEADERS[1:]])


class FakeTab:
    def __init__(self, record):
        self.rows = [worker.QUEUE_HEADERS, [record.get(header, "") for header in worker.QUEUE_HEADERS]]

    def get_all_values(self):
        return self.rows


def queue_record(commence):
    return {
        "BetID": "42", "Commence UTC": worker.iso(commence),
        "Sport": "baseball_mlb", "Book": "fanduel",
        "Team 1": "Cubs", "Team 2": "Mets", "Selection": "Cubs",
        "Bet Type": "Moneyline", "OddsTaken": "-110", "Market Key": "h2h",
        "Status": "PENDING",
    }


def test_far_future_rows_do_not_poll_or_sample(monkeypatch):
    now = datetime(2026, 7, 15, 12, tzinfo=timezone.utc)
    tab = FakeTab(queue_record(now + timedelta(days=1)))
    score_calls = []
    monkeypatch.setattr(worker, "fetch_scores_live", lambda sport: score_calls.append(sport))
    monkeypatch.setattr(worker, "fetch_pinnacle_featured", lambda sport: (_ for _ in ()).throw(AssertionError()))
    monkeypatch.setattr(worker, "capture_record", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError()))
    worker.process_queue(tab, now=now)
    assert score_calls == []


def test_unknown_scores_wait_for_grace_before_fallback(monkeypatch):
    commence = datetime(2026, 7, 15, 12, tzinfo=timezone.utc)
    tab = FakeTab(queue_record(commence))
    scores = SimpleNamespace(ok=False, games=[], credits={"last": 1, "used": 1, "remaining": 99})
    monkeypatch.setattr(worker, "fetch_scores_live", lambda sport: scores)
    monkeypatch.setattr(worker, "fetch_pinnacle_featured", lambda sport: {"ok": False, "credits": {"last": 0}})
    monkeypatch.setattr(worker, "_resolve_event_id", lambda *args: None)
    finalized = []
    monkeypatch.setattr(worker, "finalize", lambda *args, **kwargs: finalized.append(kwargs) or "FALLBACK")
    monkeypatch.setattr(worker, "capture_record", lambda *args, **kwargs: None)

    worker.process_queue(tab, now=commence + timedelta(seconds=1))
    assert finalized == []
    worker.process_queue(tab, now=commence + timedelta(seconds=worker.UNKNOWN_GRACE_SECONDS + 1))
    assert finalized[-1]["start_status"] == worker.START_UNKNOWN
    assert finalized[-1]["quality"] == worker.QUALITY_PROVISIONAL


def test_event_id_backfill_defers_doubleheader(monkeypatch):
    commence = datetime(2026, 7, 15, 12, tzinfo=timezone.utc)
    games = [
        {"id": "a", "home_team": "Cubs", "away_team": "Mets"},
        {"id": "b", "home_team": "Cubs", "away_team": "Mets"},
    ]
    record = queue_record(commence)
    monkeypatch.setattr(worker, "_fetch_live_events", lambda sport: games)
    assert worker._resolve_event_id(record, games, commence, {}) is None


def test_capture_budget_uses_real_market_cascade_cost(monkeypatch):
    commence = datetime(2026, 7, 15, 12, tzinfo=timezone.utc)
    record = queue_record(commence)
    tab = FakeTab(record)
    totals = iter([10, 12])
    monkeypatch.setattr(worker, "live_credit_total", lambda: next(totals))
    monkeypatch.setattr(worker, "fetch_live_closing_odds", lambda bet: {
        "closing_odds": "-110", "fetched_at": worker.iso(commence),
        "book_last_update": worker.iso(commence), "event_id": "event-1",
    })
    monkeypatch.setattr(worker, "update_queue_row", lambda *args, **kwargs: None)
    monkeypatch.setattr(worker, "_budget_day", commence.date().isoformat())
    monkeypatch.setattr(worker, "_estimated_daily_credits", 0)
    worker.capture_record(
        tab, 2, record, "VERIFIED_PREGAME", commence, worker.QUEUE_HEADERS,
    )
    assert worker._estimated_daily_credits == 2
