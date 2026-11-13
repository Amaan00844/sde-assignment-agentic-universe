"""
Event ingestion: malformed check, deduplication, timestamp parsing to IST,
unknown disposition filtering, and append-only ledger storage.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import sqlite3

from solution.config import IST
from solution.crypto import hash_phone
from solution.db import increment_counter
from solution.lead_processor import compute_file_hash
from solution.schema import DispositionCatalogue, parse_phone, parse_timestamp_ist


class EventProcessor:
    def __init__(self, conn: sqlite3.Connection, catalogue: DispositionCatalogue):
        self.conn = conn
        self.catalogue = catalogue
        # Cache existing event_ids in memory for fast O(1) deduplication
        self.seen_event_ids: Set[str] = set()
        self._load_seen_events()

    def _load_seen_events(self) -> None:
        rows = self.conn.execute("SELECT event_id FROM interaction_events;").fetchall()
        for r in rows:
            self.seen_event_ids.add(r[0])

    def process_event_file(self, file_path: Path, drop_name: str) -> Dict[str, Any]:
        """
        Processes a single JSONL event file.
        Returns a summary of processed lines.
        """
        file_key = f"{drop_name}/events/{file_path.name}"
        file_hash = compute_file_hash(file_path)

        # Check file idempotency
        existing = self.conn.execute(
            "SELECT records_count FROM ingested_sources WHERE file_path = ? AND file_hash = ?;",
            (file_key, file_hash),
        ).fetchone()
        if existing is not None:
            return {"status": "skipped", "reason": "already_ingested", "records": existing[0]}

        lines_total = 0
        malformed = 0
        duplicates = 0
        unknown_disp = 0

        valid_events: List[Tuple[str, str, str, str, str, Optional[str], str, Optional[str], int, int, int]] = []

        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue

                lines_total += 1

                # 1. Malformed check
                try:
                    data = json.loads(line)
                    if not isinstance(data, dict):
                        malformed += 1
                        continue
                    event_id = data.get("event_id")
                    if not event_id or not isinstance(event_id, str):
                        malformed += 1
                        continue
                except Exception:
                    malformed += 1
                    continue

                # 2. Duplicate check
                if event_id in self.seen_event_ids:
                    duplicates += 1
                    continue

                # 3. Parse timestamp
                occurred_at_raw = data.get("occurred_at")
                if not occurred_at_raw:
                    malformed += 1
                    continue
                try:
                    dt_ist = parse_timestamp_ist(occurred_at_raw)
                    occurred_at_ist = dt_ist.isoformat()
                except Exception:
                    malformed += 1
                    continue

                event_type = data.get("event_type", "")
                workflow_id = data.get("workflow_id", "")
                call_id = data.get("call_id", "")
                payload = data.get("payload", {})
                if not isinstance(payload, dict):
                    payload = {}

                # 4. Unknown disposition check:
                # "a disposition.* event whose code is not in dispositions.csv for that workflow -> count it and ignore it."
                disposition_code: Optional[str] = None
                if event_type.startswith("disposition."):
                    disposition_code = payload.get("disposition_code")
                    if not disposition_code or not self.catalogue.is_known(workflow_id, disposition_code):
                        unknown_disp += 1
                        # Do not insert into ledger; ignore it
                        self.seen_event_ids.add(event_id)
                        continue

                # 5. Phone normalization & hashing
                raw_phone = data.get("phone")
                phone_hash: Optional[str] = None
                if raw_phone:
                    canonical_p = parse_phone(raw_phone)
                    if canonical_p:
                        phone_hash = hash_phone(canonical_p)

                is_telephony_failed = 1 if event_type == "call.telephony_failed" else 0
                is_connected = 1 if event_type == "call.connected" else 0
                is_call_initiated = 1 if event_type == "call.initiated" else 0

                valid_events.append((
                    event_id,
                    event_type,
                    workflow_id,
                    call_id,
                    occurred_at_ist,
                    phone_hash,
                    json.dumps(payload),
                    disposition_code,
                    is_telephony_failed,
                    is_connected,
                    is_call_initiated,
                ))
                self.seen_event_ids.add(event_id)

        # Batch insert into append-only ledger
        if valid_events:
            self.conn.executemany(
                """
                INSERT OR IGNORE INTO interaction_events (
                    event_id, event_type, workflow_id, call_id, occurred_at_ist,
                    phone_hash, payload_json, disposition_code,
                    is_telephony_failed, is_connected, is_call_initiated
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                valid_events,
            )

        # Update quality counters
        increment_counter(self.conn, "events_total", lines_total)
        increment_counter(self.conn, "malformed_events", malformed)
        increment_counter(self.conn, "duplicate_events_dropped", duplicates)
        increment_counter(self.conn, "unknown_disposition_events", unknown_disp)

        now_ts = datetime.now(IST).isoformat()
        self.conn.execute(
            """
            INSERT OR REPLACE INTO ingested_sources (
                file_path, file_hash, file_type, workflow_id, batch_id, records_count, ingested_at
            ) VALUES (?, ?, 'events', NULL, NULL, ?, ?);
            """,
            (file_key, file_hash, lines_total, now_ts),
        )

        return {
            "status": "success",
            "lines_total": lines_total,
            "malformed": malformed,
            "duplicates": duplicates,
            "unknown_disp": unknown_disp,
            "valid_inserted": len(valid_events),
        }
