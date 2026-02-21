# SAT EQB Output Schema

This document defines the persisted schema for data and run artifacts under `$OUT_DIR/sat_eqb`.

## Version Markers
- `schema_version`: run/data artifact schema version (`1.0.0`)
- `state_version`: profile-state schema version (`1.0.0`)
- `index_version`: TODO index schema version (`1.0.0`)
- `progress_version`: run-progress checkpoint schema version (`1.0.0`)

## Root Layout
All generated files are written under:
- `$OUT_DIR/sat_eqb`

Main structure:
- `data/questions/<question_dir>/question.json`
- `data/questions/<question_dir>/question.html`
- `data/questions/<question_dir>/question.md`
- `data/questions/<question_dir>/assets/*`
- `data.jsonl`
- `data-stats.json`
- `runs/<run_id>/*`
- `state/profiles/<profile_id>.json`
- `state/latest-run.json`
- `history.jsonl`
- `todo/TODO.md`
- `todo/todo-index.json`
- `todo/todo-items.jsonl`

Compatibility migration:
- Legacy paths from older versions are migrated into canonical `data/*` locations.

## Observed Dataset Profile (Sampled on 2026-02-14)
This section documents observed values from the current downloaded dataset (`3268` questions). These are observations, not hard guarantees.

- `source`: `digital` (2809), `legacy` (459)
- `metadata.assessment`: `SAT`
- `metadata.assessment_id`: `99`
- `metadata.test`: `Math`, `Reading and Writing`
- `metadata.test_id`: `2` for Math, `1` for Reading and Writing
- `metadata.difficulty`: `Easy`, `Medium`, `Hard`
- `content.question_type`: `mcq`, `Multiple Choice`, `spr`, `SPR`
- `metadata.score_band_range`: integer `1..7`
- `metadata.question_id`: 8-char lowercase hex strings (observed)
- `metadata.external_id`: UUID (digital) or null
- `metadata.ibn`: legacy identifier or null (mixed formats observed)
- `assets.source_type`: `data_uri` (observed in this dataset)
- `assets.mime_type`: `image/png` (observed in this dataset)
- `state_standards`: non-empty list in current dataset; 115 unique standard IDs observed

## Entity Relationships
- One logical question record is stored in:
  - `data/questions/<question_dir>/question.json` (canonical per-question payload)
  - one line in `data.jsonl` (global dataset index snapshot)
- `data.jsonl` contains `question_key` for stable indexing:
  - `external:<external_id>` for digital
  - `ibn:<ibn>` for legacy
  - fallback `question:<question_id>`
- `lifecycle` tracks creation and last-modification run lineage.

## Canonical Question Payload

### `data/questions/<question_dir>/question.json`
Type: JSON object

Top-level fields:

| Field | Type | Meaning | Observed constraints |
| --- | --- | --- | --- |
| `question_key` | string (optional in per-file, always present in `data.jsonl`) | stable dataset key | `external:*`, `ibn:*`, fallback `question:*` |
| `metadata` | object | normalized metadata | required |
| `source` | string enum | source family | `digital`, `legacy` |
| `content` | object | normalized question content | required |
| `assets` | array | downloaded/relinked assets | empty allowed |
| `parse_warnings` | array of strings | parser warnings for this item | empty is common |
| `raw_table_row` | object | raw row from `digital/get-questions` | preserved for drift analysis |
| `raw_detail_payload` | object | raw detail payload from source endpoint | preserved for drift analysis |
| `raw_payload` | object | backward-compatible alias of `raw_detail_payload` | expected equal to `raw_detail_payload` |
| `lifecycle` | object | creation/modification lineage | expected complete for persisted rows |

#### `metadata`

