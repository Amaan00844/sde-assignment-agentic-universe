"""
Command-line interface for Assignment A data pipeline.
Supports:
  python run.py ingest <drop_path>
  python run.py report --out <output_dir>
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from solution.config import DEFAULT_DB_PATH, DEFAULT_SCHEMAS_DIR
from solution.db import get_connection, init_database, sync_calls_table
from solution.event_processor import EventProcessor
from solution.lead_processor import LeadProcessor
from solution.reporting import ReportGenerator
from solution.schema import SchemaRegistry


def ingest_drop(
    drop_dir: Path | str,
    db_path: Path | str = DEFAULT_DB_PATH,
    schemas_dir: Path | str = DEFAULT_SCHEMAS_DIR,
) -> None:
    drop_path = Path(drop_dir).resolve()
    if not drop_path.exists():
        print(f"Error: Drop directory '{drop_path}' does not exist.", file=sys.stderr)
        sys.exit(1)

    drop_name = drop_path.name
    registry = SchemaRegistry(schemas_dir)
    conn = get_connection(db_path)
    init_database(conn, registry.dispositions)

    lead_proc = LeadProcessor(conn)
    event_proc = EventProcessor(conn, registry.dispositions)

    print(f"=== Ingesting {drop_name} (from {drop_path}) ===")

    # 1. Ingest Leads
    leads_root = drop_path / "leads"
    if leads_root.exists():
        for wf_dir in sorted(leads_root.iterdir()):
            if not wf_dir.is_dir():
                continue
            wf_id = wf_dir.name
            wf_spec = registry.get_workflow(wf_id)
            if not wf_spec:
                print(f"Warning: Unknown workflow schema for '{wf_id}', skipping.", file=sys.stderr)
                continue

            # Process files in filename order
            csv_files = sorted(wf_dir.glob("*.csv"), key=lambda p: p.name)
            for csv_file in csv_files:
                res = lead_proc.process_lead_file(csv_file, wf_spec, drop_name)
                print(f"  [Leads] {wf_id}/{csv_file.name}: {res}")

    # Commit lead changes
    conn.commit()

    # 2. Ingest Events
    events_root = drop_path / "events"
    if events_root.exists():
        jsonl_files = sorted(events_root.glob("*.jsonl"), key=lambda p: p.name)
        for jsonl_file in jsonl_files:
            res = event_proc.process_event_file(jsonl_file, drop_name)
            print(f"  [Events] {jsonl_file.name}: {res}")

    # Commit event changes
    conn.commit()
    conn.close()
    print(f"=== Finished ingesting {drop_name} ===\n")


def generate_reports(
    output_dir: Path | str,
    db_path: Path | str = DEFAULT_DB_PATH,
    schemas_dir: Path | str = DEFAULT_SCHEMAS_DIR,
) -> None:
    out_path = Path(output_dir).resolve()
    registry = SchemaRegistry(schemas_dir)
    conn = get_connection(db_path)
    init_database(conn, registry.dispositions)

    print(f"=== Generating reports to '{out_path}' ===")

    # Rebuild the materialized calls table from the ledger before reporting
    t0 = time.time()
    sync_calls_table(conn)
    print(f"  Materialized calls table synced in {time.time() - t0:.2f}s")

    generator = ReportGenerator(conn)
    results = generator.generate_all(out_path)
    for name, p in results.items():
        print(f"  - Generated {name}: {p}")
    conn.close()
    print("=== Finished generating reports ===\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Agentic Universe Assignment A: Interaction Ledger & Reporting")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="Path to SQLite database file")
    parser.add_argument("--schemas", default=str(DEFAULT_SCHEMAS_DIR), help="Path to schemas directory")

    subparsers = parser.add_subparsers(dest="command", required=True)

    # Ingest subcommand
    ingest_parser = subparsers.add_parser("ingest", help="Ingest a data drop directory")
    ingest_parser.add_argument("drop_path", help="Path to drop directory (e.g. data/drop_1)")

    # Report subcommand
    report_parser = subparsers.add_parser("report", help="Generate report files")
    report_parser.add_argument("--out", required=True, help="Directory to write output reports to")

    args = parser.parse_args()

    if args.command == "ingest":
        ingest_drop(args.drop_path, db_path=args.db, schemas_dir=args.schemas)
    elif args.command == "report":
        generate_reports(args.out, db_path=args.db, schemas_dir=args.schemas)


if __name__ == "__main__":
    main()
