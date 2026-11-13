"""
Reporting generator: produces the five required report files exactly to the specification contract.
Uses the materialized `calls` table for fast SQL-based aggregation.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Dict

import sqlite3

from solution.db import get_counters, get_orphan_events_count, get_rejections_by_reason


class ReportGenerator:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def generate_all(self, output_dir: Path | str) -> Dict[str, Path]:
        """
        Generates the 5 required report files in output_dir:
          1. batch_summary.csv
          2. daily_overview.csv
          3. lead_report.csv
          4. rejections.csv
          5. data_quality.json
        """
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)

        batch_summary_path = out / "batch_summary.csv"
        daily_overview_path = out / "daily_overview.csv"
        lead_report_path = out / "lead_report.csv"
        rejections_path = out / "rejections.csv"
        data_quality_path = out / "data_quality.json"

        self._generate_batch_summary(batch_summary_path)
        self._generate_daily_overview(daily_overview_path)
        self._generate_lead_report(lead_report_path)
        self._generate_rejections(rejections_path)
        self._generate_data_quality(data_quality_path)

        return {
            "batch_summary": batch_summary_path,
            "daily_overview": daily_overview_path,
            "lead_report": lead_report_path,
            "rejections": rejections_path,
            "data_quality": data_quality_path,
        }

    def _generate_batch_summary(self, path: Path) -> None:
        """
        1. batch_summary.csv, sorted by workflow_id, batch_id
        """
        sql = """
            WITH linked_calls AS (
                SELECT c.workflow_id, c.call_id, c.phone_hash,
                       c.has_telephony_failed, c.effective_disposition
                FROM calls c
                JOIN leads l ON c.workflow_id = l.workflow_id AND c.phone_hash = l.phone_hash
            ),
            lead_call_stats AS (
                SELECT
                    lc.workflow_id,
                    lc.phone_hash,
                    COUNT(lc.call_id) AS total_calls,
                    MAX(COALESCE(dc.is_terminal, 0)) AS any_terminal,
                    MIN(lc.has_telephony_failed) AS all_telephony_failed
                FROM linked_calls lc
                LEFT JOIN disposition_catalogue dc ON lc.workflow_id = dc.workflow_id AND lc.effective_disposition = dc.code
                GROUP BY lc.workflow_id, lc.phone_hash
            ),
            lead_buckets AS (
                SELECT
                    l.workflow_id,
                    l.batch_id,
                    CASE
                        WHEN lcs.total_calls IS NULL THEN 'yet_to_reach_out'
                        WHEN lcs.any_terminal = 1 THEN 'completed'
                        WHEN lcs.all_telephony_failed = 1 THEN 'in_progress_telephony_failed'
                        ELSE 'in_progress_dialed'
                    END AS bucket
                FROM leads l
                LEFT JOIN lead_call_stats lcs ON l.workflow_id = lcs.workflow_id AND l.phone_hash = lcs.phone_hash
            )
            SELECT
                workflow_id,
                batch_id,
                COUNT(*) AS total_leads,
                SUM(CASE WHEN bucket = 'yet_to_reach_out' THEN 1 ELSE 0 END) AS yet_to_reach_out,
                SUM(CASE WHEN bucket = 'in_progress_telephony_failed' THEN 1 ELSE 0 END) AS in_progress_telephony_failed,
                SUM(CASE WHEN bucket = 'in_progress_dialed' THEN 1 ELSE 0 END) AS in_progress_dialed,
                SUM(CASE WHEN bucket = 'completed' THEN 1 ELSE 0 END) AS completed
            FROM lead_buckets
            GROUP BY workflow_id, batch_id
            ORDER BY workflow_id, batch_id;
        """
        rows = self.conn.execute(sql).fetchall()
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                "workflow_id", "batch_id", "total_leads",
                "yet_to_reach_out", "in_progress_telephony_failed",
                "in_progress_dialed", "completed",
            ])
            for r in rows:
                writer.writerow([
                    r["workflow_id"], r["batch_id"], r["total_leads"],
                    r["yet_to_reach_out"], r["in_progress_telephony_failed"],
                    r["in_progress_dialed"], r["completed"],
                ])

    def _generate_daily_overview(self, path: Path) -> None:
        """
        2. daily_overview.csv: one row per workflow per IST date that has at least one call,
        sorted by workflow_id, date
        """
        sql = """
            WITH linked_calls AS (
                SELECT c.workflow_id, c.call_id, c.phone_hash, c.call_date,
                       c.is_connected, c.effective_disposition
                FROM calls c
                JOIN leads l ON c.workflow_id = l.workflow_id AND c.phone_hash = l.phone_hash
            )
            SELECT
                lc.workflow_id,
                lc.call_date AS date,
                COUNT(DISTINCT lc.call_id) AS total_calls,
                SUM(lc.is_connected) AS connected_calls,
                printf('%.4f', CAST(SUM(lc.is_connected) AS FLOAT) / COUNT(DISTINCT lc.call_id)) AS connectivity_rate,
                COUNT(DISTINCT CASE WHEN dc.is_success = 1 THEN lc.phone_hash ELSE NULL END) AS success_leads
            FROM linked_calls lc
            LEFT JOIN disposition_catalogue dc ON lc.workflow_id = dc.workflow_id AND lc.effective_disposition = dc.code
            GROUP BY lc.workflow_id, lc.call_date
            HAVING COUNT(DISTINCT lc.call_id) > 0
            ORDER BY lc.workflow_id, date;
        """
        rows = self.conn.execute(sql).fetchall()
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                "workflow_id", "date", "total_calls", "connected_calls",
                "connectivity_rate", "success_leads",
            ])
            for r in rows:
                writer.writerow([
                    r["workflow_id"], r["date"], r["total_calls"],
                    r["connected_calls"], r["connectivity_rate"], r["success_leads"],
                ])

    def _generate_lead_report(self, path: Path) -> None:
        """
        3. lead_report.csv: one row per lead, sorted by workflow_id, external_ref
        """
        sql = """
            WITH linked_calls AS (
                SELECT c.workflow_id, c.call_id, c.phone_hash,
                       c.initiated_at_ist, c.is_connected, c.has_telephony_failed,
                       c.effective_disposition
                FROM calls c
                JOIN leads l ON c.workflow_id = l.workflow_id AND c.phone_hash = l.phone_hash
            ),
            lead_call_stats AS (
                SELECT
                    lc.workflow_id,
                    lc.phone_hash,
                    COUNT(lc.call_id) AS total_calls,
                    SUM(lc.is_connected) AS connected_calls,
                    MAX(lc.initiated_at_ist) AS last_call_at,
                    MAX(COALESCE(dc.is_terminal, 0)) AS any_terminal,
                    MIN(lc.has_telephony_failed) AS all_telephony_failed,
                    MAX(COALESCE(dc.is_success, 0)) AS any_success
                FROM linked_calls lc
                LEFT JOIN disposition_catalogue dc ON lc.workflow_id = dc.workflow_id AND lc.effective_disposition = dc.code
                GROUP BY lc.workflow_id, lc.phone_hash
            ),
            ranked_disps AS (
                SELECT
                    lc.workflow_id,
                    lc.phone_hash,
                    lc.effective_disposition,
                    ROW_NUMBER() OVER (
                        PARTITION BY lc.workflow_id, lc.phone_hash
                        ORDER BY lc.initiated_at_ist DESC, lc.call_id DESC
                    ) AS rn
                FROM linked_calls lc
                WHERE lc.effective_disposition IS NOT NULL
            )
            SELECT
                l.workflow_id,
                l.external_ref,
                l.batch_id,
                l.phone_masked,
                CASE
                    WHEN COALESCE(lcs.total_calls, 0) = 0 THEN 'yet_to_reach_out'
                    WHEN lcs.any_terminal = 1 THEN 'completed'
                    WHEN lcs.all_telephony_failed = 1 THEN 'in_progress_telephony_failed'
                    ELSE 'in_progress_dialed'
                END AS bucket,
                COALESCE(lcs.total_calls, 0) AS total_calls,
                COALESCE(lcs.connected_calls, 0) AS connected_calls,
                COALESCE(lcs.last_call_at, '') AS last_call_at,
                COALESCE(rd.effective_disposition, '') AS current_disposition,
                CASE WHEN COALESCE(lcs.any_success, 0) = 1 THEN 'true' ELSE 'false' END AS ever_success
            FROM leads l
            LEFT JOIN lead_call_stats lcs ON l.workflow_id = lcs.workflow_id AND l.phone_hash = lcs.phone_hash
            LEFT JOIN ranked_disps rd ON l.workflow_id = rd.workflow_id AND l.phone_hash = rd.phone_hash AND rd.rn = 1
            ORDER BY l.workflow_id, l.external_ref;
        """
        rows = self.conn.execute(sql).fetchall()
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                "workflow_id", "external_ref", "batch_id", "phone_masked",
                "bucket", "total_calls", "connected_calls", "last_call_at",
                "current_disposition", "ever_success",
            ])
            for r in rows:
                writer.writerow([
                    r["workflow_id"], r["external_ref"], r["batch_id"],
                    r["phone_masked"], r["bucket"], r["total_calls"],
                    r["connected_calls"], r["last_call_at"],
                    r["current_disposition"], r["ever_success"],
                ])

    def _generate_rejections(self, path: Path) -> None:
        """
        4. rejections.csv, sorted by workflow_id, batch_id, row_number. No PII.
        """
        sql = """
            SELECT workflow_id, batch_id, row_number, reason
            FROM rejections
            ORDER BY workflow_id, batch_id, row_number;
        """
        rows = self.conn.execute(sql).fetchall()
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["workflow_id", "batch_id", "row_number", "reason"])
            for r in rows:
                writer.writerow([r["workflow_id"], r["batch_id"], r["row_number"], r["reason"]])

    def _generate_data_quality(self, path: Path) -> None:
        """
        5. data_quality.json
        """
        counters = get_counters(self.conn)
        rejections_by_reason = get_rejections_by_reason(self.conn)
        orphan_events = get_orphan_events_count(self.conn)

        data = {
            "lead_rows_total": counters["lead_rows_total"],
            "lead_rows_rejected": counters["lead_rows_rejected"],
            "rejections_by_reason": rejections_by_reason,
            "duplicate_lead_rows_merged": counters["duplicate_lead_rows_merged"],
            "field_warnings": counters["field_warnings"],
            "events_total": counters["events_total"],
            "malformed_events": counters["malformed_events"],
            "duplicate_events_dropped": counters["duplicate_events_dropped"],
            "orphan_events": orphan_events,
            "unknown_disposition_events": counters["unknown_disposition_events"],
        }

        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
