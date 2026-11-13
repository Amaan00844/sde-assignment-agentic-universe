"""
Unit tests for effective disposition resolution and lead bucket classification.
"""
import json
import pytest
from solution.config import DEFAULT_SCHEMAS_DIR
from solution.db import get_connection, init_database, sync_calls_table
from solution.event_processor import EventProcessor
from solution.lead_processor import LeadProcessor
from solution.schema import SchemaRegistry


@pytest.fixture
def populated_db(tmp_path):
    db_file = tmp_path / "test_ledger.db"
    conn = get_connection(db_file)
    registry = SchemaRegistry(DEFAULT_SCHEMAS_DIR)
    init_database(conn, registry.dispositions)

    # Ingest 4 leads
    lead_proc = LeadProcessor(conn)
    mr_spec = registry.get_workflow("motor_renewal")
    csv_content = (
        "customer_name,phone,policy_number\n"
        "Lead Zero,9876543210,MOT/0\n"       # Will have 0 calls -> yet_to_reach_out
        "Lead Terminal,9876543211,MOT/1\n"   # Call with terminal disposition -> completed
        "Lead Failed,9876543212,MOT/2\n"     # All calls telephony_failed -> in_progress_telephony_failed
        "Lead Dialed,9876543213,MOT/3\n"     # Busy call -> in_progress_dialed
    )
    csv_file = tmp_path / "batch_001.csv"
    csv_file.write_text(csv_content, encoding="utf-8")
    lead_proc.process_lead_file(csv_file, mr_spec, "drop_1")
    conn.commit()

    return conn, registry


def test_effective_disposition_override(tmp_path, populated_db):
    conn, registry = populated_db
    event_proc = EventProcessor(conn, registry.dispositions)

    # Lead 1: call initiated, set disposition to payment_link_sent (not terminal, not success),
    # then supervisor override to renewed (terminal, success).
    events = [
        {"event_id": "e1", "event_type": "call.initiated", "workflow_id": "motor_renewal",
         "call_id": "c1", "occurred_at": "2026-10-01 10:00:00", "phone": "+919876543211"},
        {"event_id": "e2", "event_type": "call.connected", "workflow_id": "motor_renewal",
         "call_id": "c1", "occurred_at": "2026-10-01 10:00:10", "phone": "+919876543211"},
        {"event_id": "e3", "event_type": "disposition.set", "workflow_id": "motor_renewal",
         "call_id": "c1", "occurred_at": "2026-10-01 10:05:00", "phone": "+919876543211",
         "payload": {"disposition_code": "payment_link_sent"}},
        # Override has no phone!
        {"event_id": "e4", "event_type": "disposition.override", "workflow_id": "motor_renewal",
         "call_id": "c1", "occurred_at": "2026-10-01 12:00:00",
         "payload": {"disposition_code": "renewed", "overridden_by": "supervisor_1"}},
    ]
    efile = tmp_path / "events.jsonl"
    efile.write_text("\n".join(json.dumps(e) for e in events), encoding="utf-8")
    event_proc.process_event_file(efile, "drop_1")
    conn.commit()

    # Rebuild calls table
    sync_calls_table(conn)

    # Verify effective disposition of c1 is 'renewed'
    row = conn.execute("SELECT effective_disposition FROM calls WHERE call_id = 'c1';").fetchone()
    assert row["effective_disposition"] == "renewed"

    # Verify lead bucket using SQL
    lead_row = conn.execute("""
        WITH linked_calls AS (
            SELECT c.workflow_id, c.call_id, c.phone_hash, c.is_connected,
                   c.has_telephony_failed, c.effective_disposition, c.initiated_at_ist
            FROM calls c
            JOIN leads l ON c.workflow_id = l.workflow_id AND c.phone_hash = l.phone_hash
        ),
        lead_call_stats AS (
            SELECT
                lc.workflow_id, lc.phone_hash,
                COUNT(lc.call_id) AS total_calls,
                MAX(COALESCE(dc.is_terminal, 0)) AS any_terminal,
                MIN(lc.has_telephony_failed) AS all_telephony_failed,
                MAX(COALESCE(dc.is_success, 0)) AS any_success
            FROM linked_calls lc
            LEFT JOIN disposition_catalogue dc ON lc.workflow_id = dc.workflow_id AND lc.effective_disposition = dc.code
            GROUP BY lc.workflow_id, lc.phone_hash
        ),
        ranked_disps AS (
            SELECT lc.workflow_id, lc.phone_hash, lc.effective_disposition,
                   ROW_NUMBER() OVER (PARTITION BY lc.workflow_id, lc.phone_hash ORDER BY lc.initiated_at_ist DESC) as rn
            FROM linked_calls lc WHERE lc.effective_disposition IS NOT NULL
        )
        SELECT
            l.external_ref,
            CASE
                WHEN COALESCE(lcs.total_calls, 0) = 0 THEN 'yet_to_reach_out'
                WHEN lcs.any_terminal = 1 THEN 'completed'
                WHEN lcs.all_telephony_failed = 1 THEN 'in_progress_telephony_failed'
                ELSE 'in_progress_dialed'
            END AS bucket,
            COALESCE(rd.effective_disposition, '') AS current_disposition,
            CASE WHEN COALESCE(lcs.any_success, 0) = 1 THEN 'true' ELSE 'false' END AS ever_success
        FROM leads l
        LEFT JOIN lead_call_stats lcs ON l.workflow_id = lcs.workflow_id AND l.phone_hash = lcs.phone_hash
        LEFT JOIN ranked_disps rd ON l.workflow_id = rd.workflow_id AND l.phone_hash = rd.phone_hash AND rd.rn = 1
        WHERE l.external_ref = 'MOT/1';
    """).fetchone()

    assert lead_row["bucket"] == "completed"
    assert lead_row["current_disposition"] == "renewed"
    assert lead_row["ever_success"] == "true"


