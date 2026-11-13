"""
Unit tests for orphan event resolution across drops (events arriving before leads).
"""
import json
import pytest
from solution.config import DEFAULT_SCHEMAS_DIR
from solution.db import get_connection, get_orphan_events_count, init_database, sync_calls_table
from solution.event_processor import EventProcessor
from solution.lead_processor import LeadProcessor
from solution.schema import SchemaRegistry


def test_orphan_resolution_when_lead_arrives_later(tmp_path):
    db_file = tmp_path / "test_ledger.db"
    conn = get_connection(db_file)
    registry = SchemaRegistry(DEFAULT_SCHEMAS_DIR)
    init_database(conn, registry.dispositions)

    event_proc = EventProcessor(conn, registry.dispositions)
    lead_proc = LeadProcessor(conn)
    mr_spec = registry.get_workflow("motor_renewal")

    # Step 1: Events arrive in Drop 1 for phone +919876543299 (no lead exists yet)
    events = [
        {"event_id": "e1", "event_type": "call.initiated", "workflow_id": "motor_renewal",
         "call_id": "c100", "occurred_at": "2026-10-01 10:00:00", "phone": "+919876543299"},
        {"event_id": "e2", "event_type": "call.connected", "workflow_id": "motor_renewal",
         "call_id": "c100", "occurred_at": "2026-10-01 10:00:10", "phone": "+919876543299"},
        {"event_id": "e3", "event_type": "disposition.set", "workflow_id": "motor_renewal",
         "call_id": "c100", "occurred_at": "2026-10-01 10:05:00", "phone": "+919876543299",
         "payload": {"disposition_code": "renewed"}},
    ]
    efile1 = tmp_path / "events_drop1.jsonl"
    efile1.write_text("\n".join(json.dumps(e) for e in events), encoding="utf-8")
    event_proc.process_event_file(efile1, "drop_1")
    conn.commit()

    # Rebuild calls table
    sync_calls_table(conn)

    # In Drop 1: All 3 events are orphans!
    assert get_orphan_events_count(conn) == 3

    # Step 2: Lead arrives in Drop 2!
    csv_content = (
        "customer_name,phone,policy_number\n"
        "Late Lead,9876543299,MOT/99\n"
    )
    lfile2 = tmp_path / "batch_003.csv"
    lfile2.write_text(csv_content, encoding="utf-8")
    lead_proc.process_lead_file(lfile2, mr_spec, "drop_2")
    conn.commit()

    # Rebuild calls table after new lead
    sync_calls_table(conn)

    # Now: Events are no longer orphans!
    assert get_orphan_events_count(conn) == 0

    # And reports reflect the call - use the report generator
    from solution.reporting import ReportGenerator
    import csv
    report_dir = tmp_path / "reports"
    gen = ReportGenerator(conn)
    gen.generate_all(report_dir)

    with open(report_dir / "lead_report.csv", "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = {r["external_ref"]: r for r in reader}

    assert rows["MOT/99"]["bucket"] == "completed"
    assert rows["MOT/99"]["total_calls"] == "1"
    assert rows["MOT/99"]["connected_calls"] == "1"
    assert rows["MOT/99"]["current_disposition"] == "renewed"
    assert rows["MOT/99"]["ever_success"] == "true"
