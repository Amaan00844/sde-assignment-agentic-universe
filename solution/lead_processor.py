"""
Lead batch processing: header matching, schema validation, rejection, merging, and PII protection.
"""
from __future__ import annotations

import csv
import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import sqlite3

from solution.config import IST
from solution.crypto import encrypt_pii, hash_phone, mask_phone
from solution.db import (
    increment_counter,
    increment_rejection_reason,
)
from solution.schema import (
    FieldSpec,
    WorkflowSpec,
    normalize_header,
    parse_date,
    parse_decimal,
    parse_enum,
    parse_phone,
    parse_string,
)


def compute_file_hash(path: Path | str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


class LeadProcessor:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def process_lead_file(
        self,
        file_path: Path,
        workflow_spec: WorkflowSpec,
        drop_name: str,
    ) -> Dict[str, Any]:
        """
        Processes a single CSV lead batch for the given workflow.
        Returns a summary of processed rows.
        """
        batch_id = file_path.stem
        workflow_id = workflow_spec.workflow_id
        file_key = f"{drop_name}/leads/{workflow_id}/{file_path.name}"
        file_hash = compute_file_hash(file_path)

        # Check idempotency: if this exact file has already been ingested, skip
        existing = self.conn.execute(
            "SELECT records_count FROM ingested_sources WHERE file_path = ? AND file_hash = ?;",
            (file_key, file_hash),
        ).fetchone()
        if existing is not None:
            return {"status": "skipped", "reason": "already_ingested", "records": existing[0]}

        rows_total = 0
        rows_rejected = 0
        duplicate_merged = 0
        warnings = 0

        rejection_records: List[Tuple[str, str, int, str]] = []
        rejection_reason_counts: Dict[str, int] = {}

        # In-memory index of leads for this workflow to enable fast batching
        existing_leads_db: Dict[str, Dict[str, Any]] = {}
        for r in self.conn.execute(
            "SELECT phone_hash, batch_id, external_ref, customer_name_encrypted, attributes_json FROM leads WHERE workflow_id = ?;",
            (workflow_id,),
        ).fetchall():
            existing_leads_db[r["phone_hash"]] = {
                "batch_id": r["batch_id"],
                "external_ref": r["external_ref"],
                "customer_name_encrypted": r["customer_name_encrypted"],
                "attributes_json": r["attributes_json"],
                "in_db": True,
            }

        new_leads_to_insert: Dict[str, Tuple] = {}
        leads_to_update: List[Tuple] = []
        now_ts = datetime.now(IST).isoformat()

        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            reader = csv.reader(f)
            try:
                raw_headers = next(reader)
            except StopIteration:
                return {"status": "empty", "records": 0}

            # Header mapping: normalize headers and match against field name or aliases
            col_to_field: List[Optional[FieldSpec]] = []
            for h in raw_headers:
                norm_h = normalize_header(h)
                matched_field: Optional[FieldSpec] = None
                for f_spec in workflow_spec.fields:
                    if norm_h == f_spec.normalized_name or norm_h in f_spec.normalized_aliases:
                        matched_field = f_spec
                        break
                col_to_field.append(matched_field)

            row_number = 0
            for row in reader:
                # Blank rows (every cell empty or whitespace) are skipped and not counted
                if not any(cell.strip() for cell in row):
                    continue

                row_number += 1
                rows_total += 1

                # Map cells to field names
                row_raw_values: Dict[str, str] = {}
                for col_idx, cell in enumerate(row):
                    if col_idx < len(col_to_field):
                        f_spec = col_to_field[col_idx]
                        if f_spec:
                            row_raw_values[f_spec.name] = cell.strip()

                # Walk schema fields in schema order for rejection
                rejected = False
                rejection_reason: Optional[str] = None
                row_parsed_values: Dict[str, Any] = {}
                row_warnings = 0

                for f_spec in workflow_spec.fields:
                    fname = f_spec.name
                    raw_val = row_raw_values.get(fname)
                    is_missing = raw_val is None or raw_val == ""

                    if f_spec.required:
                        if is_missing:
                            rejection_reason = f"missing_required:{fname}"
                            rejected = True
                            break
                        if f_spec.type == "phone":
                            canonical_p = parse_phone(raw_val)
                            if canonical_p is None:
                                rejection_reason = "invalid_phone"
                                rejected = True
                                break
                            row_parsed_values[fname] = canonical_p
                        elif f_spec.type == "string":
                            row_parsed_values[fname] = parse_string(raw_val)
                        else:
                            row_parsed_values[fname] = raw_val
                    else:
                        if is_missing:
                            row_parsed_values[fname] = None
                        else:
                            parsed = None
                            if f_spec.type == "string":
                                parsed = parse_string(raw_val)
                            elif f_spec.type == "phone":
                                parsed = parse_phone(raw_val)
                            elif f_spec.type == "date":
                                parsed = parse_date(raw_val)
                            elif f_spec.type == "decimal":
                                parsed = parse_decimal(raw_val)
                            elif f_spec.type == "enum":
                                parsed = parse_enum(raw_val, f_spec.values)

                            if parsed is None:
                                row_parsed_values[fname] = None
                                row_warnings += 1
                            else:
                                row_parsed_values[fname] = parsed

                if rejected:
                    rows_rejected += 1
                    rejection_records.append((workflow_id, batch_id, row_number, rejection_reason))
                    rejection_reason_counts[rejection_reason] = rejection_reason_counts.get(rejection_reason, 0) + 1
                    continue

                # Row is valid: apply warnings
                warnings += row_warnings

                canonical_phone = row_parsed_values["phone"]
                p_hash = hash_phone(canonical_phone)
                p_mask = mask_phone(canonical_phone)
                customer_name = row_parsed_values.get("customer_name", "")
                external_ref = str(row_parsed_values.get(workflow_spec.reference_field_name, ""))

                attributes = {
                    f.name: row_parsed_values.get(f.name)
                    for f in workflow_spec.fields
                    if not f.pii and not f.reference
                }

                if p_hash not in existing_leads_db:
                    # First time seeing this lead
                    for f_spec in workflow_spec.fields:
                        if not f_spec.pii and not f_spec.reference and f_spec.default is not None:
                            if attributes.get(f_spec.name) is None:
                                attributes[f_spec.name] = f_spec.default

                    name_enc = encrypt_pii(customer_name)
                    phone_enc = encrypt_pii(canonical_phone)
                    attrs_json = json.dumps(attributes)

                    existing_leads_db[p_hash] = {
                        "batch_id": batch_id,
                        "external_ref": external_ref,
                        "customer_name_encrypted": name_enc,
                        "attributes_json": attrs_json,
                        "in_db": False,
                    }
                    new_leads_to_insert[p_hash] = (
                        workflow_id,
                        p_hash,
                        p_mask,
                        phone_enc,
                        name_enc,
                        batch_id,
                        external_ref,
                        attrs_json,
                        now_ts,
                        now_ts,
                    )
                else:
                    # Merged duplicate
                    duplicate_merged += 1
                    lead_data = existing_leads_db[p_hash]
                    prev_attributes = json.loads(lead_data["attributes_json"])
                    for k, v in attributes.items():
                        if v is not None:
                            prev_attributes[k] = v

                    for f_spec in workflow_spec.fields:
                        if not f_spec.pii and not f_spec.reference and f_spec.default is not None:
                            if prev_attributes.get(f_spec.name) is None:
                                prev_attributes[f_spec.name] = f_spec.default

                    new_name_enc = (
                        encrypt_pii(customer_name)
                        if customer_name
                        else lead_data["customer_name_encrypted"]
                    )
                    new_ref = external_ref if external_ref else lead_data["external_ref"]
                    new_attrs_json = json.dumps(prev_attributes)

                    lead_data["customer_name_encrypted"] = new_name_enc
                    lead_data["external_ref"] = new_ref
                    lead_data["attributes_json"] = new_attrs_json

                    if not lead_data["in_db"]:
                        new_leads_to_insert[p_hash] = (
                            workflow_id,
                            p_hash,
                            p_mask,
                            encrypt_pii(canonical_phone),
                            new_name_enc,
                            lead_data["batch_id"],
                            new_ref,
                            new_attrs_json,
                            now_ts,
                            now_ts,
                        )
                    else:
                        leads_to_update.append((
                            new_name_enc,
                            new_ref,
                            new_attrs_json,
                            now_ts,
                            workflow_id,
                            p_hash,
                        ))

        # Batch insert new leads
        if new_leads_to_insert:
            self.conn.executemany(
                """
                INSERT INTO leads (
                    workflow_id, phone_hash, phone_masked, phone_encrypted,
                    customer_name_encrypted, batch_id, external_ref,
                    attributes_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                list(new_leads_to_insert.values()),
            )

        # Batch update merged leads
        if leads_to_update:
            self.conn.executemany(
                """
                UPDATE leads
                SET customer_name_encrypted = ?,
                    external_ref = ?,
                    attributes_json = ?,
                    updated_at = ?
                WHERE workflow_id = ? AND phone_hash = ?;
                """,
                leads_to_update,
            )

        # Record rejections in database
        if rejection_records:
            self.conn.executemany(
                """
                INSERT OR REPLACE INTO rejections (workflow_id, batch_id, row_number, reason)
                VALUES (?, ?, ?, ?);
                """,
                rejection_records,
            )

        # Update persistent counters
        increment_counter(self.conn, "lead_rows_total", rows_total)
        increment_counter(self.conn, "lead_rows_rejected", rows_rejected)
        increment_counter(self.conn, "duplicate_lead_rows_merged", duplicate_merged)
        increment_counter(self.conn, "field_warnings", warnings)

        for reason, cnt in rejection_reason_counts.items():
            increment_rejection_reason(self.conn, reason, cnt)

        # Mark source as ingested
        now_ts = datetime.now(IST).isoformat()
        self.conn.execute(
            """
            INSERT OR REPLACE INTO ingested_sources (
                file_path, file_hash, file_type, workflow_id, batch_id, records_count, ingested_at
            ) VALUES (?, ?, 'leads', ?, ?, ?, ?);
            """,
            (file_key, file_hash, workflow_id, batch_id, rows_total, now_ts),
        )

        return {
            "status": "success",
            "rows_total": rows_total,
            "rows_rejected": rows_rejected,
            "duplicate_merged": duplicate_merged,
            "warnings": warnings,
        }