| Field | Type | Meaning | Observed constraints |
| --- | --- | --- | --- |
| `question_id` | string | displayed question identifier | observed 8-char lowercase hex |
| `assessment` | string enum | assessment label | observed `SAT` |
| `assessment_id` | integer enum | assessment numeric ID | observed `99` |
| `test` | string enum | test section | observed `Math`, `Reading and Writing` |
| `test_id` | integer enum | test numeric ID | observed `2` (Math), `1` (RW) |
| `domain` | string enum (8 observed) | high-level skill domain | see domain table below |
| `domain_code` | string enum (8 observed) | domain code | see domain table below |
| `skill` | string | skill description | may include trailing whitespace in source |
| `skill_code` | string | skill code (e.g. `P.B.`) | 29 unique observed |
| `difficulty` | string enum | normalized difficulty | `Easy`, `Medium`, `Hard` |
| `score_band_range` | integer | score-band proxy | observed `1..7` |
| `external_id` | string or null | digital ID | UUID when present |
| `ibn` | string or null | legacy ID | legacy-only when present |
| `program` | string or null | program label | observed `SAT` |
| `create_date` | integer epoch-ms or null | source create timestamp | observed 2023-08-02 to 2025-08-13 UTC |
| `update_date` | integer epoch-ms or null | source update timestamp | observed 2023-08-02 to 2025-08-13 UTC |
| `state_standards` | array of strings | standards aligned to the skill | non-empty in current dataset |
| `original_url` | string or null | official-site reference URL for locating the question | generated by downloader; see confidence note |

Observed domain mapping:

| `domain` | `domain_code` |
| --- | --- |
| `Algebra` | `H` |
| `Advanced Math` | `P` |
| `Problem-Solving and Data Analysis` | `Q` |
| `Geometry and Trigonometry` | `S` |
| `Information and Ideas` | `INI` |
| `Craft and Structure` | `CAS` |
| `Expression of Ideas` | `EOI` |
| `Standard English Conventions` | `SEC` |

#### `content`

| Field | Type | Meaning | Observed constraints |
| --- | --- | --- | --- |
| `prompt_html` | string | pre-stem passage/prompt HTML | may be empty |
| `stem_html` | string | main question body HTML | can be very large (100k+ chars observed) |
| `answer_options` | array of objects | choice list | 0..4 observed |
| `rationale_html` | string | explanation HTML | may be empty |
| `correct_answers` | array of strings | answer key values | 0..3 observed |
| `question_type` | string or null | source question style/type | casing differs by source (`mcq` vs `Multiple Choice`, `spr` vs `SPR`) |

`answer_options[]` object:
- `letter` string (e.g., `A`, `B`, `C`, `D`)
- `content_html` string

#### `assets[]`

| Field | Type | Meaning |
| --- | --- | --- |
| `original_url` | string | original asset URL or data URI reference |
| `local_path` | string | relative path under question dir (typically `assets/...`) |
| `source_type` | string enum | `remote` or `data_uri` |
| `mime_type` | string or null | MIME type if known |
| `size_bytes` | integer or null | asset size |
| `sha256` | string or null | file digest |

#### `lifecycle`

| Field | Type | Meaning |
| --- | --- | --- |
| `created_run_id` | string | run that first created persisted record |
| `modified_run_id` | string | last run that changed normalized payload |
| `create_time` | ISO8601 string | first persistence timestamp |
| `modified_time` | ISO8601 string | last change timestamp |

### `data/questions/<question_dir>/question.html`
Type: HTML document

Contains:
- metadata table
- rendered prompt/stem/answers/rationale
- parse warning section (if any)
- links rewritten to local assets

### `data/questions/<question_dir>/question.md`
Type: Markdown document

Contains:
- original URL at top
- question content in readable markdown
- LaTeX-style math markers (`$...$` / `$$...$$`) where conversion is possible
- answer choices / response input marker
- correct answer and rationale
- metadata section at end

## Dataset Index and Global Stats

### `data.jsonl`
Type: JSON Lines

- one full question payload per line
- includes top-level `question_key`
- updated after each run and by maintenance backfill tools

### `data-stats.json`
Type: JSON object

Fields:
- `schema_version` string
- `generated_at_utc` string
- `total_records` integer
- `by_assessment` object
- `by_test` object
- `by_domain` object
- `by_difficulty` object
- `by_source` object
- `by_question_type` object
- `assets` object:
  - `records_with_assets` integer
  - `total_assets` integer
  - `total_asset_bytes` integer
  - `by_source_type` object
  - `by_mime_type` object
- `parsing` object:
  - `records_with_warnings` integer
  - `warnings_total` integer
- `metadata_completeness` object:
  - `records_with_original_url` integer
  - `records_missing_original_url` integer
