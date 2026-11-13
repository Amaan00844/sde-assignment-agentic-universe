"""
Unit tests for date and decimal parsing.
"""
import pytest
from solution.schema import parse_date, parse_decimal


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2026-11-23", "2026-11-23"),
        ("27/10/2026", "2026-10-27"),
        ("17-12-2026", "2026-12-17"),
        ("29-Dec-2026", "2026-12-29"),
        ("01-dec-2026", "2026-12-01"),
        ("15-OCT-2026", "2026-10-15"),
        ("05/11/2026", "2026-11-05"),  # Day-first: 5th Nov
        ("29-Feb-2024", "2024-02-29"),  # Leap year
    ],
)
def test_valid_dates(raw, expected):
    assert parse_date(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "31/02/2026",    # Feb 31
        "2026-13-01",    # Month 13
        "29-Feb-2026",    # Non-leap year Feb 29
        "soon",
        "TBD",
        "32-10-2026",
        "",
        None,
    ],
)
def test_invalid_dates(raw):
    assert parse_date(raw) is None


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("₹27,554", 27554.0),
        ("6850", 6850.0),
        ("Rs. 31629", 31629.0),
        ("Rs 12000", 12000.0),
        ("INR 25301.4", 25301.4),
        ("INR 15176.5", 15176.5),
        ("4985.75", 4985.75),
        ("  ₹ 1,234.567 ", 1234.57),  # Round to 2 places
        ("0", 0.0),
        ("0.00", 0.0),
    ],
)
def test_valid_decimals(raw, expected):
    assert parse_decimal(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "-150.00",        # Negative
        "twelve thousand",
        "-",
        "N/A",
        "",
        None,
    ],
)
def test_invalid_decimals(raw):
    assert parse_decimal(raw) is None
