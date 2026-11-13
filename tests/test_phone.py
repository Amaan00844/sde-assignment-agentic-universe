"""
Unit tests for phone number normalisation, validation, masking, and hashing.
"""
import pytest
from solution.crypto import hash_phone, mask_phone
from solution.schema import parse_phone


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("+91 79328 96340", "+917932896340"),
        ("06832582386", "+916832582386"),
        ("91-81518-64492", "+918151864492"),
        ("8880395512", "+918880395512"),
        ("(+91) 8196106462", "+918196106462"),
        ("9932302346", "+919932302346"),
        ("+916463745499", "+916463745499"),
        ("7136363179", "+917136363179"),
        ("6271122182", "+916271122182"),
        ("  +91 98765 43210  ", "+919876543210"),
    ],
)
def test_valid_phone_normalization(raw, expected):
    assert parse_phone(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "+1 415 555 0134",      # US number
        "5123456789",          # Starts with 5 (invalid mobile prefix)
        "1234567890",          # Starts with 1
        "98765",               # Too short (5 digits)
        "9876543210123",       # Too long (13 digits)
        "022-2456-7890",       # Landline
        "N/A",                 # Alphabetic
        "",                    # Empty
        None,                  # None
        "91512345678",         # 11 digits starting with 91 (not 12)
        "05123456789",         # 11 digits starting with 0, but 10-digit starts with 5
    ],
)
def test_invalid_phone_rejected(raw):
    assert parse_phone(raw) is None


def test_phone_masking():
    assert mask_phone("+919876543210") == "+91******3210"
    assert mask_phone("+916463745499") == "+91******5499"


def test_phone_hashing():
    p1 = "+919876543210"
    p2 = "+919876543210"
    p3 = "+919876543211"

    # Deterministic
    assert hash_phone(p1) == hash_phone(p2)
    # Distinct
    assert hash_phone(p1) != hash_phone(p3)
    # Hex string 64 chars (SHA-256)
    assert len(hash_phone(p1)) == 64
