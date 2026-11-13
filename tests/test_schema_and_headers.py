"""
Unit tests for schema loading, header matching, and enum validation.
"""
import pytest
from solution.config import DEFAULT_SCHEMAS_DIR
from solution.schema import SchemaRegistry, normalize_header, parse_enum, parse_string


def test_header_normalization():
    assert normalize_header("Insured Name") == "insured name"
    assert normalize_header("mobile_no") == "mobile no"
    assert normalize_header("  Policy___Number  ") == "policy number"
    assert normalize_header("Order_ID") == "order id"
    assert normalize_header("Language") == "language"


def test_enum_validation():
    allowed = ["en", "hi", "ta", "kn", "te", "mr"]
    assert parse_enum("EN", allowed) == "en"
    assert parse_enum("  Hi  ", allowed) == "hi"
    assert parse_enum("mr", allowed) == "mr"
    assert parse_enum("Hindi", allowed) is None
    assert parse_enum("french", allowed) is None
    assert parse_enum("", allowed) is None


def test_string_validation():
    assert parse_string("  Aarav Sharma  ") == "Aarav Sharma"
    assert parse_string("   ") is None
    assert parse_string("") is None
    assert parse_string(None) is None


def test_schema_registry_loading():
    registry = SchemaRegistry(DEFAULT_SCHEMAS_DIR)
    mr = registry.get_workflow("motor_renewal")
    assert mr is not None
    assert mr.reference_field_name == "policy_number"
    assert len(mr.fields) == 6

    pf = registry.get_workflow("policy_feedback")
    assert pf is not None
    assert pf.reference_field_name == "order_id"

    # Dispositions
    assert registry.dispositions.is_known("motor_renewal", "renewed")
    assert registry.dispositions.get("motor_renewal", "renewed").is_terminal is True
    assert registry.dispositions.get("motor_renewal", "renewed").is_success is True

    assert registry.dispositions.is_known("policy_feedback", "feedback_captured")
    assert not registry.dispositions.is_known("motor_renewal", "RENEWD")
