from datetime import datetime, timedelta, timezone

from closing_capture_worker import (
    LEGACY_QUEUE_HEADERS,
    QUEUE_HEADERS,
    active_slot,
    latest_sample,
    parse_utc,
    queue_headers_are_compatible,
)


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


def test_queue_schema_preserves_legacy_prefix_and_accepts_extensions():
    assert QUEUE_HEADERS[:len(LEGACY_QUEUE_HEADERS)] == LEGACY_QUEUE_HEADERS
    assert queue_headers_are_compatible(LEGACY_QUEUE_HEADERS)
    assert queue_headers_are_compatible([*QUEUE_HEADERS, "Future Extension"])
    assert not queue_headers_are_compatible(["Changed", *LEGACY_QUEUE_HEADERS[1:]])