def test_bucket_classification_rules(tmp_path, populated_db):
    conn, registry = populated_db
    event_proc = EventProcessor(conn, registry.dispositions)

    events = [
        # MOT/2 Call 1:
        {"event_id": "e10", "event_type": "call.initiated", "workflow_id": "motor_renewal",
         "call_id": "c_fail_1", "occurred_at": "2026-10-02 09:00:00", "phone": "+919876543212"},
        {"event_id": "e11", "event_type": "call.telephony_failed", "workflow_id": "motor_renewal",
         "call_id": "c_fail_1", "occurred_at": "2026-10-02 09:00:05", "phone": "+919876543212"},
        # MOT/2 Call 2:
        {"event_id": "e12", "event_type": "call.initiated", "workflow_id": "motor_renewal",
         "call_id": "c_fail_2", "occurred_at": "2026-10-02 11:00:00", "phone": "+919876543212"},
        {"event_id": "e13", "event_type": "call.telephony_failed", "workflow_id": "motor_renewal",
         "call_id": "c_fail_2", "occurred_at": "2026-10-02 11:00:05", "phone": "+919876543212"},

        # MOT/3 Call:
        {"event_id": "e20", "event_type": "call.initiated", "workflow_id": "motor_renewal",
         "call_id": "c_dialed", "occurred_at": "2026-10-02 14:00:00", "phone": "+919876543213"},
        {"event_id": "e21", "event_type": "call.ended", "workflow_id": "motor_renewal",
         "call_id": "c_dialed", "occurred_at": "2026-10-02 14:00:30", "phone": "+919876543213",
         "payload": {"duration_sec": 0, "end_reason": "busy"}},
    ]
    efile = tmp_path / "events.jsonl"
    efile.write_text("\n".join(json.dumps(e) for e in events), encoding="utf-8")
    event_proc.process_event_file(efile, "drop_1")
    conn.commit()

    # Rebuild calls table
    sync_calls_table(conn)

    # Use the report generator to verify buckets through full lead_report generation
    from solution.reporting import ReportGenerator
    import csv
    report_dir = tmp_path / "reports"
    gen = ReportGenerator(conn)
    gen.generate_all(report_dir)

    with open(report_dir / "lead_report.csv", "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = {r["external_ref"]: r for r in reader}

    assert rows["MOT/0"]["bucket"] == "yet_to_reach_out"
    assert rows["MOT/0"]["total_calls"] == "0"

    assert rows["MOT/2"]["bucket"] == "in_progress_telephony_failed"
    assert rows["MOT/2"]["total_calls"] == "2"
    assert rows["MOT/2"]["connected_calls"] == "0"

    assert rows["MOT/3"]["bucket"] == "in_progress_dialed"
    assert rows["MOT/3"]["total_calls"] == "1"
    assert rows["MOT/3"]["connected_calls"] == "0"
