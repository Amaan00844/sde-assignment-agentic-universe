"""
Unit tests for lead batch processing, rejection precedence, duplicate merging, and PII protection.
"""
import json
import sqlite3
import pytest
from pathlib import Path
from solution.config import DEFAULT_SCHEMAS_DIR
from solution.crypto import decrypt_pii, hash_phone
from solution.db import get_connection, init_database
from solution.lead_processor import LeadProcessor
from solution.schema import SchemaRegistry


@pytest.fixture
def test_db(tmp_path):
    db_file = tmp_path / "test_ledger.db"
    conn = get_connection(db_file)
    registry = SchemaRegistry(DEFAULT_SCHEMAS_DIR)
    init_database(conn, registry.dispositions)
    return conn, registry


def test_blank_rows_and_row_numbering(tmp_path, test_db):
    conn, registry = test_db
    processor = LeadProcessor(conn)
    wf_spec = registry.get_workflow("motor_renewal")

    csv_content = (
        "customer_name,phone,policy_number,policy_expiry,premium_amount,preferred_language\n"
        "\n"  # blank row
        "   ,   ,   ,   ,   ,   \n"  # whitespace blank row
        "Aarav Sharma,9876543210,MOT/1,2026-11-23,10000,en\n"  # row 1: valid
        "\n"  # blank row
        "Vivaan Gupta,12345,MOT/2,2026-11-23,10000,en\n"  # row 2: invalid phone
    )
    csv_file = tmp_path / "batch_001.csv"
    csv_file.write_text(csv_content, encoding="utf-8")

    res = processor.process_lead_file(csv_file, wf_spec, "drop_1")
    conn.commit()

    assert res["rows_total"] == 2
    assert res["rows_rejected"] == 1

    rejections = conn.execute("SELECT row_number, reason FROM rejections;").fetchall()
    assert len(rejections) == 1
    # The rejected row is the 2nd non-blank row
    assert rejections[0]["row_number"] == 2
    assert rejections[0]["reason"] == "invalid_phone"


def test_rejection_precedence_in_schema_order(tmp_path, test_db):
    conn, registry = test_db
    processor = LeadProcessor(conn)
    wf_spec = registry.get_workflow("motor_renewal")

    # Row with both missing customer_name AND invalid phone:
    # customer_name is checked first in schema order!
    csv_content = (
        "customer_name,phone,policy_number\n"
        ",5123456789,MOT/1\n"
    )
    csv_file = tmp_path / "batch_001.csv"
    csv_file.write_text(csv_content, encoding="utf-8")

    processor.process_lead_file(csv_file, wf_spec, "drop_1")
    conn.commit()

    rejections = conn.execute("SELECT reason FROM rejections;").fetchall()
    assert len(rejections) == 1
    assert rejections[0]["reason"] == "missing_required:customer_name"


def test_field_warnings_and_defaults(tmp_path, test_db):
    conn, registry = test_db
    processor = LeadProcessor(conn)
    wf_spec = registry.get_workflow("motor_renewal")

    # Valid row with invalid date (31/02) and invalid decimal ('twelve thousand')
    csv_content = (
        "customer_name,phone,policy_number,policy_expiry,premium_amount,preferred_language\n"
        "Aarav Sharma,9876543210,MOT/1,31/02/2026,twelve thousand,\n"
    )
    csv_file = tmp_path / "batch_001.csv"
    csv_file.write_text(csv_content, encoding="utf-8")

    res = processor.process_lead_file(csv_file, wf_spec, "drop_1")
    conn.commit()

    assert res["rows_total"] == 1
    assert res["rows_rejected"] == 0
    # Two invalid optional fields -> 2 warnings
    assert res["warnings"] == 2

    # Check lead attributes in database: invalid fields became None, default applied for language
    row = conn.execute("SELECT attributes_json FROM leads;").fetchone()
    attrs = json.loads(row["attributes_json"])
    assert attrs["policy_expiry"] is None
    assert attrs["premium_amount"] is None
    assert attrs["preferred_language"] == "en"  # Default applied!


def test_duplicate_lead_merging_and_batch_id_lock(tmp_path, test_db):
    conn, registry = test_db
    processor = LeadProcessor(conn)
    wf_spec = registry.get_workflow("motor_renewal")

    # First batch: creates lead in batch_001
    batch1 = tmp_path / "batch_001.csv"
    batch1.write_text(
        "customer_name,phone,policy_number,policy_expiry,premium_amount,preferred_language\n"
        "Aarav Sharma,9876543210,MOT/1,2026-11-20,10000,hi\n",
        encoding="utf-8",
    )
    res1 = processor.process_lead_file(batch1, wf_spec, "drop_1")
    conn.commit()
    assert res1["duplicate_merged"] == 0

    # Second batch: same phone in batch_002 with updated premium, null date, null language
    batch2 = tmp_path / "batch_002.csv"
    batch2.write_text(
        "customer_name,phone,policy_number,policy_expiry,premium_amount,preferred_language\n"
        "Aarav Sharma,9876543210,MOT/1,,25000,\n",
        encoding="utf-8",
    )
    res2 = processor.process_lead_file(batch2, wf_spec, "drop_1")
    conn.commit()
    assert res2["duplicate_merged"] == 1

    lead = conn.execute("SELECT batch_id, attributes_json FROM leads;").fetchone()
    # Permanent batch_id remains batch_001
    assert lead["batch_id"] == "batch_001"

    attrs = json.loads(lead["attributes_json"])
    # Premium overwritten (25000.0)
    assert attrs["premium_amount"] == 25000.0
    # Date preserved ("2026-11-20") because new row was null
    assert attrs["policy_expiry"] == "2026-11-20"
    # Language preserved ("hi")
    assert attrs["preferred_language"] == "hi"


def test_pii_protection(tmp_path, test_db):
    conn, registry = test_db
    processor = LeadProcessor(conn)
    wf_spec = registry.get_workflow("motor_renewal")

    batch = tmp_path / "batch_001.csv"
    batch.write_text(
        "customer_name,phone,policy_number\n"
        "Secret User,9876543210,MOT/100\n",
        encoding="utf-8",
    )
    processor.process_lead_file(batch, wf_spec, "drop_1")
    conn.commit()

    lead = conn.execute("SELECT * FROM leads;").fetchone()
    # Plaintext phone is NEVER stored
    assert "+919876543210" not in dict(lead).values()
    assert "Secret User" not in dict(lead).values()

    # Phone is masked
    assert lead["phone_masked"] == "+91******3210"
    # Phone hash matches
    assert lead["phone_hash"] == hash_phone("+919876543210")
    # Encrypted fields can be decrypted back
    assert decrypt_pii(lead["customer_name_encrypted"]) == "Secret User"
    assert decrypt_pii(lead["phone_encrypted"]) == "+919876543210"
