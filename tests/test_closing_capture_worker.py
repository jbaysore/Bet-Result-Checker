from datetime import datetime, timedelta, timezone

from closing_capture_worker import active_slot, latest_sample, parse_utc


NOW = datetime(2026, 7, 10, 18, 0, tzinfo=timezone.utc)


def test_active_slot_boundaries():
    assert active_slot(NOW, NOW + timedelta(minutes=10)) == "T-10"
    assert active_slot(NOW, NOW + timedelta(minutes=5)) == "T-5"
    assert active_slot(NOW, NOW + timedelta(minutes=1)) == "T-1"
    assert active_slot(NOW, NOW + timedelta(minutes=11)) is None
    assert active_slot(NOW, NOW) is None


def test_latest_sample_prefers_most_recent_success():
    assert latest_sample({"T-10 Price": "+120", "T-5 Price": "-105", "T-1 Price": ""}) == "-105"
    assert latest_sample({"T-10 Price": "+120", "T-5 Price": "-105", "T-1 Price": "-110"}) == "-110"
    assert latest_sample({}) is None


def test_parse_utc_accepts_z_and_rejects_invalid():
    assert parse_utc("2026-07-10T18:00:00Z") == NOW
    assert parse_utc("not-a-date") is None
