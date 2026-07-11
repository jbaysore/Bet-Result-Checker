"""Unit tests for sheets_quota retry helper."""

from unittest.mock import MagicMock

import gspread
import pytest

import sheets_quota


def _make_api_error(message: str) -> gspread.exceptions.APIError:
    """Build a gspread APIError without needing a real HTTP response."""
    resp = MagicMock()
    resp.json.return_value = {"error": {"message": message, "code": 429}}
    resp.text = message
    try:
        return gspread.exceptions.APIError(resp)
    except TypeError:
        # Older/newer gspread variants differ; fall back to a subclass.
        class _Err(gspread.exceptions.APIError):
            def __init__(self):
                Exception.__init__(self, message)

        return _Err()


def test_call_with_sheets_retry_succeeds_first_try():
    fn = MagicMock(return_value="ok")
    assert sheets_quota.call_with_sheets_retry("test", fn, 1, x=2) == "ok"
    fn.assert_called_once_with(1, x=2)


def test_call_with_sheets_retry_retries_on_429(monkeypatch):
    sleeps = []
    monkeypatch.setattr(sheets_quota.time, "sleep", lambda s: sleeps.append(s))

    err = _make_api_error("[429]: Quota exceeded for quota metric 'Read requests'")
    fn = MagicMock(side_effect=[err, "recovered"])
    assert sheets_quota.call_with_sheets_retry(
        "test", fn, max_attempts=3, base_sleep_sec=1
    ) == "recovered"
    assert fn.call_count == 2
    assert sleeps == [1]


def test_call_with_sheets_retry_raises_non_quota():
    err = _make_api_error("[500]: Internal error")
    fn = MagicMock(side_effect=err)
    with pytest.raises(gspread.exceptions.APIError):
        sheets_quota.call_with_sheets_retry("test", fn, max_attempts=3, base_sleep_sec=1)
    assert fn.call_count == 1
