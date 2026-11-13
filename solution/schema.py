"""
Schema definitions, header matching, field validators, and disposition catalogue loader.
"""
from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from solution.config import IST

MONTH_MAP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def normalize_header(header: str) -> str:
    """
    Normalise header: lower-case it, trim it, and replace every run of '_'
    or whitespace with a single space.
    """
    if not header:
        return ""
    h = header.strip().lower()
    return re.sub(r"[\s_]+", " ", h)


def parse_phone(raw: str) -> Optional[str]:
    """
    Indian mobile number validation:
    - Strip every non-digit.
    - If 12 digits starting with 91, drop the 91.
    - If 11 digits starting with 0, drop the 0.
    - Valid only if exactly 10 digits and starts with 6, 7, 8 or 9.
    - Stored canonically as +91XXXXXXXXXX.
    """
    if raw is None:
        return None
    digits = re.sub(r"\D", "", str(raw))
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) == 10 and digits[0] in "6789":
        return f"+91{digits}"
    return None


def parse_date(raw: str) -> Optional[str]:
    """
    Date validation:
    Accepts:
      - YYYY-MM-DD
      - DD/MM/YYYY
      - DD-MM-YYYY
      - DD-Mon-YYYY (case-insensitive month, e.g. 01-Dec-2026)
    Dates are day-first. Impossible dates (like 31/02) are invalid.
    Returns canonical YYYY-MM-DD string.
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None

    # YYYY-MM-DD
    m1 = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", s)
    if m1:
        y, m, d = int(m1.group(1)), int(m1.group(2)), int(m1.group(3))
        try:
            return datetime(y, m, d).strftime("%Y-%m-%d")
        except ValueError:
            return None

    # DD/MM/YYYY
    m2 = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", s)
    if m2:
        d, m, y = int(m2.group(1)), int(m2.group(2)), int(m2.group(3))
        try:
            return datetime(y, m, d).strftime("%Y-%m-%d")
        except ValueError:
            return None

    # DD-MM-YYYY
    m3 = re.match(r"^(\d{1,2})-(\d{1,2})-(\d{4})$", s)
    if m3:
        d, m, y = int(m3.group(1)), int(m3.group(2)), int(m3.group(3))
        try:
            return datetime(y, m, d).strftime("%Y-%m-%d")
        except ValueError:
            return None

    # DD-Mon-YYYY
    m4 = re.match(r"^(\d{1,2})-([A-Za-z]{3})-(\d{4})$", s)
    if m4:
        d, mon_str, y = int(m4.group(1)), m4.group(2).lower(), int(m4.group(3))
        if mon_str in MONTH_MAP:
            m = MONTH_MAP[mon_str]
            try:
                return datetime(y, m, d).strftime("%Y-%m-%d")
            except ValueError:
                return None

    return None


def parse_decimal(raw: str) -> Optional[float]:
    """
    Decimal validation:
    - Optionally prefixed by ₹, Rs, Rs. or INR (case-insensitive, optional space).
    - Commas are thousands separators.
    - Must be >= 0.
    - Round to 2 places.
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    s = re.sub(r"^(?:[₹\u20b9]|rs\.?|inr)\s*", "", s, flags=re.IGNORECASE).strip()
    s = s.replace(",", "")
    try:
        val = float(s)
        if val < 0:
            return None
        return round(val, 2)
    except ValueError:
        return None


def parse_enum(raw: str, allowed_values: List[str]) -> Optional[str]:
    """
    Enum validation:
    Compared case-insensitively against allowed values and stored lower-case.
    """
    if raw is None:
        return None
    s = str(raw).strip().lower()
    allowed = {v.lower(): v.lower() for v in allowed_values}
    return allowed.get(s)


def parse_string(raw: str) -> Optional[str]:
    """
    String validation:
    Trimmed value. Empty string counts as missing (None).
    """
    if raw is None:
        return None
    s = str(raw).strip()
    return s if s else None


def parse_timestamp_ist(ts_str: str) -> datetime:
    """
    Parses timestamp in UTC ('...Z'), offset ('...+05:30'), or naive ('2026-10-03 10:01:00').
    Naive means IST. Converts everything to an aware datetime in IST timezone.
    """
    s = ts_str.strip()
    if s.endswith("Z"):
        dt = datetime.fromisoformat(s[:-1] + "+00:00")
        return dt.astimezone(IST)
    elif "+" in s or (len(s) > 10 and "-" in s[10:]):
        dt = datetime.fromisoformat(s)
        return dt.astimezone(IST)
    else:
        # Naive timestamp -> means IST per spec
        dt = datetime.fromisoformat(s)
        return dt.replace(tzinfo=IST)


@dataclass
class FieldSpec:
    name: str
    type: str  # string, phone, date, decimal, enum
    required: bool
    pii: bool = False
    reference: bool = False
    default: Optional[str] = None
    values: List[str] = field(default_factory=list)
    aliases: List[str] = field(default_factory=list)
    normalized_name: str = ""
    normalized_aliases: List[str] = field(default_factory=list)

    def __post_init__(self):
        self.normalized_name = normalize_header(self.name)
        self.normalized_aliases = [normalize_header(a) for a in self.aliases]


@dataclass
class WorkflowSpec:
    workflow_id: str
    description: str
    fields: List[FieldSpec]
    field_by_name: Dict[str, FieldSpec] = field(default_factory=dict)
    reference_field_name: str = ""

    def __post_init__(self):
        for f in self.fields:
            self.field_by_name[f.name] = f
            if f.reference:
                self.reference_field_name = f.name


@dataclass
class DispositionSpec:
    workflow_id: str
    code: str
    label: str
    is_terminal: bool
    is_success: bool


class DispositionCatalogue:
    def __init__(self, csv_path: Path | str):
        self._items: Dict[tuple[str, str], DispositionSpec] = {}
        self.load(csv_path)

    def load(self, csv_path: Path | str) -> None:
        with open(csv_path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                wf = row["workflow_id"].strip()
                code = row["code"].strip()
                label = row["label"].strip()
                is_terminal = row["is_terminal"].strip().lower() == "true"
                is_success = row["is_success"].strip().lower() == "true"
                spec = DispositionSpec(wf, code, label, is_terminal, is_success)
                self._items[(wf, code)] = spec

    def get(self, workflow_id: str, code: str) -> Optional[DispositionSpec]:
        return self._items.get((workflow_id, code))

    def is_known(self, workflow_id: str, code: str) -> bool:
        return (workflow_id, code) in self._items


class SchemaRegistry:
    def __init__(self, schemas_dir: Path | str):
        self.schemas_dir = Path(schemas_dir)
        self.workflows: Dict[str, WorkflowSpec] = {}
        self.dispositions: DispositionCatalogue = DispositionCatalogue(self.schemas_dir / "dispositions.csv")
        self.load_schemas()

    def load_schemas(self) -> None:
        for json_file in self.schemas_dir.glob("*.json"):
            with open(json_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            fields = []
            for f in data.get("fields", []):
                fields.append(
                    FieldSpec(
                        name=f["name"],
                        type=f["type"],
                        required=f.get("required", False),
                        pii=f.get("pii", False),
                        reference=f.get("reference", False),
                        default=f.get("default"),
                        values=f.get("values", []),
                        aliases=f.get("aliases", []),
                    )
                )
            wf_spec = WorkflowSpec(
                workflow_id=data["workflow_id"],
                description=data.get("description", ""),
                fields=fields,
            )
            self.workflows[wf_spec.workflow_id] = wf_spec

    def get_workflow(self, workflow_id: str) -> Optional[WorkflowSpec]:
        return self.workflows.get(workflow_id)
