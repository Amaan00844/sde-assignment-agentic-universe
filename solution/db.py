"""
Database management, schema setup, tables, and repository methods using SQLite.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from solution.config import DEFAULT_DB_PATH
from solution.schema import DispositionCatalogue, SchemaRegistry


def get_connection(db_path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """
    Creates and returns a SQLite connection optimized for high throughput.
    Uses WAL journal mode and normal synchronous flag.
    """
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=60.0)
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA cache_size = -64000;")  # 64MB cache
    conn.row_factory = sqlite3.Row
    return conn


def init_database(conn: sqlite3.Connection, catalogue: Optional[DispositionCatalogue] = None) -> None:
    """
    Initializes database tables and indexes.
    """
    with conn:
        # Table 1: Ingested source files for tracking and idempotency
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ingested_sources (
                file_path TEXT PRIMARY KEY,
                file_hash TEXT NOT NULL,
                file_type TEXT NOT NULL,
                workflow_id TEXT,
                batch_id TEXT,
                records_count INTEGER NOT NULL DEFAULT 0,
                ingested_at TEXT NOT NULL
            );
        """)

        # Table 2: Persistent quality counters
        conn.execute("""
            CREATE TABLE IF NOT EXISTS quality_counters (
                metric_name TEXT PRIMARY KEY,
                metric_value INTEGER NOT NULL DEFAULT 0
            );
        """)

        # Table 3: Rejections by reason counts
        conn.execute("""
            CREATE TABLE IF NOT EXISTS rejections_by_reason (
                reason TEXT PRIMARY KEY,
                count INTEGER NOT NULL DEFAULT 0
            );
        """)

        # Table 4: Rejections records
        conn.execute("""
            CREATE TABLE IF NOT EXISTS rejections (
                workflow_id TEXT NOT NULL,
                batch_id TEXT NOT NULL,
                row_number INTEGER NOT NULL,
                reason TEXT NOT NULL,
                PRIMARY KEY (workflow_id, batch_id, row_number)
            );
        """)

        # Table 5: Disposition catalogue
        conn.execute("""
            CREATE TABLE IF NOT EXISTS disposition_catalogue (
                workflow_id TEXT NOT NULL,
                code TEXT NOT NULL,
                label TEXT NOT NULL,
                is_terminal INTEGER NOT NULL DEFAULT 0,
                is_success INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (workflow_id, code)
            );
        """)

        # Table 6: Leads (PII protected at rest)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS leads (
                workflow_id TEXT NOT NULL,
                phone_hash TEXT NOT NULL,
                phone_masked TEXT NOT NULL,
                phone_encrypted TEXT NOT NULL,
                customer_name_encrypted TEXT NOT NULL,
                batch_id TEXT NOT NULL,
                external_ref TEXT NOT NULL,
                attributes_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (workflow_id, phone_hash)
            );
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_leads_wf_batch ON leads (workflow_id, batch_id);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_leads_wf_ref ON leads (workflow_id, external_ref);")

        # Table 7: Interaction Events (Append-Only Interaction Ledger)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS interaction_events (
                event_id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL,
                workflow_id TEXT NOT NULL,
                call_id TEXT NOT NULL,
                occurred_at_ist TEXT NOT NULL,
                phone_hash TEXT,
                payload_json TEXT NOT NULL,
                disposition_code TEXT,
                is_telephony_failed INTEGER NOT NULL DEFAULT 0,
                is_connected INTEGER NOT NULL DEFAULT 0,
                is_call_initiated INTEGER NOT NULL DEFAULT 0
            );
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_wf_call ON interaction_events (workflow_id, call_id);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_wf_phone ON interaction_events (workflow_id, phone_hash);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_call_type ON interaction_events (call_id, event_type);")

        # Table 8: Materialized Calls table — rebuilt at report time for fast queries
        conn.execute("""
            CREATE TABLE IF NOT EXISTS calls (
                workflow_id TEXT NOT NULL,
                call_id TEXT PRIMARY KEY,
                phone_hash TEXT,
                initiated_at_ist TEXT NOT NULL,
                call_date TEXT NOT NULL,
                is_connected INTEGER NOT NULL DEFAULT 0,
                has_telephony_failed INTEGER NOT NULL DEFAULT 0,
                effective_disposition TEXT
            );
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_calls_wf_phone ON calls (workflow_id, phone_hash);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_calls_wf_date ON calls (workflow_id, call_date);")

    # Populate disposition catalogue if provided
    if catalogue:
        with conn:
            for (wf, code), spec in catalogue._items.items():
                conn.execute("""
                    INSERT OR REPLACE INTO disposition_catalogue (
                        workflow_id, code, label, is_terminal, is_success
                    ) VALUES (?, ?, ?, ?, ?);
                """, (wf, code, spec.label, 1 if spec.is_terminal else 0, 1 if spec.is_success else 0))


def sync_calls_table(conn: sqlite3.Connection) -> None:
    """
    Rebuilds the materialized `calls` table from the append-only interaction ledger.
    Computes effective dispositions (overrides > sets, latest by occurred_at wins),
    initiated timestamps, connectivity, and telephony failure status.
    """
    with conn:
        conn.execute("DELETE FROM calls;")
        conn.execute("""
            INSERT INTO calls
            WITH call_base AS (
                SELECT
                    workflow_id,
                    call_id,
                    MAX(phone_hash) AS phone_hash,
                    MIN(CASE WHEN event_type = 'call.initiated' THEN occurred_at_ist ELSE NULL END) AS initiated_at_ist,
                    MAX(CASE WHEN event_type = 'call.connected' THEN 1 ELSE 0 END) AS is_connected,
                    MAX(CASE WHEN event_type = 'call.telephony_failed' THEN 1 ELSE 0 END) AS has_telephony_failed
                FROM interaction_events
                GROUP BY workflow_id, call_id
                HAVING initiated_at_ist IS NOT NULL
            ),
            ranked_overrides AS (
                SELECT workflow_id, call_id, disposition_code,
                       ROW_NUMBER() OVER (PARTITION BY workflow_id, call_id ORDER BY occurred_at_ist DESC, event_id DESC) as rn
                FROM interaction_events WHERE event_type = 'disposition.override' AND disposition_code IS NOT NULL
            ),
            ranked_sets AS (
                SELECT workflow_id, call_id, disposition_code,
                       ROW_NUMBER() OVER (PARTITION BY workflow_id, call_id ORDER BY occurred_at_ist DESC, event_id DESC) as rn
                FROM interaction_events WHERE event_type = 'disposition.set' AND disposition_code IS NOT NULL
            )
            SELECT
                cb.workflow_id,
                cb.call_id,
                cb.phone_hash,
                cb.initiated_at_ist,
                SUBSTR(cb.initiated_at_ist, 1, 10) AS call_date,
                cb.is_connected,
                cb.has_telephony_failed,
                COALESCE(ro.disposition_code, rs.disposition_code) AS effective_disposition
            FROM call_base cb
            LEFT JOIN ranked_overrides ro ON cb.workflow_id = ro.workflow_id AND cb.call_id = ro.call_id AND ro.rn = 1
            LEFT JOIN ranked_sets rs ON cb.workflow_id = rs.workflow_id AND cb.call_id = rs.call_id AND rs.rn = 1;
        """)


def increment_counter(conn: sqlite3.Connection, name: str, amount: int = 1) -> None:
    conn.execute("""
        INSERT INTO quality_counters (metric_name, metric_value)
        VALUES (?, ?)
        ON CONFLICT(metric_name) DO UPDATE SET metric_value = metric_value + excluded.metric_value;
    """, (name, amount))


def increment_rejection_reason(conn: sqlite3.Connection, reason: str, amount: int = 1) -> None:
    conn.execute("""
        INSERT INTO rejections_by_reason (reason, count)
        VALUES (?, ?)
        ON CONFLICT(reason) DO UPDATE SET count = count + excluded.count;
    """, (reason, amount))


def get_counters(conn: sqlite3.Connection) -> Dict[str, int]:
    rows = conn.execute("SELECT metric_name, metric_value FROM quality_counters;").fetchall()
    counters = {r["metric_name"]: r["metric_value"] for r in rows}
    defaults = [
        "lead_rows_total",
        "lead_rows_rejected",
        "duplicate_lead_rows_merged",
        "field_warnings",
        "events_total",
        "malformed_events",
        "duplicate_events_dropped",
        "unknown_disposition_events",
    ]
    for d in defaults:
        if d not in counters:
            counters[d] = 0
    return counters


def get_rejections_by_reason(conn: sqlite3.Connection) -> Dict[str, int]:
    rows = conn.execute("SELECT reason, count FROM rejections_by_reason ORDER BY reason;").fetchall()
    return {r["reason"]: r["count"] for r in rows}


def get_orphan_events_count(conn: sqlite3.Connection) -> int:
    """
    Computes count of orphan events — events whose call cannot be linked to any valid lead.
    A call is linked if it exists in the `calls` table and has a matching lead by phone_hash.
    """
    orphans = conn.execute("""
        WITH linked_calls AS (
            SELECT c.workflow_id, c.call_id
            FROM calls c
            JOIN leads l ON c.workflow_id = l.workflow_id AND c.phone_hash = l.phone_hash
        )
        SELECT COUNT(*)
        FROM interaction_events e
        WHERE NOT EXISTS (
            SELECT 1 FROM linked_calls lc
            WHERE e.workflow_id = lc.workflow_id AND e.call_id = lc.call_id
        );
    """).fetchone()[0]
    return orphans
