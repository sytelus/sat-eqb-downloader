# SAT Educator Question Bank Downloader (College Board)

Python library and CLI for downloading SAT Suite Educator Question Bank content and rendering assets directly from College Board APIs.

## Key Features
- No Selenium, Firefox, or browser drivers.
- API-driven retrieval from the same backend endpoints used by the official website.
- Full HTML capture for long questions and long rationales (no screenshot truncation).
- Per-question markdown export (`question.md`) with readable structure and math rendered into LaTeX-style markers.
- Downloads and rewrites assets for offline rendering:
  - images
  - diagrams/charts
  - `data:` URI media
  - style URL references and `srcset`
- Supports both source families:
  - digital questions (`external_id`)
  - legacy disclosed questions (`ibn`)
- Resumable downloads by filter profile with frequent progress checkpoints.
- Persistent run history and statistics.
- Retry of previously failed content.
- Request pacing for rate-limit friendliness.
- Structured run logs and per-question parse warnings.
- Runtime anomaly detection with persistent TODO backlog for parser/schema improvements.
- Adds `metadata.original_url` reference for each question payload.

## Ubuntu 24.04 Compatibility
Works on Ubuntu 24.04 with standard Python tooling.

Requirements:
- Python 3.9+
- `pip`

Install:
```bash
python3 -m pip install -e .
```

## Dependencies
Version-ranged dependencies are used (no exact pins):
- `requests>=2.31.0,<3.0.0`
- `beautifulsoup4>=4.12.0,<5.0.0`

## Output Location
All run history and data are written under:
- `$OUT_DIR/sat_eqb`

Behavior:
- If `--output-dir` is supplied, that path is treated as `OUT_DIR`.
- Otherwise environment variable `OUT_DIR` is used.
- If `OUT_DIR` is not set, current directory is used.

Generated structure:
- `sat_eqb/data/questions/...`
- `sat_eqb/data.jsonl` (main dataset, updated after each run)
- `sat_eqb/data-stats.json` (global current-state dataset stats)
- `sat_eqb/runs/<run_id>/...` where `<run_id>` is UTC date-time stamped
- `sat_eqb/state/profiles/<profile_id>.json`
- `sat_eqb/state/latest-run.json`
- `sat_eqb/history.jsonl`
- `sat_eqb/todo/TODO.md`
- `sat_eqb/todo/todo-index.json`
- `sat_eqb/todo/todo-items.jsonl`

Compatibility note:
- Existing legacy question folders under `sat_eqb/dataset/questions` are migrated into `sat_eqb/data/questions` on subsequent runs.

Per-question files under `sat_eqb/data/questions/<question_dir>/`:
- `question.json`
- `question.html`
- `question.md`
- `assets/*`

## Schema
See [`SCHEMA.md`](SCHEMA.md) for complete, up-to-date file schemas for:
- per-question outputs
- run summaries and statistics
- profile resume state
- run history records

## Python API Usage
```python
from college_board_scraper import Scraper, ScraperAmount

scraper = Scraper(
    assessment="SAT",
    test="Math",
    options={"Algebra", "Advanced Math"},
    difficulties={"Easy", "Medium", "Hard"},
    skills={
        "Algebra": {"Linear equations in one variable", "Linear functions"},
        "Advanced Math": {"Equivalent expressions"},
    },
    exclude_active_questions=True,
    state="CA",
    max_requests_per_second=3.0,
)

records = scraper.scrape(
    amount=25,
    output_dir="/tmp/out",         # data written under /tmp/out/sat_eqb
    restart=False,                 # resume by default
    download_new=True,             # default
    download_failed=True,          # default
    download_assets=True,
    save_output=True,
    continue_on_error=True,
)

print(len(records))
print(scraper.last_run_summary["status"])
```

## CLI Usage
Basic example:
```bash
college-board-scraper \
  --assessment "SAT" \
  --test "Math" \
  --option "Algebra" \
  --option "Advanced Math" \
  --amount 25
```

