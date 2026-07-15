from datetime import datetime, timedelta, timezone

from closing_provenance import (
    ClosingSample, QUALITY_STALE, QUALITY_VERIFIED,
    legacy_row_is_pooled, quality_for_detected_live, select_pre_margin_sample,
)


NOW = datetime(2026, 7, 15, 20, 0, tzinfo=timezone.utc)


def sample(seconds_before, price, *, quote_age=10, state="VERIFIED_PREGAME"):
    fetched = NOW - timedelta(seconds=seconds_before)
    return ClosingSample(price, fetched, fetched - timedelta(seconds=quote_age), state)


def test_newest_inside_margin_falls_back_to_earlier_slot():
    chosen = select_pre_margin_sample([
        sample(30, "-120"), sample(100, "-115"), sample(200, "-110"),
    ], NOW, margin_seconds=90)
    assert chosen.price == "-115"


def test_stale_when_safe_sample_gap_exceeds_max_age():
    chosen = sample(360, "-105")
    assert quality_for_detected_live(chosen, NOW, max_age_seconds=300) == QUALITY_STALE


def test_fresh_safe_sample_is_verified_close():
    chosen = sample(100, "-105")
    assert quality_for_detected_live(chosen, NOW) == QUALITY_VERIFIED


def test_missing_book_timestamp_is_stale():
    chosen = ClosingSample("-105", NOW - timedelta(seconds=100), None, "VERIFIED_PREGAME")
    assert quality_for_detected_live(chosen, NOW) == QUALITY_STALE


def test_legacy_pool_contract():
    assert legacy_row_is_pooled("LEGACY_UNAUDITED", "", "")
    assert legacy_row_is_pooled("LEGACY_UNAUDITED", "", "SAFE")
    assert not legacy_row_is_pooled("LEGACY_UNAUDITED", "", "INDETERMINATE")
    assert not legacy_row_is_pooled("", "", "")
