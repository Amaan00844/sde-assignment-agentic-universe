# AI Usage Disclosure

## Tools used
Google Gemini (via Antigravity IDE)

## What I used them for
- **Scaffolding** — initial project structure (file layout, CLI argument parsing, config setup)
- **Boilerplate code** — SQLite connection management, Fernet encryption/decryption wrappers, CSV reader setup
- **Phone normalisation regex** — first draft of the 6-format phone parsing logic
- **SQL queries** — the CTE-based `sync_calls_table` query for computing effective dispositions with window functions
- **Test scaffolding** — parametrized pytest structure for phone, date, and decimal tests
- **Debugging** — diagnosing the defaults-before-merge bug that was overwriting existing field values

## What I wrote or decided myself
- **Overall architecture** — the decision to use a materialized `calls` table instead of SQL views, the three-phase pipeline design (ingest leads → ingest events → report)
- **PII strategy** — the HMAC-SHA-256 + Fernet dual-storage approach for phones, and the decision to store `phone_hash` on events for lazy linking
- **Orphan resolution design** — storing `phone_hash` at event ingestion and linking via JOIN at report time, so orphans auto-resolve when leads arrive later
- **Edge case handling** — rejection precedence (schema-order first-failure), blank row detection, duplicate merge semantics (last-write-wins, batch_id locked to first)
- **Data model** — all 8 table designs, index selection, the decision to track quality counters persistently rather than recompute
- **Performance diagnosis** — identifying that SQL views were the bottleneck on large data, and the materialized table solution

## Where the AI was wrong or unhelpful
- **Defaults applied before merging** — AI initially generated code that applied defaults (e.g., `preferred_language → en`) to each row before merging duplicates. This caused a later duplicate row to overwrite a real value (e.g., `hi`) with the default (`en`). I caught this by inspecting test output where language values were wrong, and restructured the flow to apply defaults only after all merging is complete.
- **File handle closed before reading** — after adding the in-memory lead cache optimisation, the AI generated code that closed the CSV file handle before iterating rows. The `csv.DictReader` is lazy, so this produced empty results. I fixed it by moving the processing loop inside the `with open()` block.

## How I verified AI-generated code
- **70 parametrized tests** covering phone normalisation, date/decimal parsing, header matching, lead processing, event processing, disposition logic, orphan resolution, and end-to-end pipeline with idempotency
- **Manual inspection** of all 5 report files against the spec's output contract
- **Idempotency verification** — re-running ingest and confirming all files skipped
- **Line-by-line reading** of critical sections: PII handling, duplicate merge logic, effective disposition SQL, and orphan counting
