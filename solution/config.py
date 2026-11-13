"""
Configuration settings for Assignment A pipeline.
"""
from __future__ import annotations

import os
from datetime import timedelta, timezone
from pathlib import Path

# Indian Standard Time (UTC+5:30)
IST = timezone(timedelta(hours=5, minutes=30))

# Base directory of the repository
BASE_DIR = Path(__file__).resolve().parent.parent

# Default paths
DEFAULT_SCHEMAS_DIR = BASE_DIR / "assignment-a-data" / "schemas"
DEFAULT_DB_PATH = BASE_DIR / "solution" / "ledger.db"

# PII Pepper for HMAC-SHA256 (can be overridden via environment variable)
PEPPER_KEY = os.getenv("AU_PII_PEPPER", "agentic_universe_secure_pepper_2026").encode("utf-8")

# Fernet encryption key for reversible PII at rest (must be url-safe base64-encoded 32-byte key)
# Default static dev key for reproducible evaluation unless overridden via AU_FERNET_KEY
FERNET_KEY = os.getenv("AU_FERNET_KEY", "uKzN8_uS07fW_9YVp8B8YQ6P8d_jH8uB2vD_6N5t3rM=").encode("utf-8")
