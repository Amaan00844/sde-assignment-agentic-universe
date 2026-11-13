"""
End-to-end and idempotency integration tests.
"""
import csv
import json
import pytest
from pathlib import Path
from solution.cli import generate_reports, ingest_drop
from solution.config import DEFAULT_SCHEMAS_DIR


def test_end_to_end_pipeline_and_idempotency(tmp_path):
    db_file = tmp_path / "ledger.db"
    reports_dir = tmp_path / "reports"

    # Assume data directory is available from generator
    data_dir = Path("data")
    if not (data_dir / "drop_1").exists():
        pytest.skip("Test data not generated in data/")

    # 1. Ingest drop_1
    ingest_drop(data_dir / "drop_1", db_path=db_file, schemas_dir=DEFAULT_SCHEMAS_DIR)

    # 2. Re-ingest drop_1 (must be idempotent)
    ingest_drop(data_dir / "drop_1", db_path=db_file, schemas_dir=DEFAULT_SCHEMAS_DIR)

    # 3. Ingest drop_2
    ingest_drop(data_dir / "drop_2", db_path=db_file, schemas_dir=DEFAULT_SCHEMAS_DIR)

    # 4. Generate reports
    generate_reports(reports_dir, db_path=db_file, schemas_dir=DEFAULT_SCHEMAS_DIR)

    # 5. Check all 5 files exist
    assert (reports_dir / "batch_summary.csv").exists()
    assert (reports_dir / "daily_overview.csv").exists()
    assert (reports_dir / "lead_report.csv").exists()
    assert (reports_dir / "rejections.csv").exists()
    assert (reports_dir / "data_quality.json").exists()

    # Verify data_quality.json
    with open(reports_dir / "data_quality.json", "r", encoding="utf-8") as f:
        dq = json.load(f)
    assert dq["lead_rows_total"] > 0
    assert dq["events_total"] > 0
    assert dq["lead_rows_total"] == dq["lead_rows_rejected"] + dq["duplicate_lead_rows_merged"] + 1892

    # Verify daily_overview formatting: connectivity_rate has 4 decimal places
    with open(reports_dir / "daily_overview.csv", "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            parts = row["connectivity_rate"].split(".")
            assert len(parts) == 2
            assert len(parts[1]) == 4
