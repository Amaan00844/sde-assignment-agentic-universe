# Assignment A: Interaction Ledger & Reporting Pipeline — Solution

## Prerequisites

- Python 3.11+
- pip

## Setup

```bash
# Install dependencies
pip install -r requirements.txt
```

## Usage

### 1. Generate data

```bash
python assignment-a-data/tools/generate_data.py --seed 42 --scale small --out data
```

### 2. Ingest drops

```bash
python run.py ingest data/drop_1
python run.py ingest data/drop_2
```

Re-running the same drop is safe — files already ingested are automatically skipped (idempotent).

### 3. Generate reports

```bash
python run.py report --out reports/
```

This produces 5 files in `reports/`:
- `batch_summary.csv`
- `daily_overview.csv`
- `lead_report.csv`
- `rejections.csv`
- `data_quality.json`

### 4. Run tests

```bash
python -m pytest tests/ -v
```

70 tests covering phone normalisation, date/decimal parsing, lead processing, event processing, disposition logic, orphan resolution, and full E2E pipeline.

## Project Structure

```
solution/
├── __init__.py
├── config.py           # Paths, timezone, PII keys
├── crypto.py           # HMAC hashing, Fernet encryption, phone masking
├── schema.py           # Schema loading, field validators, dispositions
├── db.py               # SQLite schema, materialized calls, counters
├── lead_processor.py   # CSV batch processing, rejection, merging
├── event_processor.py  # JSONL processing, dedup, timestamp normalisation
├── reporting.py        # Report generators (5 files)
└── cli.py              # CLI entry point (ingest / report)
```

## Database

SQLite, stored at `solution/ledger.db`. PII (phone, name) is encrypted at rest using HMAC-SHA-256 hashing and Fernet symmetric encryption.
