"""Unit tests for sources.scores_live — offline, no API key required."""

from unittest.mock import MagicMock, patch

import sources.scores_live as sl


def _resp(status=200, body=None, headers=None):
    resp = MagicMock()
    resp.status_code = status
    resp.headers = headers or {"x-requests-remaining": "500", "x-requests-used": "3",
                               "x-requests-last": "1"}
    resp.json.return_value = body if body is not None else []
    return resp


# ── event_live_state ──────────────────────────────────────────────────────────
def test_pregame_when_no_live_signal():
    assert sl.event_live_state({"completed": False, "scores": None, "last_update": None}) == sl.PREGAME
    assert sl.event_live_state({"completed": False, "scores": []}) == sl.PREGAME
    assert sl.event_live_state({}) == sl.PREGAME


def test_live_when_scores_or_last_update_present():
    assert sl.event_live_state({"completed": False, "last_update": "2026-07-11T18:15:00Z"}) == sl.LIVE
    assert sl.event_live_state(
        {"completed": False, "scores": [{"name": "A", "score": "1"}]}) == sl.LIVE


def test_completed_takes_precedence_over_scores():
    game = {"completed": True, "scores": [{"name": "A", "score": "3"}],
            "last_update": "2026-07-11T21:00:00Z"}
    assert sl.event_live_state(game) == sl.COMPLETED


# ── classify_coverage ─────────────────────────────────────────────────────────
def test_coverage_unsupported_only_on_404():
    r = sl.ScoresResult("weird_key", status="ERROR", http_status=404)
    assert sl.classify_coverage(r, {"baseball_mlb"}) == sl.COVERAGE_UNSUPPORTED


def test_coverage_transient_for_non_404_errors():
    for http in (401, 422, 429, None):
        r = sl.ScoresResult("baseball_mlb", status="ERROR", http_status=http)
        assert sl.classify_coverage(r, {"baseball_mlb"}) == sl.COVERAGE_TRANSIENT_ERROR


def test_coverage_off_season_when_not_active():
    r = sl.ScoresResult("soccer_epl", status="OK", games=[])
    assert sl.classify_coverage(r, {"baseball_mlb"}) == sl.COVERAGE_OFF_SEASON


def test_coverage_temporarily_empty_vs_supported():
    active = {"baseball_mlb"}
    assert sl.classify_coverage(
        sl.ScoresResult("baseball_mlb", status="OK", games=[]), active
    ) == sl.COVERAGE_TEMPORARILY_EMPTY
    assert sl.classify_coverage(
        sl.ScoresResult("baseball_mlb", status="OK", games=[{"id": "1"}]), active
    ) == sl.COVERAGE_SUPPORTED


def test_coverage_unknown_active_list_maps_empty_to_transient():
    # /sports itself failed → can't distinguish off-season from empty.
    assert sl.classify_coverage(
        sl.ScoresResult("baseball_mlb", status="OK", games=[]), None
    ) == sl.COVERAGE_TRANSIENT_ERROR
    assert sl.classify_coverage(
        sl.ScoresResult("baseball_mlb", status="OK", games=[{"id": "1"}]), None
    ) == sl.COVERAGE_SUPPORTED


# ── CoverageCache ─────────────────────────────────────────────────────────────
def test_coverage_cache_respects_ttl_and_skips_transient():
    cache = sl.CoverageCache(ttl_seconds=100)
    cache.put("baseball_mlb", sl.COVERAGE_SUPPORTED, now_monotonic=1000.0)
    assert cache.get("baseball_mlb", now_monotonic=1050.0) == sl.COVERAGE_SUPPORTED
    assert cache.get("baseball_mlb", now_monotonic=1101.0) is None  # expired

    cache.put("soccer_epl", sl.COVERAGE_TRANSIENT_ERROR, now_monotonic=1000.0)
    assert cache.get("soccer_epl", now_monotonic=1000.0) is None  # never cached


# ── fetch_scores_live ─────────────────────────────────────────────────────────
@patch("sources.scores_live.requests.get")
def test_fetch_ok_parses_games_and_credits(mock_get):
    mock_get.return_value = _resp(200, [{"id": "abc", "commence_time": "2026-07-11T18:00:00Z"}])
    result = sl.fetch_scores_live("baseball_mlb")
    assert result.ok
    assert len(result.games) == 1
    assert result.credits["remaining"] == "500"
    assert result.credits["last"] == "1"
    # No daysFrom is sent — that's what keeps completed history out of the window.
    _args, kwargs = mock_get.call_args
    assert "daysFrom" not in kwargs["params"]


@patch("sources.scores_live.requests.get")
def test_fetch_404_flags_http_status(mock_get):
    mock_get.return_value = _resp(404, {"error_code": "UNKNOWN_SPORT"})
    result = sl.fetch_scores_live("nonsense_key")
    assert not result.ok
    assert result.http_status == 404


@patch("sources.scores_live.requests.get")
def test_fetch_non_list_is_error(mock_get):
    mock_get.return_value = _resp(200, {"unexpected": "object"})
    result = sl.fetch_scores_live("baseball_mlb")
    assert not result.ok
    assert "not a list" in result.error


@patch("sources.scores_live.requests.get", side_effect=sl.requests.RequestException("boom"))
def test_fetch_network_error_is_error_without_http_status(mock_get):
    result = sl.fetch_scores_live("baseball_mlb")
    assert not result.ok
    assert result.http_status is None
