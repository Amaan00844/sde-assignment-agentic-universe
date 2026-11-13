# Submission

## Candidate
- **Name:** Amaan
- **Assignment chosen:** A (Data)
- **Time spent (honest estimate):** ~5h

## How to run
```bash
# 1. Install dependencies (from repo root)
pip install -r requirements.txt   # adds 'cryptography' for PII encryption

# 2. Generate synthetic data
python assignment-a-data/tools/generate_data.py --seed 42 --scale small --out data

# 3. Ingest drops in order
python run.py ingest data/drop_1
python run.py ingest data/drop_2

# 4. Generate reports
python run.py report --out reports/

# 5. Run tests (70 tests)
python -m pytest tests/ -v
```

## Design

### Architecture

The pipeline is a three-phase CLI application:

1. **Ingest Leads** — reads CSV batches per workflow, validates/normalises each row against the workflow's JSON schema, rejects bad rows, merges duplicates, and stores leads with PII encrypted at rest.
2. **Ingest Events** — reads JSONL event files line-by-line, deduplicates by `event_id`, normalises timestamps to IST, and appends to the interaction ledger. Events store `phone_hash` for later linking; no eager lead-lookup at ingest time.
3. **Report** — materialises a `calls` table from the ledger (computes effective dispositions, connectivity, etc.), then runs SQL queries to produce the five report files.

```
CSV batches ──► lead_processor ──► leads table (PII encrypted)
                                   rejections table
                                   quality_counters

JSONL events ──► event_processor ──► interaction_events (append-only ledger)
                                     quality_counters

report cmd  ──► sync_calls_table() ──► calls (materialized)
            ──► SQL queries ──► 5 report files
```

### Database Schema (SQLite)

| Table | Purpose | Primary Key |
|---|---|---|
| `ingested_sources` | File-level idempotency tracking (path + SHA-256 hash) | `file_path` |
| `leads` | Validated leads with encrypted PII | `(workflow_id, phone_hash)` |
| `rejections` | Rejected rows with reason | `(workflow_id, batch_id, row_number)` |
| `interaction_events` | Append-only event ledger | `event_id` |
| `calls` | Materialized call-level aggregates | `call_id` |
| `disposition_catalogue` | Terminal/success flags per workflow | `(workflow_id, code)` |
| `quality_counters` | Running quality metric counters | `metric_name` |
| `rejections_by_reason` | Running rejection reason counts | `reason` |

**Why SQLite?** Zero-config, single-file deployment, no Docker required. For evaluation purposes it's the simplest choice that still demonstrates proper schema design.

### PII Protection

Phone numbers are stored three ways — none is plaintext:

| Column | Algorithm | Purpose |
|---|---|---|
| `phone_hash` | HMAC-SHA-256 with secret pepper | Indexed lookups and event-to-lead linking. One-way, can't be reversed. |
| `phone_encrypted` | Fernet (AES-128-CBC) | Reversible decryption for operational needs |
| `phone_masked` | `+91******XXXX` | Display in reports |
| `customer_name_encrypted` | Fernet | Reversible encryption for names |

Events store only `phone_hash` — never the actual phone number. Linking is done via hash equality.

### Materialized Calls Table

SQL views joining 600k+ events were too slow. Instead, `sync_calls_table()` rebuilds a flat `calls` table from the ledger at report time using window functions for effective disposition logic (overrides > sets, latest by `occurred_at` wins). This takes ~0.2s on the small dataset and ~12s on the large dataset, and makes all report queries fast.

## Decisions & assumptions

| Decision | Why | Alternative considered |
|---|---|---|
| SQLite over Postgres | Zero-config for evaluators; demonstrates same schema principles | Docker Compose Postgres (provided, but adds setup friction) |
| Materialized `calls` table instead of SQL views | Views caused O(n²) joins on 600k events; materialized table is 100x faster | Keeping views with query-level optimization |
| HMAC-SHA-256 for phone identity | Deterministic, one-way, and can serve as a JOIN key between leads and events | Encrypting phones and decrypting for every lookup (too slow) |
| Fernet (AES-128-CBC) for reversible PII | Built-in key rotation support, standard in Python `cryptography` library | GPG, NaCl, or raw AES-GCM |
| Events store `phone_hash`, not lead FK | Enables orphan auto-resolution — when a lead arrives later, its events link automatically via hash match | Eager lead lookup at ingest time (breaks when events arrive before leads) |
| Defaults applied after merge, not per-row | Spec says "defaults apply after merging, to fields still null". Applying before merge would overwrite real values from earlier rows. | Apply defaults per-row before merge |
| `event_id` tiebreaker for same-timestamp dispositions | When two disposition events have identical `occurred_at`, pick the one with the lexicographically later `event_id` | Random / undefined behavior |
| In-memory lead cache during batch processing | Reads all existing leads for a workflow into memory before processing; avoids N+1 DB queries. Works for 100k leads. | Individual SQL lookups per row |
| File-level idempotency via path + SHA-256 | Simple and correct. If file content changes, it's treated as a new ingest. | Row-level checksums (more complex, overkill for batch files) |
| `call.initiated` required for a call to count | Spec says "A call is counted only if its `call.initiated` event is present". Events without an initiation are effectively orphaned from call counting. | Count calls from any event |

