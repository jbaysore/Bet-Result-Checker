"""Unit tests for sheets_writer row-read helpers (no live Sheets API)."""

import pytest

from sheets_writer import _bet_id_matches, _cell_at, _promo_id_matches


@pytest.mark.parametrize(
    "row,col,expected",
    [
        pytest.param(["a", "b", "c"], 1, "a", id="first_col"),
        pytest.param(["a", "b", "c"], 3, "c", id="last_col"),
        pytest.param(["a", "b"], 5, "", id="past_end"),
        pytest.param(["  spaced  "], 1, "spaced", id="strips_whitespace"),
        pytest.param([], 1, "", id="empty_row"),
    ],
)
def test_cell_at(row, col, expected):
    assert _cell_at(row, col) == expected


@pytest.mark.parametrize(
    "row,col,bet_id,expected",
    [
        pytest.param(["84", "WIN"], 1, "84", True, id="match_string"),
        pytest.param(["84", "WIN"], 1, 84, True, id="match_int_expected"),
        pytest.param(["85", "WIN"], 1, "84", False, id="mismatch"),
    ],
)
def test_bet_id_matches(row, col, bet_id, expected):
    assert _bet_id_matches(row, col, bet_id) is expected


@pytest.mark.parametrize(
    "row,col,promo_id,expected",
    [
        pytest.param([" 18 ", "Pending"], 1, "18", True, id="match_stripped"),
        pytest.param(["19", "Pending"], 1, "18", False, id="mismatch"),
    ],
)
def test_promo_id_matches(row, col, promo_id, expected):
    assert _promo_id_matches(row, col, promo_id) is expected