- `lifecycle` object:
  - `created_run_ids_count` integer
  - `modified_run_ids_count` integer
  - `latest_modified_time` string or null

## Run Artifacts

### `runs/<run_id>/run-config.json`
Type: JSON object

Includes:
- run identity (`run_id`, optional `run_label`, `profile_id`)
- filter snapshot (`filters`)
- flag snapshot (`restart`, `download_new`, `download_failed`, `download_assets`, etc.)
- amount and rate-limit settings

### `runs/<run_id>/run-summary.json`
Type: JSON object

High-level run result including:
- status (`completed`, `completed_with_errors`, `interrupted`, `failed`)
- timing
- selection and processing counters
- request/asset/byte stats
- dataset change counters (`new`, `modified`, `unchanged`)
- TODO summary pointers
- global dataset stats snapshot

### `runs/<run_id>/run-stats.json`
Type: JSON object

Detailed stats blocks:
- `run`, `timing`, `selection`, `processing`, `requests`, `sources`
- `question_breakdown` (attempted/success/failed by domain/difficulty/source/type)
- `assets`, `bytes`, `dataset`, `parsing`, `todo`, `progress`

`bytes` includes:
- `record_json_total`
- `content_html_total`
- `raw_table_row_total`
- `raw_detail_payload_total`
- `written_question_json_total`
- `written_question_html_total`
- `written_question_markdown_total`

### `runs/<run_id>/stats.yaml`
Type: YAML document

Contains:
- `run_summary` (same logical content as `run-summary.json`)
- `run_stats` (same logical content as `run-stats.json`)

### `runs/<run_id>/run-progress.json`
Type: JSON object

Frequent checkpoint with:
- stage (`initialized`, `selected`, `processing`, `interrupted`, `completed`, `completed_with_errors`, `failed`)
- rolling counters and current question pointer

### `runs/<run_id>/errors.jsonl`
Type: JSON Lines

Per failure event:
- `timestamp_utc`, `question_key`, `question_id`, `error`, `traceback`

### `runs/<run_id>/new_ids.csv` and `runs/<run_id>/modified_ids.csv`
Type: CSV

Columns:
- `question_key`
- `question_id`
- `external_id`
- `ibn`

### `runs/<run_id>/run.log`
Type: plain text log

Contains run execution logs, warning/error traces, and operational details.

### TODO files under run directory
- `runs/<run_id>/todo-items.jsonl`
- `runs/<run_id>/todo-summary.json`
- `runs/<run_id>/todo-items.md`

## State / Resume Files

### `state/profiles/<profile_id>.json`
Type: JSON object

Per-profile resumable state:
- filter snapshot and question status map
- per-question attempt/success/failure counters
- last run/error timestamps
- `data_path` pointer for successful items

### `state/latest-run.json`
Type: JSON object

Pointer to latest run and top-line counters.

### `history.jsonl`
Type: JSON Lines

Append-only run history summary stream.

## Global TODO Backlog

### `todo/todo-index.json`
Type: JSON object

Deduplicated TODO signature index with occurrence counters and sample references.

### `todo/todo-items.jsonl`
Type: JSON Lines

Append-only TODO event stream.

### `todo/TODO.md`
Type: Markdown

Human-readable backlog derived from TODO index.

## Confidence and Known Gaps

### `metadata.original_url`
- Confidence: **medium**
- What is known:
  - URL is deterministic and points to official College Board web app route (`/digital/results` or `/results`) with identifying query parameters.
- What is uncertain:
  - Current SPA does not expose a stable direct deep-link API that opens a specific question modal by URL alone.
- TODO:
  - Re-check frontend behavior periodically for newly introduced deep-link support and upgrade this field to a true deep-link if available.

### `content.question_type` normalization
- Confidence: **high** about semantic equivalence, **medium** about upstream canonical casing.
- Observation:
  - Equivalent values arrive with source-dependent casing (`mcq` vs `Multiple Choice`, `spr` vs `SPR`).
- TODO:
  - Optionally add a normalized companion field if downstream consumers need strict enum consistency.

### `raw_*` payload drift
- Confidence: **high** that raw payloads are preserved; **variable** for future unknown keys.
- TODO:
  - Continue triaging TODO backlog signatures for schema drift and extend normalized fields where useful.