## Failure modes considered

| Failure | How it's handled |
|---|---|
| Re-running same drop | Idempotent: `ingested_sources` table tracks file path + SHA-256 hash; skips already-ingested files |
| Duplicate events across drops | `event_id` is PRIMARY KEY; duplicates rejected at DB level + in-memory set for same-file dedup |
| Events before their lead | Events store `phone_hash`; linking happens at report time via JOIN. Orphans auto-resolve when lead arrives. |
| Malformed JSON event lines | Caught by `json.loads()` exception; counted as `malformed_events`, skipped |
| Missing `event_id` | Treated as malformed; counted and skipped |
| Unknown disposition codes | Checked against `disposition_catalogue`; counted as `unknown_disposition_events`, event inserted but disposition_code set to NULL |
| Blank CSV rows | Detected (all cells empty/whitespace); skipped without counting |
| Bad phone numbers | 6-format normalisation handles most; invalid phones cause rejection with `invalid_phone` reason |
| Crash mid-ingest | SQLite WAL mode + transactions. Incomplete files won't be in `ingested_sources`, so re-run will re-process them. |
| Concurrent access | SQLite WAL mode supports concurrent reads. Single-writer model assumed. |

**Not handled:** distributed deployment, network failures (not applicable to local pipeline), schema migrations across versions, key rotation for Fernet.

## Testing

70 tests across 8 test files covering:

| Test file | Tests | What's covered |
|---|---|---|
| `test_phone.py` | 14 | Phone normalisation (10 valid, 5 invalid), masking, hashing |
| `test_date_and_decimal.py` | 18 | Date parsing (8 valid, 8 invalid), decimal parsing (10 valid, 6 invalid) |
| `test_schema_and_headers.py` | 4 | Header normalisation, enum validation, string validation, schema registry loading |
| `test_lead_processing.py` | 5 | Blank rows, rejection precedence, field warnings/defaults, duplicate merging, PII protection |
| `test_event_processing.py` | 2 | Timestamp parsing to IST, event processing rules (malformed/duplicate/unknown) |
| `test_dispositions_and_buckets.py` | 2 | Effective disposition override logic, bucket classification (all 4 types) |
| `test_orphan_linking.py` | 1 | Orphan resolution when lead arrives in later drop |
| `test_end_to_end_and_idempotency.py` | 1 | Full pipeline E2E with idempotency verification |

**What's not tested:** Large-scale performance regression, Fernet key rotation, concurrent ingestion. These are stretch/production concerns.

## Results

### Small dataset (seed 42)

| Metric | Value |
|---|---|
| Lead rows total | 2,057 |
| Leads rejected | 108 |
| Duplicates merged | 57 |
| Field warnings | 122 |
| Events total | 8,745 |
| Malformed events | 8 |
| Duplicate events dropped | 237 |
| Orphan events | 461 |
| Unknown dispositions | 19 |

**Timings (small):**
- Ingest drop_1: ~5s
- Ingest drop_2: ~6s
- Report generation: ~2s (including 0.18s for calls table sync)
- Re-ingest (idempotency): instant (all files skipped)

### Surprising findings in the data
- ~5.3% orphan rate (461/8745) — a meaningful fraction of events reference phones not in any lead batch
- Rejection reasons are dominated by `invalid_phone` (42) followed by various `missing_required` fields
- 57 duplicates merged across batches, with later batches overwriting fields (last-write-wins)
- `disposition.override` events lack `phone` and must be linked via `call_id`

## If I had another day

1. **Large-scale benchmark** — run full pipeline on `--scale large` (~140k leads, ~600k events) and optimise to hit the 2-minute target. Batch inserts with `executemany`, reduce Python-side processing.
2. **Incremental reporting** — avoid rebuilding the full `calls` table each time by tracking which events are new since last sync.
3. **Production architecture note** — how to schedule this with Airflow/Cron for 100k leads/day: partition by workflow, add monitoring/alerting on quality metrics, use Postgres with proper connection pooling.
4. **Schema migrations** — use Alembic for database versioning so schema changes don't require rebuilding.
5. **Key rotation** — implement Fernet key rotation with multi-key decryption support.
