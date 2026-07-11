"""Shared sheet-date parser (F1 root cause: sheet dates are M/D/YYYY)."""

from datetime import date

from date_utils import parse_sheet_date


def test_parses_both_sheet_formats_to_same_date():
    assert parse_sheet_date("7/10/2026") == date(2026, 7, 10)
    assert parse_sheet_date("2026-07-10") == date(2026, 7, 10)
    assert parse_sheet_date("7/10/2026") == parse_sheet_date("2026-07-10")


def test_single_digit_month_day():
    assert parse_sheet_date("6/4/2026") == date(2026, 6, 4)


def test_whitespace_tolerated():
    assert parse_sheet_date("  6/14/2026  ") == date(2026, 6, 14)


def test_returns_none_on_junk_or_empty():
    for bad in ["", None, "garbage", "2026/07/10", "13/40/2026", "2026-13-40"]:
        assert parse_sheet_date(bad) is None
