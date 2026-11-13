"""
Unit tests for event ingestion: malformed check, deduplication, timestamp normalization,
and unknown disposition filtering.
"""
import json
import pytest
from solution.config import DEFAULT_SCHEMAS_DIR
from solution.db import get_connection, init_database
from solution.event_processor import EventProcessor
from solution.schema import SchemaRegistry, parse_timestamp_ist


@pytest.fixture
def test_db(tmp_path):
    db_file = tmp_path / "test_ledger.db"
    conn = get_connection(db_file)
    registry = SchemaRegistry(DEFAULT_SCHEMAS_DIR)
    init_database(conn, registry.dispositions)
    return conn, registry


def test_timestamp_parsing_to_ist():
    # UTC 'Z'
    dt1 = parse_timestamp_ist("2026-10-05T13:20:21Z")
    assert dt1.strftime("%Y-%m-%d %H:%M:%S") == "2026-10-05 18:50:21"
    assert dt1.tzinfo is not None

    # Offset '+05:30'
    dt2 = parse_timestamp_ist("2026-10-01T10:40:19+05:30")
    assert dt2.strftime("%Y-%m-%d %H:%M:%S") == "2026-10-01 10:40:19"

    # Naive means IST per spec
    dt3 = parse_timestamp_ist("2026-10-01 09:15:55")
    assert dt3.strftime("%Y-%m-%d %H:%M:%S") == "2026-10-01 09:15:55"
    assert dt3.tzinfo is not None


def test_event_processing_rules(tmp_path, test_db):
    conn, registry = test_db
    processor = EventProcessor(conn, registry.dispositions)

    events_content = (
        # 1. Valid event
        json.dumps({
            "event_id": "evt_001",
            "event_type": "call.initiated",
            "workflow_id": "motor_renewal",
            "call_id": "call_001",
            "occurred_at": "2026-10-01T09:00:00Z",
            "phone": "+919876543210",
        }) + "\n"
        # 2. Malformed JSON
        + "NOT_JSON_DATA\n"
        # 3. Missing event_id
        + json.dumps({
            "event_type": "call.initiated",
            "workflow_id": "motor_renewal",
            "call_id": "call_002",
            "occurred_at": "2026-10-01 10:00:00",
        }) + "\n"
        # 4. Duplicate event_id (evt_001 repeated)
        + json.dumps({
            "event_id": "evt_001",
            "event_type": "call.initiated",
            "workflow_id": "motor_renewal",
            "call_id": "call_001",
            "occurred_at": "2026-10-01T09:00:00Z",
            "phone": "+919876543210",
        }) + "\n"
        # 5. Unknown disposition event
        + json.dumps({
            "event_id": "evt_002",
            "event_type": "disposition.set",
            "workflow_id": "motor_renewal",
            "call_id": "call_001",
            "occurred_at": "2026-10-01 10:30:00",
            "phone": "+919876543210",
            "payload": {"disposition_code": "NON_EXISTENT_CODE"},
        }) + "\n"
        # 6. Valid disposition event
        + json.dumps({
            "event_id": "evt_003",
            "event_type": "disposition.set",
            "workflow_id": "motor_renewal",
            "call_id": "call_001",
            "occurred_at": "2026-10-01 10:35:00",
            "phone": "+919876543210",
            "payload": {"disposition_code": "renewed"},
        }) + "\n"
    )

    event_file = tmp_path / "events_0001.jsonl"
    event_file.write_text(events_content, encoding="utf-8")

    res = processor.process_event_file(event_file, "drop_1")
    conn.commit()

    assert res["lines_total"] == 6
    assert res["malformed"] == 2  # Lines 2 and 3
    assert res["duplicates"] == 1  # Line 4
    assert res["unknown_disp"] == 1  # Line 5
    assert res["valid_inserted"] == 2  # Lines 1 and 6

    # Verify rows in append-only interaction ledger
    rows = conn.execute("SELECT event_id FROM interaction_events ORDER BY event_id;").fetchall()
    assert [r[0] for r in rows] == ["evt_001", "evt_003"]
