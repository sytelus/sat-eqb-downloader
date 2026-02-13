# Architecture Notes

## System Overview
The project uses direct HTTP APIs behind SAT Suite Educator Question Bank and stores all run artifacts under `$OUT_DIR/sat_eqb`.

Core goals:
- deterministic API-based extraction (no browser automation)
- resilient parsing for digital + legacy content
- offline-renderable outputs with local assets
- resumable, restartable execution with full run history

## End-to-End Flow
1. Resolve output root and initialize directories (`dataset/`, `runs/`, `state/`, `todo/`).
2. Load/validate lookup catalog.
3. Build query payload and fetch filtered table rows.
4. Determine candidate set using persistent profile state:
   - skip prior successes
   - include new questions (`download_new`)
   - include prior failures (`download_failed`)
   - or reset profile with `restart`
5. Prefetch digital details in batches.
6. For each candidate:
   - resolve payload source (digital or legacy)
   - inspect payload/key drift and missing metadata
   - parse normalized content + collect parse warnings
   - download/rewrite assets and capture asset anomalies
   - write per-question output
   - persist success/failure state atomically
   - tag persisted record lifecycle (`created_run_id`, `modified_run_id`, `create_time`, `modified_time`)
   - update canonical `dataset.jsonl`
   - write run progress checkpoint
7. Emit run summary, detailed stats, TODO summaries, logs, and history entries.

## Key Modules
- `src/college_board_scraper/core.py`
  - Scraper orchestration, query execution, parse normalization.
  - Request pacing and retry-aware request wrapper.
  - Persistent state handling and run-history writes.
  - Payload drift/missing metadata detection.
  - Global/per-run TODO artifact generation.
  - Frequent `run-progress.json` checkpoint writes.
  - Run statistics and logging.
- `src/college_board_scraper/assets.py`
  - HTML asset extraction and local rewriting.
  - Handles `src`/`href`/`xlink:href`/`data`/`poster`/`srcset`/`style url(...)` and `data:` URIs.
- `src/college_board_scraper/models.py`
  - Normalized output dataclasses.
  - Includes full raw source payload storage.
- `src/college_board_scraper/cli.py`
  - CLI argument parsing and run-mode selection.

## Resumability Model
Resumability is profile-based:
- Profile ID = hash of normalized filter configuration.
- State file = `state/profiles/<profile_id>.json`.
- Question state tracks attempts, status (`success`/`failed`), errors, and output path.

Behavior:
- default: process new + failed, skip successful
- `--only-new`: only unseen
- `--only-failed`: only previously failed
- `--restart`: clear profile state and reprocess from start
- interrupted runs can resume from persisted profile state; run checkpoint file records last processed position.

## Rate-Limit and Reliability Strategy
- Proactive request pacing (`max_requests_per_second`, default `3.0`).
- HTTP retry adapter for transient failures including `429`.
- Retry-After aware retry behavior.
- Per-request endpoint + status code accounting in run stats.
- Question-level failure isolation with optional fail-fast mode.

## Logging and Diagnostics
Per run (`runs/<run_id>/`):
- `run.log`: operational logs and warnings
- `errors.jsonl`: structured per-question failures with traceback
- `run-summary.json`: concise run result
- `run-stats.json`: detailed metrics
- `stats.yaml`: YAML-formatted stats/summary mirror
- `run-progress.json`: crash-safe frequent progress checkpoints
- `todo-items.jsonl` / `todo-summary.json`: anomaly details for the run
- `todo-items.md`: human-readable run TODO report
- `new_ids.csv` / `modified_ids.csv`: dataset deltas for the run

Per question:
- `parse_warnings` captured in output JSON and surfaced in rendered HTML.

Canonical dataset:
- `dataset/questions/<question_dir>/question.json` and `question.html`
- `dataset.jsonl` root snapshot updated on each run
- legacy `data/questions` content is auto-migrated into `dataset/questions`

Global TODO loop (`todo/`):
- `todo-items.jsonl`: append-only anomaly events
- `todo-index.json`: deduplicated signatures with occurrences and samples
- `TODO.md`: human-readable prioritized backlog for self-improvement

## Statistics Coverage
Run stats include:
- timing (query/prefetch/process/total)
- selection (available/requested/candidate/skipped reason counts)
- processing (processed/success/failed)
- source mix (digital/legacy)
- asset counts/bytes by asset source type
- request totals by endpoint and status code
- throttle sleep totals
- TODO anomaly counts by category/severity
- progress checkpoint write count

## Confidence Register
- High confidence: endpoint contracts and filter semantics currently used by site.
- High confidence: resumable behavior and run history persistence.
- High confidence: run-progress checkpoint + profile-state writes prevent loss of successful progress during interrupts/reboots.
- High confidence: long-content capture without truncation.
- Medium confidence: future upstream payload drift (mitigated by raw payload retention + TODO drift alarms).
- Medium confidence: unknown future asset reference patterns outside current parser coverage (mitigated by anomaly TODO events).

## TODOs
1. Add optional schema validation checks against `SCHEMA.md` examples in CI.
2. Add TODO lifecycle management (`open`/`resolved`) tooling and triage CLI.
3. Add configurable parallelism with global request budget control.
4. Add optional content deduplication reports across profiles.