Example with skills, difficulty, state standards, and active-item exclusion:
```bash
college-board-scraper \
  --assessment "SAT" \
  --test "Math" \
  --option "Algebra" \
  --option "Advanced Math" \
  --difficulty "Easy" \
  --difficulty "Medium" \
  --difficulty "Hard" \
  --skill "Algebra:Linear equations in one variable,Linear functions" \
  --skill "Advanced Math:Equivalent expressions" \
  --exclude-active-questions \
  --state CA \
  --include-state-standards \
  --amount all
```

Run only previously failed content for a profile:
```bash
college-board-scraper \
  --assessment "SAT" \
  --test "Math" \
  --option "Algebra" \
  --only-failed
```

Restart from scratch for a profile (ignore prior progress):
```bash
college-board-scraper \
  --assessment "SAT" \
  --test "Math" \
  --option "Algebra" \
  --restart
```

## Complete CLI Argument Reference
- `--assessment <name>`:
  - Required.
  - Valid values come from live lookup endpoint (for example `SAT`).
- `--test <name>`:
  - Required.
  - Valid values include `Reading and Writing` and `Math`.
- `--option <domain>`:
  - Required, repeatable.
  - Domain filters within selected test.
- `--difficulty <Easy|Medium|Hard>`:
  - Optional, repeatable.
  - Difficulty filter.
- `--skill "<Domain>:<skill1>,<skill2>,..."`:
  - Optional, repeatable.
  - Skill-level filter by domain.
- `--state <state code or state name>`:
  - Optional.
  - Enables state standards enrichment when combined with `--include-state-standards`.
- `--amount <N|all>`:
  - Optional, default `all`.
  - Max number of rows to process after filtering.
- `--exclude-active-questions`:
  - Optional flag.
  - Excludes currently active digital-test items from result set.
- `--output-dir <path>`:
  - Optional.
  - Base output directory (`OUT_DIR`); actual output root is `<path>/sat_eqb`.
- `--download-new/--no-download-new`:
  - Optional, default `--download-new`.
  - Include/exclude content not yet successfully downloaded in profile state.
- `--download-failed/--no-download-failed`:
  - Optional, default `--download-failed`.
  - Include/exclude content that failed in prior profile runs.
- `--only-new`:
  - Convenience alias for `--download-new --no-download-failed`.
- `--only-failed`:
  - Convenience alias for `--no-download-new --download-failed`.
- `--restart`:
  - Optional flag.
  - Clears saved profile progress and starts from beginning.
- `--max-requests-per-second <float>`:
  - Optional, default `3.0`.
  - Proactive request pacing to respect endpoint rate limits.
- `--run-label <text>`:
  - Optional.
  - Included in run ID/history for easier traceability.
- `--include-state-standards/--no-include-state-standards`:
  - Optional, default `--include-state-standards`.
  - Toggle state standards fetch when state is set.
- `--download-assets/--no-download-assets`:
  - Optional, default `--download-assets`.
  - Toggle asset downloads and local link rewriting.
- `--save-output/--no-save-output`:
  - Optional, default `--save-output`.
  - Toggle per-question JSON/HTML output persistence.
- `--continue-on-error/--no-continue-on-error`:
  - Optional, default `--continue-on-error`.
  - Continue processing after question-level failures or stop immediately.

## Resumability and Retry Behavior
- Progress is tracked per filter profile in `state/profiles/<profile_id>.json`.
- Run checkpoint state is updated frequently in `runs/<run_id>/run-progress.json`.
- By default, each new run:
  - skips already successful question downloads,
  - retries previously failed questions,
  - downloads newly discovered questions.
- `--restart` clears profile progress and reprocesses from the beginning.
- If interrupted (for example Ctrl+C), a subsequent run resumes from persisted successful progress and retries pending/failed items according to flags.

## Rate-Limit Handling
- Proactive pacing via `--max-requests-per-second` (default `3.0`).
- Request retries configured for transient errors and `429` responses.
- `Retry-After` is respected by HTTP retry logic.

## Logging and Reliability Diagnostics
Each run writes diagnostics to:
- `runs/<run_id>/run.log`
- `runs/<run_id>/errors.jsonl`
- `runs/<run_id>/run-summary.json`
- `runs/<run_id>/run-stats.json`
- `runs/<run_id>/stats.yaml`
- `runs/<run_id>/run-progress.json`
- `runs/<run_id>/todo-items.jsonl`
- `runs/<run_id>/todo-summary.json`
- `runs/<run_id>/todo-items.md`
- `runs/<run_id>/new_ids.csv`
- `runs/<run_id>/modified_ids.csv`

Parse irregularities and unexpected source formats are captured as:
- `parse_warnings` in each `question.json`
- warning/error logs in `run.log`
- persistent TODO backlog in `sat_eqb/todo/TODO.md`

Run stats include, at minimum:
- attempted/success/failed question counts
- failure counts by exception type
- counts by domain/category, difficulty, source, and question type
- asset counts and bytes
- payload/output byte totals
- request totals, status codes, endpoint counts, and response-byte totals
- dataset delta counts (`new`, `modified`, `unchanged`)

## Self-Improvement TODO Loop
When the scraper encounters unanticipated situations, it records them as TODO items instead of silently dropping context:
- new/unrecognized source payload fields
- missing metadata fields
- unsupported or unknown asset URL/type patterns
- parse warnings and malformed content blocks
- complex HTML/media layout patterns that may need renderer updates

Each TODO item includes:
- timestamp, run ID, profile ID
- category, severity, summary, recommended action
- question context (`question_key`, `question_id`) when available
- structured details payload and deterministic signature for deduplication

Artifacts:
- Global backlog: `sat_eqb/todo/TODO.md`
- Structured global index: `sat_eqb/todo/todo-index.json`
- Append-only TODO event stream: `sat_eqb/todo/todo-items.jsonl`
- Per-run TODO stream: `sat_eqb/runs/<run_id>/todo-items.jsonl`
- Per-run TODO summary: `sat_eqb/runs/<run_id>/todo-summary.json`
- Per-run markdown TODO report: `sat_eqb/runs/<run_id>/todo-items.md`

Run summaries prominently include TODO counts and pointers so triage can happen immediately after every run.

## Metadata and Source Completeness
Each question output includes:
- normalized metadata fields for downstream usage
- canonical source-site reference URL (`metadata.original_url`)
- full raw table row payload (`raw_table_row`)
- full raw detail payload (`raw_detail_payload`)
- compatibility alias (`raw_payload`)
- lifecycle tracking tags:
  - `lifecycle.created_run_id`
  - `lifecycle.modified_run_id`
  - `lifecycle.create_time`
  - `lifecycle.modified_time`

This preserves complete source data while providing a stable normalized model.

Original URL note:
- Current College Board SPA does not expose a guaranteed direct deep-link that always opens a specific question modal by URL alone.
- `metadata.original_url` therefore points to the canonical official route plus identifying query parameters so the question context can be reproduced and located reliably.

The root-level main dataset file `sat_eqb/data.jsonl` is updated after each run and includes these lifecycle tags for every record.

Global dataset-state stats are maintained continuously in:
- `sat_eqb/data-stats.json`

These global stats cover current dataset totals and breakdowns by assessment, test, domain/category, difficulty, source, question type, asset details, and parse-warning prevalence.

## Testing
Fast local tests:
```bash
pytest -q
```

Live integration tests:
```bash
RUN_LIVE_TESTS=1 pytest -q tests/test_live_integration.py
```

## Post-Run Maintenance and Analytics
Backfill existing downloaded dataset with:
- `metadata.original_url`
- regenerated `question.md` for every question
- refreshed `data-stats.json`

```bash
python3 scripts/backfill_dataset_outputs.py --root-dir /path/to/out/sat_eqb
```

Generate standards distribution report and chart assets:
```bash
python3 scripts/generate_state_standards_report.py \
  --root-dir /path/to/out/sat_eqb \
  --report-path state_standards.md \
  --assets-dir report_assets
```

This writes:
- `state_standards.md` (analysis report)
- `report_assets/*.svg` (charts referenced by the report)

## Documentation Map
- Architecture and implementation notes: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
- Output/run schema: [`SCHEMA.md`](SCHEMA.md)
- Standards analysis report (generated): [`state_standards.md`](state_standards.md)
