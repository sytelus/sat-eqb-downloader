from __future__ import annotations

import hashlib
import html
import json
import logging
import os
import re
import shutil
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .assets import AssetDownloader
from .helpers import ScraperAmount, chunked
from .markdown_export import render_question_markdown
from .models import AnswerOption, QuestionContent, QuestionMetadata, QuestionRecord
from .urls import build_original_question_url

LOGGER = logging.getLogger(__name__)

SCHEMA_VERSION = "1.0.0"
PROFILE_STATE_VERSION = "1.0.0"
TODO_INDEX_VERSION = "1.0.0"
RUN_PROGRESS_VERSION = "1.0.0"


class Scraper:
    """Download SAT Suite Educator Question Bank content without browser automation."""

    # Backwards-compatible public constants. Live validation still uses lookup API.
    valid_assessments: Tuple[str, ...] = ("SAT", "PSAT/NMSQT & PSAT 10", "PSAT 8/9")
    valid_tests: Tuple[str, ...] = ("Reading and Writing", "Math")

    valid_reading_and_writing_options: Set[str] = {
        "Information and Ideas",
        "Craft and Structure",
        "Expression of Ideas",
        "Standard English Conventions",
    }
    valid_math_options: Set[str] = {
        "Algebra",
        "Advanced Math",
        "Problem-Solving and Data Analysis",
        "Geometry and Trigonometry",
    }

    valid_difficulty_options: Set[str] = {"Easy", "Medium", "Hard"}

    valid_reading_and_writing_skills: Dict[str, Set[str]] = {
        "Information and Ideas": {"Central Ideas and Details", "Inferences", "Command of Evidence"},
        "Craft and Structure": {"Words in Context", "Text Structure and Purpose", "Cross-Text Connections"},
        "Expression of Ideas": {"Rhetorical Synthesis", "Transitions"},
        "Standard English Conventions": {"Boundaries", "Form, Structure, and Sense"},
    }
    valid_math_skills: Dict[str, Set[str]] = {
        "Algebra": {
            "Linear equations in one variable",
            "Linear functions",
            "Linear equations in two variables",
            "Systems of two linear equations in two variables",
            "Linear inequalities in one or two variables",
        },
        "Advanced Math": {
            "Nonlinear functions",
            "Nonlinear equations in one variable and systems of equations in two variables",
            "Equivalent expressions",
        },
        "Problem-Solving and Data Analysis": {
            "Ratios, rates, proportional relationships, and units",
            "Percentages",
            "One-variable data: Distributions and measures of center and spread",
            "Two-variable data: Models and scatterplots",
            "Probability and conditional probability",
            "Inference from sample statistics and margin of error",
            "Evaluating statistical claims: Observational studies and experiments",
        },
        "Geometry and Trigonometry": {
            "Area and volume",
            "Lines, angles, and triangles",
            "Right triangles and trigonometry",
            "Circles",
        },
    }

    LOOKUP_URL = "https://qbank-api.collegeboard.org/msreportingquestionbank-prod/questionbank/lookup"
    GET_QUESTIONS_URL = (
        "https://qbank-api.collegeboard.org/msreportingquestionbank-prod/questionbank/digital/get-questions"
    )
    GET_QUESTION_URL = (
        "https://qbank-api.collegeboard.org/msreportingquestionbank-prod/questionbank/digital/get-question"
    )
    PDF_DOWNLOAD_URL = "https://qbank-api.collegeboard.org/msreportingquestionbank-prod/questionbank/pdf-download"
    STATE_STANDARDS_URL = (
        "https://qbank-api.collegeboard.org/msreportingquestionbank-prod/questionbank/state-standards"
    )
    LEGACY_DISCLOSED_BASE_URL = "https://saic.collegeboard.org/disclosed"
    WEBSITE_BASE_URL = "https://satsuitequestionbank.collegeboard.org/"

    _DIFFICULTY_TO_CODE = {"Easy": "E", "Medium": "M", "Hard": "H"}
    _CODE_TO_DIFFICULTY = {value: key for key, value in _DIFFICULTY_TO_CODE.items()}
    _PDF_BATCH_SIZE = 50

    _EXPECTED_TABLE_ROW_KEYS: Set[str] = {
        "createDate",
        "difficulty",
        "external_id",
        "ibn",
        "pPcc",
        "primary_class_cd",
        "primary_class_cd_desc",
        "program",
        "questionId",
        "score_band_range_cd",
        "skill_cd",
        "skill_desc",
        "uId",
        "updateDate",
    }

    _EXPECTED_DIGITAL_DETAIL_KEYS: Set[str] = {
        "answerOptions",
        "correct_answer",
        "externalid",
        "keys",
        "origin",
        "parenttemplateid",
        "parenttemplatename",
        "position",
        "rationale",
        "stem",
        "stimulus",
        "templateclusterid",
        "templateclustername",
        "templateid",
        "type",
        "vaultid",
    }
    _EXPECTED_DIGITAL_ANSWER_OPTION_KEYS: Set[str] = {"content", "id"}

    _EXPECTED_LEGACY_DETAIL_KEYS: Set[str] = {"answer", "body", "item_id", "prompt", "section"}
    _EXPECTED_LEGACY_ANSWER_KEYS: Set[str] = {"choices", "correct_choice", "correct_spr", "rationale", "style"}
    _EXPECTED_LEGACY_CHOICE_KEYS: Set[str] = {"body"}
    _EXPECTED_LEGACY_CORRECT_SPR_KEYS: Set[str] = {"absolute", "rationale"}
    _FOOTNOTE_REF_NAME_PATTERN = r"(?:^fn|footnote|^note)"
    _INSTRUCTION_STRUCTURE_PATTERNS: Tuple[str, ...] = (
        r"<h[1-6][^>]*>\s*instructions?\s*:?\s*</h[1-6]>",
        r"<(?:p|div|li|span)[^>]*>\s*(?:<strong>\s*)?instructions?\s*:",
    )

    def __init__(
        self,
        assessment: str,
        test: str,
        options: Set[str],
        difficulties: Optional[Set[str]] = None,
        skills: Optional[Dict[str, Set[str]]] = None,
        exclude_active_questions: bool = False,
        state: Optional[str] = None,
        output_dir: str | Path | None = None,
        request_timeout: float = 30.0,
        max_retries: int = 5,
        max_requests_per_second: float = 3.0,
    ) -> None:
        self.assessment = assessment
        self.test = test
        self.options = set(options)
        self.difficulties = set(difficulties) if difficulties else None
        self.skills = {key: set(value) for key, value in (skills or {}).items()} if skills else None
        self.exclude_active_questions = exclude_active_questions
        self.output_dir = Path(output_dir) if output_dir else Path(os.getenv("OUT_DIR", "."))
        self.request_timeout = request_timeout
        self.max_requests_per_second = max_requests_per_second

        self._request_lock = threading.Lock()
        self._last_request_monotonic = 0.0
        if self.max_requests_per_second <= 0:
            self._min_request_interval_seconds = 0.0
        else:
            self._min_request_interval_seconds = 1.0 / self.max_requests_per_second

        self._run_stats: Dict[str, Any] = {}

        self._session = self._build_session(max_retries=max_retries)
        self._lookup_payload = self._fetch_lookup_payload()
        self._assessment_id_by_name = {
            entry["text"]: int(entry["id"]) for entry in self._lookup_payload["lookupData"]["assessment"]
        }
        self._test_id_by_name = {
            entry["text"]: int(entry["id"]) for entry in self._lookup_payload["lookupData"]["test"]
        }
        self._domain_catalog = self._build_domain_catalog(self._lookup_payload)
        self._state_selection = self._resolve_state_selection(state)

        self._validate_filters()

        self.last_errors: List[str] = []
        self.last_run_summary: Dict[str, Any] = {}
        self._active_run_context: Dict[str, Any] = {}
        self._active_todo_index: Dict[str, Any] = {"index_version": TODO_INDEX_VERSION, "items": {}}
        self._active_run_todo_counts: Dict[str, int] = {"total_items": 0, "new_signatures": 0}
        self._active_run_todo_signatures: Set[str] = set()
        self._active_run_todo_category_counts: Dict[str, int] = {}
        self._active_run_todo_severity_counts: Dict[str, int] = {}

    def scrape(
        self,
        amount: int | ScraperAmount,
        save_images: bool = False,
        *,
        output_dir: Optional[str | Path] = None,
        save_output: bool = True,
        download_assets: bool = True,
        continue_on_error: bool = True,
        include_state_standards: bool = True,
        restart: bool = False,
        download_new: bool = True,
        download_failed: bool = True,
        run_label: Optional[str] = None,
    ) -> List[QuestionRecord]:
        """Download and parse questions using the active filters.

        Notes:
        - `save_images` is retained for backward compatibility and maps to
          `save_output=True, download_assets=True`.
        - Progress is persisted per filter profile under `$OUT_DIR/sat_eqb/state`.
        - Runs are resumable by default unless `restart=True`.
        """

        if save_images:
            save_output = True
            download_assets = True

        if not download_new and not download_failed and not restart:
            raise ValueError("At least one of download_new/download_failed must be True unless restart=True")

        run_started_at = _utc_now_iso()
        run_started_monotonic = time.monotonic()

        root_dir = self._resolve_root_dir(output_dir)
        paths = self._ensure_output_layout(root_dir)

        profile_filters = self._build_profile_filters(include_state_standards=include_state_standards)
        profile_id = self._build_profile_id(profile_filters)
        profile_state_path = paths["profiles_dir"] / f"{profile_id}.json"

        run_id = self._build_run_id(run_label)
        run_dir = paths["runs_dir"] / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        run_log_path = run_dir / "run.log"
        run_errors_path = run_dir / "errors.jsonl"
        run_stats_yaml_path = run_dir / "stats.yaml"
        run_todo_markdown_path = run_dir / "todo-items.md"
        run_new_ids_path = run_dir / "new_ids.csv"
        run_modified_ids_path = run_dir / "modified_ids.csv"
        run_todo_items_path = run_dir / "todo-items.jsonl"
        run_todo_summary_path = run_dir / "todo-summary.json"
        run_progress_path = run_dir / "run-progress.json"

        todo_items_path = paths["todo_dir"] / "todo-items.jsonl"
        todo_index_path = paths["todo_dir"] / "todo-index.json"
        todo_markdown_path = paths["todo_dir"] / "TODO.md"
        data_path = paths["data_path"]
        data_stats_path = paths["data_stats_path"]

        dataset_index = self._load_dataset_index(
            data_path=data_path,
            questions_dir=paths["questions_dir"],
        )
        new_dataset_rows: List[Dict[str, str]] = []
        modified_dataset_rows: List[Dict[str, str]] = []

        log_handler = self._attach_run_log_handler(run_log_path)

        self._run_stats = self._initial_run_stats(
            run_id=run_id,
            run_label=run_label,
            root_dir=root_dir,
            profile_id=profile_id,
            restart=restart,
            save_output=save_output,
            download_assets=download_assets,
            download_new=download_new,
            download_failed=download_failed,
            continue_on_error=continue_on_error,
            initial_dataset_count=len(dataset_index),
        )
        self.last_errors = []
        self._active_todo_index = self._load_todo_index(todo_index_path)
        self._active_run_todo_counts = {"total_items": 0, "new_signatures": 0}
        self._active_run_todo_signatures = set()
        self._active_run_todo_category_counts = {}
        self._active_run_todo_severity_counts = {}
        self._active_run_context = {
            "run_id": run_id,
            "profile_id": profile_id,
            "todo_items_path": todo_items_path,
            "run_todo_items_path": run_todo_items_path,
            "todo_index_path": todo_index_path,
            "todo_markdown_path": todo_markdown_path,
            "run_todo_summary_path": run_todo_summary_path,
            "run_progress_path": run_progress_path,
        }
        todo_items_path.parent.mkdir(parents=True, exist_ok=True)
        run_todo_items_path.parent.mkdir(parents=True, exist_ok=True)
        todo_items_path.touch(exist_ok=True)
        run_todo_items_path.touch(exist_ok=True)

        run_config = {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "run_label": run_label,
            "profile_id": profile_id,
            "started_at_utc": run_started_at,
            "root_dir": str(root_dir),
            "filters": profile_filters,
            "flags": {
                "restart": restart,
                "download_new": download_new,
                "download_failed": download_failed,
                "save_output": save_output,
                "download_assets": download_assets,
                "continue_on_error": continue_on_error,
                "include_state_standards": include_state_standards,
            },
            "amount": amount.value if isinstance(amount, ScraperAmount) else int(amount),
            "rate_limit": {
                "max_requests_per_second": self.max_requests_per_second,
                "min_request_interval_seconds": self._min_request_interval_seconds,
            },
        }
        self._write_json_atomic(run_dir / "run-config.json", run_config)
        self._write_run_progress(
            stage="initialized",
            run_id=run_id,
            profile_id=profile_id,
            run_dir=run_dir,
        )

        results: List[QuestionRecord] = []
        interrupted = False
        fatal_exception: Optional[Exception] = None
        fatal_should_raise = False

        profile_state = self._load_profile_state(profile_state_path, profile_id=profile_id, filters=profile_filters)
        if restart:
            LOGGER.info("Restart requested: clearing profile progress for profile_id=%s", profile_id)
            profile_state = self._reset_profile_state(profile_state)
            self._save_profile_state(profile_state_path, profile_state)

        try:
            query_started = time.monotonic()
            available_rows = self._get_filtered_rows()
            self._run_stats["timing"]["query_seconds"] = round(time.monotonic() - query_started, 4)

            requested_count = self._resolve_amount(amount, len(available_rows))
            selected_rows = available_rows[:requested_count]

            candidate_rows, selection_counts = self._select_candidate_rows(
                rows=selected_rows,
                profile_state=profile_state,
                restart=restart,
                download_new=download_new,
                download_failed=download_failed,
            )
            self._run_stats["selection"].update(selection_counts)
            self._run_stats["selection"]["available_count"] = len(available_rows)
            self._run_stats["selection"]["requested_count"] = requested_count
            self._write_run_progress(
                stage="selected",
                run_id=run_id,
                profile_id=profile_id,
                run_dir=run_dir,
            )

            standards_map: Dict[str, List[str]] = {}
            if include_state_standards and self._state_selection:
                standards_started = time.monotonic()
                standards_map = self._fetch_state_standards()
                self._run_stats["timing"]["state_standards_seconds"] = round(
                    time.monotonic() - standards_started,
                    4,
                )

            external_ids = [
                self._clean_nullable(row.get("external_id"))
                for row in candidate_rows
                if self._clean_nullable(row.get("external_id"))
            ]

            detail_started = time.monotonic()
            digital_payloads = self._fetch_digital_payloads(external_ids)
            self._run_stats["timing"]["digital_payload_prefetch_seconds"] = round(
                time.monotonic() - detail_started,
                4,
            )

            asset_downloader = AssetDownloader(
                self._session,
                site_base_url=self.WEBSITE_BASE_URL,
                timeout=self.request_timeout,
                request_callable=self._perform_request,
                anomaly_callback=self._handle_asset_anomaly,
            )

            process_started = time.monotonic()
            total_candidates = len(candidate_rows)
            LOGGER.info(
                "Run %s starting: available=%d requested=%d candidates=%d",
                run_id,
                len(available_rows),
                requested_count,
                total_candidates,
            )

            for position, row in enumerate(candidate_rows, start=1):
                question_key = self._question_key_from_row(row)
                question_id = str(row.get("questionId") or row.get("uId") or "")
                LOGGER.info("Processing %d/%d question_id=%s key=%s", position, total_candidates, question_id, question_key)
                row_domain, row_difficulty, row_source = self._classify_row_for_stats(row)
                self._increment_named_counter(self._run_stats["processing"], "attempted_count")
                self._record_question_breakdown(
                    outcome="attempted",
                    domain=row_domain,
                    difficulty=row_difficulty,
                    source=row_source,
                )
                self._write_run_progress(
                    stage="processing",
                    run_id=run_id,
                    profile_id=profile_id,
                    run_dir=run_dir,
                    current_question_key=question_key,
                    current_question_id=question_id,
                    position=position,
                    total_candidates=total_candidates,
                )

                try:
                    self._inspect_table_row(
                        row=row,
                        question_key=question_key,
                        question_id=question_id,
                    )
                    metadata = self._build_metadata(row, standards_map, source=row_source)
                    detail_payload, source = self._resolve_detail_payload(row, digital_payloads)
                    self._inspect_detail_payload(
                        source=source,
                        payload=detail_payload,
                        question_key=question_key,
                        question_id=metadata.question_id,
                    )
                    content, parse_warnings = self._parse_content(
                        source=source,
                        payload=detail_payload,
                        question_key=question_key,
                    )
                    self._inspect_content_layout(
                        content=content,
                        source=source,
                        question_key=question_key,
                        question_id=metadata.question_id,
                    )

                    question_rel_path = self._determine_question_relative_path(
                        profile_state=profile_state,
                        question_key=question_key,
                        question_id=metadata.question_id,
                    )
                    question_dir = root_dir / question_rel_path

                    record = QuestionRecord(
                        metadata=metadata,
                        source=source,
                        content=content,
                        assets=[],
                        parse_warnings=parse_warnings,
                        raw_table_row=row,
                        raw_detail_payload=detail_payload,
                        raw_payload=detail_payload,
                    )

                    if parse_warnings:
                        LOGGER.warning(
                            "Question key=%s parsed with %d warning(s)",
                            question_key,
                            len(parse_warnings),
                        )
                        self._run_stats["parsing"]["warnings_count"] += len(parse_warnings)
                        for warning in parse_warnings:
                            self._record_todo_item(
                                category="parse_warning",
                                summary=warning,
                                severity="warning",
                                question_key=question_key,
                                question_id=metadata.question_id,
                                source=source,
                                details={"source": source, "warning": warning},
                                recommended_action=(
                                    "Review parser assumptions for this payload shape and extend normalization logic."
                                ),
                            )

                    if download_assets:
                        record = asset_downloader.rewrite_question_assets(
                            record,
                            question_dir,
                            question_key=question_key,
                        )

                    if save_output:
                        lifecycle_status, record = self._apply_record_lifecycle(
                            record=record,
                            question_dir=question_dir,
                            run_id=run_id,
                            observed_at_utc=_utc_now_iso(),
                        )
                        self._increment_named_counter(self._run_stats["dataset"], f"{lifecycle_status}_count")
                        output_sizes = self._write_question_output(record, question_dir)
                        dataset_index[question_key] = record.to_dict()
                        if lifecycle_status == "new":
                            new_dataset_rows.append(
                                {
                                    "question_key": question_key,
                                    "question_id": record.metadata.question_id,
                                    "external_id": record.metadata.external_id or "",
                                    "ibn": record.metadata.ibn or "",
                                }
                            )
                        elif lifecycle_status == "modified":
                            modified_dataset_rows.append(
                                {
                                    "question_key": question_key,
                                    "question_id": record.metadata.question_id,
                                    "external_id": record.metadata.external_id or "",
                                    "ibn": record.metadata.ibn or "",
                                }
                            )
                    else:
                        lifecycle_status = "unchanged"
                        output_sizes = {"json_bytes": 0, "html_bytes": 0}

                    results.append(record)
                    self._record_success(
                        profile_state=profile_state,
                        question_key=question_key,
                        row=row,
                        run_id=run_id,
                        question_rel_path=question_rel_path,
                    )
                    self._save_profile_state(profile_state_path, profile_state)

                    self._increment_named_counter(self._run_stats["processing"], "processed_count")
                    self._increment_named_counter(self._run_stats["processing"], "success_count")
                    self._increment_named_counter(self._run_stats["sources"], source)
                    self._record_question_breakdown(
                        outcome="success",
                        domain=record.metadata.domain,
                        difficulty=record.metadata.difficulty,
                        source=source,
                        question_type=record.content.question_type,
                    )
                    self._update_asset_stats(record)
                    self._update_record_byte_stats(record, output_sizes=output_sizes)
                    self._write_run_progress(
                        stage="processing",
                        run_id=run_id,
                        profile_id=profile_id,
                        run_dir=run_dir,
                        current_question_key=question_key,
                        current_question_id=question_id,
                        position=position,
                        total_candidates=total_candidates,
                    )

                except KeyboardInterrupt:
                    interrupted = True
                    LOGGER.warning("Run interrupted by user during question key=%s", question_key)
                    self._write_run_progress(
                        stage="interrupted",
                        run_id=run_id,
                        profile_id=profile_id,
                        run_dir=run_dir,
                        current_question_key=question_key,
                        current_question_id=question_id,
                        position=position,
                        total_candidates=total_candidates,
                    )
                    break
                except Exception as exc:
                    error_message = f"Failed question key={question_key}: {exc}"
                    self.last_errors.append(error_message)
                    self._increment_named_counter(self._run_stats["processing"], "processed_count")
                    self._increment_named_counter(self._run_stats["processing"], "failed_count")
                    failed_by_error_type = self._run_stats["processing"].setdefault("failed_by_error_type", {})
                    error_type = exc.__class__.__name__
                    self._increment_named_counter(failed_by_error_type, error_type)
                    self._record_question_breakdown(
                        outcome="failed",
                        domain=row_domain,
                        difficulty=row_difficulty,
                        source=row_source,
                    )

                    self._record_failure(
                        profile_state=profile_state,
                        question_key=question_key,
                        row=row,
                        run_id=run_id,
                        error_message=error_message,
                    )
                    self._save_profile_state(profile_state_path, profile_state)

                    LOGGER.exception(error_message)
                    self._append_jsonl(
                        run_errors_path,
                        {
                            "timestamp_utc": _utc_now_iso(),
                            "question_key": question_key,
                            "question_id": question_id,
                            "error": str(exc),
                            "traceback": traceback.format_exc(),
                        },
                    )
                    self._record_todo_item(
                        category="question_processing_error",
                        summary="Question processing failed",
                        severity="error",
                        question_key=question_key,
                        question_id=question_id,
                        details={
                            "error_type": exc.__class__.__name__,
                            "error": str(exc),
                        },
                        recommended_action=(
                            "Review run/errors.jsonl traceback and raw payload in question output (if present) "
                            "to add parser/downloader handling for this format."
                        ),
                    )
                    self._write_run_progress(
                        stage="processing",
                        run_id=run_id,
                        profile_id=profile_id,
                        run_dir=run_dir,
                        current_question_key=question_key,
                        current_question_id=question_id,
                        position=position,
                        total_candidates=total_candidates,
                    )

                    if not continue_on_error:
                        fatal_exception = exc
                        fatal_should_raise = True
                        break

            self._run_stats["timing"]["processing_seconds"] = round(time.monotonic() - process_started, 4)

        except KeyboardInterrupt:
            interrupted = True
            LOGGER.warning("Run interrupted by user")
            self._write_run_progress(
                stage="interrupted",
                run_id=run_id,
                profile_id=profile_id,
                run_dir=run_dir,
            )
        except Exception as exc:
            fatal_exception = exc
            fatal_should_raise = True
            error_message = f"Run failed before completion: {exc}"
            self.last_errors.append(error_message)
            LOGGER.exception(error_message)
            self._append_jsonl(
                run_errors_path,
                {
                    "timestamp_utc": _utc_now_iso(),
                    "question_key": None,
                    "question_id": None,
                    "error": str(exc),
                    "traceback": traceback.format_exc(),
                },
            )
            self._record_todo_item(
                category="run_fatal_error",
                summary="Fatal run-level exception",
                severity="error",
                details={
                    "error_type": exc.__class__.__name__,
                    "error": str(exc),
                },
                recommended_action="Inspect traceback and improve retry/fallback behavior for this failure path.",
            )
            self._write_run_progress(
                stage="failed",
                run_id=run_id,
                profile_id=profile_id,
                run_dir=run_dir,
            )
        finally:
            profile_state["updated_at_utc"] = _utc_now_iso()
            self._save_profile_state(profile_state_path, profile_state)

            ended_at_utc = _utc_now_iso()
            duration_seconds = round(time.monotonic() - run_started_monotonic, 4)
            self._run_stats["timing"]["finished_at_utc"] = ended_at_utc
            self._run_stats["timing"]["total_seconds"] = duration_seconds

            if interrupted:
                run_status = "interrupted"
            elif fatal_exception is not None:
                run_status = "failed"
            elif self._run_stats["processing"]["failed_count"] > 0:
                run_status = "completed_with_errors"
            else:
                run_status = "completed"

            todo_summary = self._finalize_todo_artifacts(
                run_id=run_id,
                profile_id=profile_id,
                run_dir=run_dir,
            )

            if save_output:
                self._write_dataset_jsonl(data_path, dataset_index)
            self._run_stats["dataset"]["total_records_after_run"] = len(dataset_index)
            self._write_id_csv(run_new_ids_path, new_dataset_rows)
            self._write_id_csv(run_modified_ids_path, modified_dataset_rows)
            global_dataset_stats = self._compute_global_dataset_stats(dataset_index)
            self._write_json_atomic(data_stats_path, global_dataset_stats)
            self._write_run_todo_markdown(
                run_todo_markdown_path,
                todo_summary=todo_summary,
                run_errors=self.last_errors,
                new_dataset_rows=new_dataset_rows,
                modified_dataset_rows=modified_dataset_rows,
            )

            summary = {
                "schema_version": SCHEMA_VERSION,
                "run_id": run_id,
                "run_label": run_label,
                "profile_id": profile_id,
                "status": run_status,
                "started_at_utc": run_started_at,
                "finished_at_utc": ended_at_utc,
                "duration_seconds": duration_seconds,
                "root_dir": str(root_dir),
                "run_dir": str(run_dir),
                "run_progress_path": str(run_progress_path),
                "data_path": str(data_path),
                "data_stats_path": str(data_stats_path),
                "profile_state_path": str(profile_state_path),
                "selection": self._run_stats["selection"],
                "processing": self._run_stats["processing"],
                "requests": self._run_stats["requests"],
                "sources": self._run_stats["sources"],
                "question_breakdown": self._run_stats["question_breakdown"],
                "assets": self._run_stats["assets"],
                "bytes": self._run_stats["bytes"],
                "dataset": self._run_stats["dataset"],
                "global_dataset_stats": global_dataset_stats,
                "parsing": self._run_stats["parsing"],
                "todo": todo_summary,
                "run_artifacts": {
                    "stats_yaml_path": str(run_stats_yaml_path),
                    "todo_items_markdown_path": str(run_todo_markdown_path),
                    "new_ids_csv_path": str(run_new_ids_path),
                    "modified_ids_csv_path": str(run_modified_ids_path),
                    "todo_items_jsonl_path": str(run_todo_items_path),
                    "todo_summary_json_path": str(run_todo_summary_path),
                },
                "errors_count": len(self.last_errors),
                "errors": self.last_errors,
                "flags": run_config["flags"],
                "filters": profile_filters,
            }

            self._write_run_progress(
                stage=run_status,
                run_id=run_id,
                profile_id=profile_id,
                run_dir=run_dir,
            )

            self.last_run_summary = summary
            self._write_json_atomic(run_dir / "run-summary.json", summary)
            self._write_json_atomic(run_dir / "run-stats.json", self._run_stats)
            self._write_yaml_atomic(
                run_stats_yaml_path,
                {
                    "run_summary": summary,
                    "run_stats": self._run_stats,
                },
            )

            self._append_jsonl(
                paths["history_path"],
                {
                    "schema_version": SCHEMA_VERSION,
                    "run_id": run_id,
                    "run_label": run_label,
                    "profile_id": profile_id,
                    "status": run_status,
                    "started_at_utc": run_started_at,
                    "finished_at_utc": ended_at_utc,
                    "duration_seconds": duration_seconds,
                    "selection": self._run_stats["selection"],
                    "processing": self._run_stats["processing"],
                    "dataset": {
                        "new_count": self._run_stats["dataset"]["new_count"],
                        "modified_count": self._run_stats["dataset"]["modified_count"],
                        "total_records_after_run": self._run_stats["dataset"]["total_records_after_run"],
                    },
                    "todo": {
                        "total_items": todo_summary["total_items"],
                        "unique_signatures_in_run": todo_summary["unique_signatures_in_run"],
                    },
                    "errors_count": len(self.last_errors),
                    "run_dir": str(run_dir),
                },
            )

            self._write_json_atomic(
                paths["state_dir"] / "latest-run.json",
                {
                    "schema_version": SCHEMA_VERSION,
                    "run_id": run_id,
                    "profile_id": profile_id,
                    "status": run_status,
                    "finished_at_utc": ended_at_utc,
                    "run_dir": str(run_dir),
                    "todo_items": todo_summary["total_items"],
                    "dataset_total_records": self._run_stats["dataset"]["total_records_after_run"],
                },
            )

            if todo_summary["total_items"] > 0:
                LOGGER.warning(
                    "Run %s recorded %d TODO anomalies (%d unique in this run). Review %s",
                    run_id,
                    todo_summary["total_items"],
                    todo_summary["unique_signatures_in_run"],
                    todo_summary["todo_markdown_path"],
                )

            self._detach_run_log_handler(log_handler)
            self._active_run_context = {}
            self._active_run_todo_counts = {"total_items": 0, "new_signatures": 0}
            self._active_run_todo_signatures = set()
            self._active_run_todo_category_counts = {}
            self._active_run_todo_severity_counts = {}

        if fatal_exception is not None and fatal_should_raise:
            raise fatal_exception

        return results

    # ---------- Request + lookup ----------

    def _build_session(self, *, max_retries: int) -> requests.Session:
        session = requests.Session()
        retries = Retry(
            total=max_retries,
            connect=max_retries,
            read=max_retries,
            status=max_retries,
            backoff_factor=0.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=("GET", "POST"),
            respect_retry_after_header=True,
        )
        adapter = HTTPAdapter(max_retries=retries)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        session.headers.update(
            {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "college-board-scraper/1.0 (+https://github.com/VG-Fish/College-Board)",
            }
        )
        return session

    def _perform_request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        endpoint = self._classify_endpoint(url)
        timeout = kwargs.pop("timeout", self.request_timeout)

        throttle_sleep = 0.0
        if self._min_request_interval_seconds > 0:
            with self._request_lock:
                now = time.monotonic()
                elapsed = now - self._last_request_monotonic
                wait = self._min_request_interval_seconds - elapsed
                if wait > 0:
                    time.sleep(wait)
                    throttle_sleep = wait
                self._last_request_monotonic = time.monotonic()
        else:
            with self._request_lock:
                self._last_request_monotonic = time.monotonic()

        if throttle_sleep > 0:
            self._run_stats["requests"]["throttle_sleep_seconds"] += round(throttle_sleep, 6)

        started = time.monotonic()
        self._record_request_attempt(endpoint)
        try:
            response = self._session.request(method=method, url=url, timeout=timeout, **kwargs)
            elapsed_seconds = round(time.monotonic() - started, 6)
            response_bytes = self._response_size_bytes(response)
            self._record_request_response(endpoint, response.status_code, elapsed_seconds, response_bytes=response_bytes)
            response.raise_for_status()
            return response
        except requests.RequestException:
            self._record_request_exception(endpoint)
            raise

    def _request_json(self, method: str, url: str, payload: Optional[Dict[str, Any]] = None) -> Any:
        response = self._perform_request(method=method, url=url, json=payload)
        try:
            return response.json()
        except ValueError as exc:
            snippet = response.text[:500]
            raise RuntimeError(f"Non-JSON response from {url}: {snippet}") from exc

    def _fetch_lookup_payload(self) -> Dict[str, Any]:
        payload = self._request_json("GET", self.LOOKUP_URL)
        if "lookupData" not in payload:
            raise RuntimeError("Lookup payload is missing expected `lookupData` key")
        return payload

    def _build_domain_catalog(self, lookup_payload: Dict[str, Any]) -> Dict[str, Dict[str, Dict[str, Any]]]:
        domain_data = lookup_payload["lookupData"]["domain"]

        catalog: Dict[str, Dict[str, Dict[str, Any]]] = {
            "Reading and Writing": {},
            "Math": {},
        }

        for domain in domain_data.get("R&W", []):
            catalog["Reading and Writing"][domain["text"]] = {
                "code": domain["primaryClassCd"],
                "skills": {skill["text"] for skill in domain.get("skill", [])},
            }

        for domain in domain_data.get("Math", []):
            catalog["Math"][domain["text"]] = {
                "code": domain["primaryClassCd"],
                "skills": {skill["text"] for skill in domain.get("skill", [])},
            }

        return catalog

    def _resolve_state_selection(self, state: Optional[str]) -> Optional[Dict[str, str]]:
        if not state:
            return None

        offerings = self._lookup_payload.get("stateOfferings", [])
        by_code = {offering["stateCd"].upper(): offering for offering in offerings}
        by_name = {offering["name"].lower(): offering for offering in offerings}

        normalized = state.strip()
        by_code_match = by_code.get(normalized.upper())
        if by_code_match:
            return {"stateCd": by_code_match["stateCd"], "name": by_code_match["name"]}

        by_name_match = by_name.get(normalized.lower())
        if by_name_match:
            return {"stateCd": by_name_match["stateCd"], "name": by_name_match["name"]}

        supported = ", ".join(sorted(by_code.keys()))
        raise ValueError(f"Unknown state '{state}'. Expected one of state code/name from lookup data. Codes: {supported}")

    # ---------- Validation ----------

    def _validate_filters(self) -> None:
        if self.assessment not in self._assessment_id_by_name:
            valid = sorted(self._assessment_id_by_name.keys())
            raise ValueError(f"assessment={self.assessment!r} must be one of {valid}.")

        if self.test not in self._test_id_by_name:
            valid = sorted(self._test_id_by_name.keys())
            raise ValueError(f"test={self.test!r} must be one of {valid}.")

        if not self.options:
            valid_options = sorted(self._domain_catalog[self.test].keys())
            raise ValueError(f"options must not be empty. Valid options for {self.test}: {valid_options}")

        valid_options = set(self._domain_catalog[self.test].keys())
        if not self.options.issubset(valid_options):
            raise ValueError(
                f"options={sorted(self.options)} must be a subset of {sorted(valid_options)} for test={self.test!r}"
            )

        if self.difficulties and not self.difficulties.issubset(self.valid_difficulty_options):
            raise ValueError(
                f"difficulties={sorted(self.difficulties)} must be a subset of {sorted(self.valid_difficulty_options)}"
            )

        if self.skills is None:
            return

        if not set(self.skills.keys()).issubset(self.options):
            raise ValueError(f"skills keys must be a subset of options={sorted(self.options)}")

        for option in self.options:
            if option not in self.skills:
                raise ValueError(
                    f"Missing skills for option={option!r}. When skills are provided, each selected option needs a skill set."
                )

            valid_skills = self._domain_catalog[self.test][option]["skills"]
            selected_skills = self.skills[option]
            if not selected_skills.issubset(valid_skills):
                raise ValueError(
                    f"skills[{option}]={sorted(selected_skills)} must be a subset of {sorted(valid_skills)}"
                )

    # ---------- Query + filtering ----------

    def _get_filtered_rows(self) -> List[Dict[str, Any]]:
        payload = {
            "asmtEventId": self._assessment_id_by_name[self.assessment],
            "test": self._test_id_by_name[self.test],
            "domain": ",".join(self._selected_domain_codes()),
        }
        rows: List[Dict[str, Any]] = self._request_json("POST", self.GET_QUESTIONS_URL, payload)

        allowed_difficulty_codes = (
            {self._DIFFICULTY_TO_CODE[difficulty] for difficulty in self.difficulties} if self.difficulties else None
        )
        allowed_skills = set(skill for group in (self.skills or {}).values() for skill in group) if self.skills else None

        live_ids = set()
        if self.exclude_active_questions:
            if self.test == "Reading and Writing":
                live_ids = set(self._lookup_payload.get("readingLiveItems", []))
            elif self.test == "Math":
                live_ids = set(self._lookup_payload.get("mathLiveItems", []))

        filtered: List[Dict[str, Any]] = []
        for row in rows:
            difficulty_code = row.get("difficulty")
            if allowed_difficulty_codes and difficulty_code not in allowed_difficulty_codes:
                continue

            skill_name = (row.get("skill_desc") or "").strip()
            if allowed_skills and skill_name not in allowed_skills:
                continue

            external_id = self._clean_nullable(row.get("external_id"))
            if live_ids and external_id and external_id in live_ids:
                continue

            filtered.append(row)

        return filtered

    def _selected_domain_codes(self) -> List[str]:
        # Keep lookup order for deterministic API payloads.
        domain_order = list(self._domain_catalog[self.test].keys())
        selected = []
        for option_name in domain_order:
            if option_name in self.options:
                selected.append(self._domain_catalog[self.test][option_name]["code"])
        return selected

    def _resolve_amount(self, amount: int | ScraperAmount, total_available: int) -> int:
        if isinstance(amount, ScraperAmount):
            if amount == ScraperAmount.ALL:
                return total_available
            if amount == ScraperAmount.RANDOM:
                raise NotImplementedError("ScraperAmount.RANDOM is not implemented yet")

        amount_int = int(amount)
        if amount_int < 0:
            raise ValueError("amount must be non-negative")
        return min(amount_int, total_available)

    def _select_candidate_rows(
        self,
        *,
        rows: Sequence[Dict[str, Any]],
        profile_state: Dict[str, Any],
        restart: bool,
        download_new: bool,
        download_failed: bool,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
        candidates: List[Dict[str, Any]] = []
        counts = {
            "candidate_count": 0,
            "skipped_success_count": 0,
            "skipped_new_disabled_count": 0,
            "skipped_failed_disabled_count": 0,
        }

        existing_questions = profile_state.get("questions", {})

        for row in rows:
            key = self._question_key_from_row(row)
            status = existing_questions.get(key, {}).get("status")

            if restart:
                candidates.append(row)
                continue

            if status == "success":
                counts["skipped_success_count"] += 1
                continue

            if status == "failed" and not download_failed:
                counts["skipped_failed_disabled_count"] += 1
                continue

            if status is None and not download_new:
                counts["skipped_new_disabled_count"] += 1
                continue

            candidates.append(row)

        counts["candidate_count"] = len(candidates)
        return candidates, counts

    # ---------- Content retrieval ----------

    def _fetch_digital_payloads(self, external_ids: Sequence[str]) -> Dict[str, Dict[str, Any]]:
        if not external_ids:
            return {}

        payloads: Dict[str, Dict[str, Any]] = {}
        unique_ids = sorted({external_id for external_id in external_ids if external_id})

        for group in chunked(unique_ids, self._PDF_BATCH_SIZE):
            data: List[Dict[str, Any]] = self._request_json(
                "POST",
                self.PDF_DOWNLOAD_URL,
                {"external_ids": list(group)},
            )
            for item in data:
                external_id = self._clean_nullable(item.get("externalid"))
                if external_id:
                    payloads[external_id] = item

        # Fallback for rare misses from bulk endpoint.
        missing_ids = [external_id for external_id in unique_ids if external_id not in payloads]
        if missing_ids:
            LOGGER.warning("Bulk digital endpoint missed %d question(s); falling back to single-item endpoint", len(missing_ids))
        for external_id in missing_ids:
            payloads[external_id] = self._request_json("POST", self.GET_QUESTION_URL, {"external_id": external_id})

        return payloads

    def _fetch_legacy_payload(self, ibn: str) -> Dict[str, Any]:
        url = f"{self.LEGACY_DISCLOSED_BASE_URL}/{ibn}.json"
        payload = self._request_json("GET", url)
        if not isinstance(payload, list) or not payload:
            raise RuntimeError(f"Legacy item response for {ibn!r} is empty or malformed")
        return payload[0]

    def _fetch_state_standards(self) -> Dict[str, List[str]]:
        if not self._state_selection:
            return {}

        payload = {
            "stateCd": self._state_selection["stateCd"],
            "asmtId": self._assessment_id_by_name[self.assessment],
        }
        rows: List[Dict[str, Any]] = self._request_json("POST", self.STATE_STANDARDS_URL, payload)
        mapped: Dict[str, List[str]] = {}
        for row in rows:
            skill_cd = row.get("skillCd")
            standards = row.get("stateStandards") or []
            if not skill_cd:
                continue
            mapped[skill_cd] = sorted(standards, key=_natural_sort_key)
        return mapped

    def _resolve_detail_payload(
        self,
        row: Dict[str, Any],
        digital_payloads: Dict[str, Dict[str, Any]],
    ) -> Tuple[Dict[str, Any], str]:
        external_id = self._clean_nullable(row.get("external_id"))
        ibn = self._clean_nullable(row.get("ibn"))

        if external_id:
            payload = digital_payloads.get(external_id)
            if not payload:
                payload = self._request_json("POST", self.GET_QUESTION_URL, {"external_id": external_id})
            return payload, "digital"

        if ibn:
            return self._fetch_legacy_payload(ibn), "legacy"

        raise RuntimeError(
            f"Question row {row.get('questionId')} has neither external_id nor ibn. Cannot fetch content payload."
        )

    def _parse_content(
        self,
        *,
        source: str,
        payload: Dict[str, Any],
        question_key: str,
    ) -> Tuple[QuestionContent, List[str]]:
        if source == "digital":
            return self._parse_digital_content(payload, question_key=question_key)
        if source == "legacy":
            return self._parse_legacy_content(payload, question_key=question_key)
        raise ValueError(f"Unsupported question source: {source}")

    def _parse_digital_content(
        self,
        payload: Dict[str, Any],
        *,
        question_key: str,
    ) -> Tuple[QuestionContent, List[str]]:
        warnings: List[str] = []

        answer_options_raw = payload.get("answerOptions")
        if answer_options_raw is None:
            answer_options_raw = []
        elif not isinstance(answer_options_raw, list):
            warnings.append("digital.answerOptions is not a list; coercing to empty list")
            answer_options_raw = []

        answer_options: List[AnswerOption] = []
        for index, option in enumerate(answer_options_raw, start=1):
            if not isinstance(option, dict):
                warnings.append(f"digital.answerOptions[{index-1}] is not an object; skipping")
                continue
            letter = chr(64 + index)
            answer_options.append(AnswerOption(letter=letter, content_html=str(option.get("content", ""))))

        correct_raw = payload.get("correct_answer") or []
        if isinstance(correct_raw, list):
            correct_answers = [str(answer) for answer in correct_raw]
        elif correct_raw is None:
            correct_answers = []
        else:
            warnings.append("digital.correct_answer is not a list; coercing to single value list")
            correct_answers = [str(correct_raw)]

        prompt_html = payload.get("stimulus") or payload.get("prompt") or ""
        stem_html = payload.get("stem") or ""
        rationale_html = payload.get("rationale") or ""
        question_type = payload.get("type")

        if not prompt_html and not stem_html:
            warnings.append("digital payload missing both prompt/stem HTML")
        if question_type is None:
            warnings.append("digital payload missing question type")

        content = QuestionContent(
            prompt_html=str(prompt_html),
            stem_html=str(stem_html),
            answer_options=answer_options,
            rationale_html=str(rationale_html),
            correct_answers=correct_answers,
            question_type=str(question_type) if question_type is not None else None,
        )

        if warnings:
            LOGGER.warning("Parse warnings for %s: %s", question_key, "; ".join(warnings))

        return content, warnings

    def _parse_legacy_content(
        self,
        payload: Dict[str, Any],
        *,
        question_key: str,
    ) -> Tuple[QuestionContent, List[str]]:
        warnings: List[str] = []

        answer = payload.get("answer") or {}
        if not isinstance(answer, dict):
            warnings.append("legacy.answer is not an object; coercing to empty object")
            answer = {}

        choices = answer.get("choices") or {}
        if not isinstance(choices, dict):
            warnings.append("legacy.answer.choices is not an object; coercing to empty object")
            choices = {}

        answer_options: List[AnswerOption] = []
        for key in sorted(choices.keys()):
            option = choices[key] or {}
            if not isinstance(option, dict):
                warnings.append(f"legacy.answer.choices.{key} is not an object; skipping")
                continue
            answer_options.append(AnswerOption(letter=key.upper(), content_html=str(option.get("body", ""))))

        correct_answers: List[str] = []
        correct_choice = answer.get("correct_choice")
        if correct_choice:
            correct_answers.append(str(correct_choice).upper())

        correct_spr = answer.get("correct_spr") or {}
        if isinstance(correct_spr, dict):
            absolute_values = correct_spr.get("absolute") or []
            if isinstance(absolute_values, list):
                for value in absolute_values:
                    correct_answers.append(str(value))
            elif absolute_values:
                warnings.append("legacy.answer.correct_spr.absolute is not a list")
        elif correct_spr:
            warnings.append("legacy.answer.correct_spr is not an object")
            correct_spr = {}

        rationale_html = (
            answer.get("rationale")
            or (correct_spr.get("rationale") if isinstance(correct_spr, dict) else "")
            or ""
        )

        prompt_html = payload.get("body") or ""
        stem_html = payload.get("prompt") or ""
        question_type = answer.get("style")

        if not prompt_html and not stem_html:
            warnings.append("legacy payload missing both body/prompt HTML")
        if question_type is None:
            warnings.append("legacy payload missing answer style")

        content = QuestionContent(
            prompt_html=str(prompt_html),
            stem_html=str(stem_html),
            answer_options=answer_options,
            rationale_html=str(rationale_html),
            correct_answers=correct_answers,
            question_type=str(question_type) if question_type is not None else None,
        )

        if warnings:
            LOGGER.warning("Parse warnings for %s: %s", question_key, "; ".join(warnings))

        return content, warnings

    # ---------- Drift inspection + TODO pipeline ----------

    def _inspect_table_row(self, *, row: Dict[str, Any], question_key: str, question_id: str) -> None:
        self._inspect_key_drift(
            payload=row,
            expected_keys=self._EXPECTED_TABLE_ROW_KEYS,
            category="new_table_row_fields",
            summary="Unrecognized table-row fields detected",
            question_key=question_key,
            question_id=question_id,
            details_prefix={"source": "table_row"},
            recommended_action="Review new row keys and extend normalized metadata schema/mapping.",
        )

        missing_fields: List[str] = []
        if not question_id:
            missing_fields.append("questionId/uId")

        for field in ("primary_class_cd_desc", "primary_class_cd", "skill_desc", "skill_cd", "difficulty"):
            if not self._clean_nullable(row.get(field)):
                missing_fields.append(field)

        if missing_fields:
            self._record_todo_item(
                category="missing_metadata",
                summary="Question row is missing expected metadata fields",
                severity="warning",
                question_key=question_key,
                question_id=question_id,
                details={
                    "source": "table_row",
                    "missing_fields": missing_fields,
                    "row_keys": sorted(row.keys()),
                },
                recommended_action="Add fallback mapping rules for missing metadata or adjust required-field assumptions.",
            )

        difficulty_code = self._clean_nullable(row.get("difficulty"))
        if difficulty_code and difficulty_code not in self._CODE_TO_DIFFICULTY:
            self._record_todo_item(
                category="new_difficulty_code",
                summary="Unrecognized difficulty code detected",
                severity="info",
                question_key=question_key,
                question_id=question_id,
                details={"difficulty_code": difficulty_code},
                recommended_action="Map the new difficulty code to a normalized label.",
            )

    def _inspect_detail_payload(
        self,
        *,
        source: str,
        payload: Dict[str, Any],
        question_key: str,
        question_id: str,
    ) -> None:
        if not isinstance(payload, dict):
            self._record_todo_item(
                category="unexpected_detail_payload_type",
                summary="Detail payload is not a JSON object",
                severity="error",
                question_key=question_key,
                question_id=question_id,
                source=source,
                details={"source": source, "payload_type": type(payload).__name__},
                recommended_action="Inspect upstream endpoint response and update parser for the new payload type.",
            )
            return

        instruction_like_keys = sorted(
            key
            for key in payload.keys()
            if isinstance(key, str) and ("instruction" in key.lower() or "footnote" in key.lower())
        )
        if instruction_like_keys:
            self._record_todo_item(
                category="instruction_or_footnote_fields",
                summary="Instruction/footnote-like payload fields detected",
                severity="info",
                question_key=question_key,
                question_id=question_id,
                source=source,
                details={"source": source, "keys": instruction_like_keys},
                recommended_action="Review these fields and map them into normalized content if needed.",
            )

        if source == "digital":
            self._inspect_key_drift(
                payload=payload,
                expected_keys=self._EXPECTED_DIGITAL_DETAIL_KEYS,
                category="new_digital_detail_fields",
                summary="Unrecognized digital detail fields detected",
                question_key=question_key,
                question_id=question_id,
                source=source,
                details_prefix={"source": source},
                recommended_action="Review new digital payload keys and extend parser/schema handling.",
            )

            answer_options = payload.get("answerOptions")
            if isinstance(answer_options, list):
                option_unknown_keys: Set[str] = set()
                non_object_count = 0
                for option in answer_options:
                    if isinstance(option, dict):
                        option_unknown_keys.update(set(option.keys()) - self._EXPECTED_DIGITAL_ANSWER_OPTION_KEYS)
                    else:
                        non_object_count += 1

                if option_unknown_keys:
                    self._record_todo_item(
                        category="new_answer_option_fields",
                        summary="Digital answer option contains unknown fields",
                        severity="info",
                        question_key=question_key,
                        question_id=question_id,
                        source=source,
                        details={"source": source, "unknown_keys": sorted(option_unknown_keys)},
                        recommended_action="Review answer option schema and map useful fields.",
                    )

                if non_object_count > 0:
                    self._record_todo_item(
                        category="malformed_answer_option",
                        summary="Digital answer options contained non-object entries",
                        severity="warning",
                        question_key=question_key,
                        question_id=question_id,
                        source=source,
                        details={"source": source, "non_object_count": non_object_count},
                        recommended_action="Add parser fallback rules for non-object answer option entries.",
                    )
            elif answer_options is not None:
                self._record_todo_item(
                    category="malformed_answer_option",
                    summary="Digital answerOptions is not a list",
                    severity="warning",
                    question_key=question_key,
                    question_id=question_id,
                    source=source,
                    details={"source": source, "value_type": type(answer_options).__name__},
                    recommended_action="Coerce/parse non-list answerOptions shapes.",
                )
            return

        if source != "legacy":
            self._record_todo_item(
                category="unknown_question_source",
                summary="Unknown question source detected",
                severity="error",
                question_key=question_key,
                question_id=question_id,
                source=source,
                details={"source": source},
                recommended_action="Add explicit parser support for this source type.",
            )
            return

        self._inspect_key_drift(
            payload=payload,
            expected_keys=self._EXPECTED_LEGACY_DETAIL_KEYS,
            category="new_legacy_detail_fields",
            summary="Unrecognized legacy detail fields detected",
            question_key=question_key,
            question_id=question_id,
            source=source,
            details_prefix={"source": source},
            recommended_action="Review new legacy payload keys and extend parser/schema handling.",
        )

        answer = payload.get("answer")
        if not isinstance(answer, dict):
            if answer is not None:
                self._record_todo_item(
                    category="malformed_legacy_answer",
                    summary="Legacy answer field is not an object",
                    severity="warning",
                    question_key=question_key,
                    question_id=question_id,
                    source=source,
                    details={"source": source, "value_type": type(answer).__name__},
                    recommended_action="Add parser fallback for non-object legacy answer payloads.",
                )
            return

        self._inspect_key_drift(
            payload=answer,
            expected_keys=self._EXPECTED_LEGACY_ANSWER_KEYS,
            category="new_legacy_answer_fields",
            summary="Unrecognized legacy answer fields detected",
            question_key=question_key,
            question_id=question_id,
            source=source,
            details_prefix={"source": source},
            recommended_action="Review new legacy answer fields and extend parser/schema handling.",
        )

        choices = answer.get("choices")
        if isinstance(choices, dict):
            choice_unknown_keys: Set[str] = set()
            for choice in choices.values():
                if isinstance(choice, dict):
                    choice_unknown_keys.update(set(choice.keys()) - self._EXPECTED_LEGACY_CHOICE_KEYS)
            if choice_unknown_keys:
                self._record_todo_item(
                    category="new_legacy_choice_fields",
                    summary="Legacy choice contains unknown fields",
                    severity="info",
                    question_key=question_key,
                    question_id=question_id,
                    source=source,
                    details={"source": source, "unknown_keys": sorted(choice_unknown_keys)},
                    recommended_action="Review and map additional fields from legacy answer choices.",
                )

        correct_spr = answer.get("correct_spr")
        if isinstance(correct_spr, dict):
            self._inspect_key_drift(
                payload=correct_spr,
                expected_keys=self._EXPECTED_LEGACY_CORRECT_SPR_KEYS,
                category="new_legacy_correct_spr_fields",
                summary="Unrecognized legacy correct_spr fields detected",
                question_key=question_key,
                question_id=question_id,
                source=source,
                details_prefix={"source": source},
                recommended_action="Review correct_spr schema and extend parser behavior for new fields.",
            )

    def _inspect_content_layout(
        self,
        *,
        content: QuestionContent,
        source: str,
        question_key: str,
        question_id: str,
    ) -> None:
        html_fragments = [content.prompt_html, content.stem_html, content.rationale_html]
        html_fragments.extend(option.content_html for option in content.answer_options)
        joined_html = "\n".join(fragment for fragment in html_fragments if fragment)
        if not joined_html:
            return

        unresolved_footnotes = self._find_unresolved_footnote_refs(joined_html)
        if unresolved_footnotes:
            self._record_todo_item(
                category="footnote_marker_detected",
                summary="Unresolved footnote reference detected in content HTML",
                severity="info",
                question_key=question_key,
                question_id=question_id,
                source=source,
                details={
                    "source": source,
                    "marker": "footnote_unresolved_ref",
                    "unresolved_refs": unresolved_footnotes[:10],
                    "unresolved_count": len(unresolved_footnotes),
                },
                recommended_action="Ensure footnote reference anchors map to existing local targets in rendered HTML.",
            )

        if self._contains_pattern(joined_html, self._INSTRUCTION_STRUCTURE_PATTERNS):
            self._record_todo_item(
                category="instruction_marker_detected",
                summary="Instruction-like structural marker detected in content HTML",
                severity="info",
                question_key=question_key,
                question_id=question_id,
                source=source,
                details={"source": source, "marker": "instruction_structure"},
                recommended_action="Verify instruction text placement in rendered question output.",
            )

        tags = {match.lower() for match in re.findall(r"<\s*([a-zA-Z0-9:_-]+)", joined_html)}
        complex_tags = sorted(tags.intersection({"audio", "canvas", "embed", "iframe", "object", "picture", "track", "video"}))
        if complex_tags:
            self._record_todo_item(
                category="complex_media_layout",
                summary="Complex HTML media tags detected",
                severity="warning",
                question_key=question_key,
                question_id=question_id,
                source=source,
                details={"source": source, "tags": complex_tags},
                recommended_action="Review rendering/downloading support for these media tags.",
            )

    @staticmethod
    def _contains_pattern(html_fragment: str, patterns: Sequence[str]) -> bool:
        return any(re.search(pattern, html_fragment, flags=re.IGNORECASE) for pattern in patterns)

    @classmethod
    def _find_unresolved_footnote_refs(cls, html_fragment: str) -> List[str]:
        ids = {value.lower() for value in re.findall(r"\bid\s*=\s*['\"]([^'\"]+)['\"]", html_fragment, flags=re.IGNORECASE)}
        refs = re.findall(r"href\s*=\s*['\"]#([^'\"]+)['\"]", html_fragment, flags=re.IGNORECASE)
        unresolved: List[str] = []
        for ref in refs:
            normalized = ref.lower().strip()
            if not normalized:
                continue
            if not re.search(cls._FOOTNOTE_REF_NAME_PATTERN, normalized, flags=re.IGNORECASE):
                continue
            if normalized not in ids:
                unresolved.append(ref)
        return sorted(set(unresolved))

    def _inspect_key_drift(
        self,
        *,
        payload: Dict[str, Any],
        expected_keys: Set[str],
        category: str,
        summary: str,
        question_key: str,
        question_id: str,
        source: Optional[str] = None,
        details_prefix: Optional[Dict[str, Any]] = None,
        recommended_action: Optional[str] = None,
    ) -> None:
        unknown_keys = sorted(set(payload.keys()) - expected_keys)
        if not unknown_keys:
            return

        details = dict(details_prefix or {})
        details["unknown_keys"] = unknown_keys
        self._record_todo_item(
            category=category,
            summary=summary,
            severity="info",
            question_key=question_key,
            question_id=question_id,
            source=source,
            details=details,
            recommended_action=recommended_action
            or "Review payload drift and update parser/schema handling for these fields.",
        )

    def _handle_asset_anomaly(self, payload: Dict[str, Any]) -> None:
        category = str(payload.get("category") or "asset_anomaly")
        summary = str(payload.get("summary") or "Asset anomaly detected")
        severity = str(payload.get("severity") or "warning")
        details = payload.get("details") if isinstance(payload.get("details"), dict) else {"details": payload.get("details")}
        source = payload.get("source")
        question_key = self._clean_nullable(payload.get("question_key"))
        question_id = self._clean_nullable(payload.get("question_id"))
        recommended_action = self._clean_nullable(payload.get("recommended_action")) or (
            "Review asset downloader behavior and add support for this asset pattern/type."
        )

        self._record_todo_item(
            category=category,
            summary=summary,
            severity=severity,
            question_key=question_key,
            question_id=question_id,
            source=source if source else "asset",
            details=details,
            recommended_action=recommended_action,
        )

    def _load_todo_index(self, todo_index_path: Path) -> Dict[str, Any]:
        if todo_index_path.exists():
            try:
                payload = json.loads(todo_index_path.read_text(encoding="utf-8"))
                items = payload.get("items")
                if isinstance(items, dict):
                    payload.setdefault("index_version", TODO_INDEX_VERSION)
                    payload.setdefault("created_at_utc", _utc_now_iso())
                    payload["updated_at_utc"] = _utc_now_iso()
                    return payload
            except json.JSONDecodeError:
                LOGGER.warning("TODO index at %s is invalid JSON; rebuilding", todo_index_path)

        now = _utc_now_iso()
        return {
            "index_version": TODO_INDEX_VERSION,
            "created_at_utc": now,
            "updated_at_utc": now,
            "items": {},
        }

    def _save_todo_index(self, todo_index_path: Path, todo_index: Dict[str, Any]) -> None:
        todo_index["updated_at_utc"] = _utc_now_iso()
        self._write_json_atomic(todo_index_path, todo_index)

    def _record_todo_item(
        self,
        *,
        category: str,
        summary: str,
        severity: str = "warning",
        question_key: Optional[str] = None,
        question_id: Optional[str] = None,
        source: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
        recommended_action: Optional[str] = None,
    ) -> Optional[str]:
        if not self._active_run_context:
            return None

        category = category.strip() or "uncategorized"
        summary = summary.strip() or "Unspecified anomaly"
        severity = severity.strip().lower() or "warning"

        clean_details = self._sanitize_for_json(details or {})
        timestamp = _utc_now_iso()
        run_id = str(self._active_run_context["run_id"])
        profile_id = str(self._active_run_context["profile_id"])
        signature = self._compute_todo_signature(category=category, summary=summary, details=clean_details)

        occurrence = {
            "schema_version": SCHEMA_VERSION,
            "timestamp_utc": timestamp,
            "run_id": run_id,
            "profile_id": profile_id,
            "signature": signature,
            "category": category,
            "severity": severity,
            "summary": summary,
            "question_key": question_key,
            "question_id": question_id,
            "source": source,
            "details": clean_details,
            "recommended_action": recommended_action
            or "Review TODO details and update parser/downloader/schema handling.",
        }

        self._append_jsonl(Path(self._active_run_context["todo_items_path"]), occurrence)
        self._append_jsonl(Path(self._active_run_context["run_todo_items_path"]), occurrence)

        items = self._active_todo_index.setdefault("items", {})
        entry = items.get(signature)
        is_new = entry is None
        if is_new:
            entry = {
                "signature": signature,
                "category": category,
                "severity": severity,
                "summary": summary,
                "details": clean_details,
                "recommended_action": occurrence["recommended_action"],
                "status": "open",
                "first_seen_at_utc": timestamp,
                "last_seen_at_utc": timestamp,
                "occurrences": 0,
                "sample_run_ids": [],
                "sample_question_keys": [],
                "sample_question_ids": [],
            }
            items[signature] = entry

        entry["last_seen_at_utc"] = timestamp
        entry["occurrences"] = int(entry.get("occurrences", 0)) + 1
        entry["severity"] = severity
        entry["updated_at_utc"] = timestamp

        self._append_unique(entry.setdefault("sample_run_ids", []), run_id, limit=20)
        if question_key:
            self._append_unique(entry.setdefault("sample_question_keys", []), question_key, limit=20)
        if question_id:
            self._append_unique(entry.setdefault("sample_question_ids", []), question_id, limit=20)

        self._active_run_todo_counts["total_items"] = int(self._active_run_todo_counts.get("total_items", 0)) + 1
        if is_new:
            self._active_run_todo_counts["new_signatures"] = int(self._active_run_todo_counts.get("new_signatures", 0)) + 1
        self._active_run_todo_signatures.add(signature)
        self._active_run_todo_category_counts[category] = int(self._active_run_todo_category_counts.get(category, 0)) + 1
        self._active_run_todo_severity_counts[severity] = int(self._active_run_todo_severity_counts.get(severity, 0)) + 1

        todo_stats = self._run_stats.setdefault("todo", {})
        todo_stats["total_items"] = int(todo_stats.get("total_items", 0)) + 1
        if is_new:
            todo_stats["new_signatures"] = int(todo_stats.get("new_signatures", 0)) + 1
        todo_stats["unique_signatures_in_run"] = len(self._active_run_todo_signatures)

        by_category = todo_stats.setdefault("by_category", {})
        by_category[category] = int(by_category.get(category, 0)) + 1
        by_severity = todo_stats.setdefault("by_severity", {})
        by_severity[severity] = int(by_severity.get(severity, 0)) + 1

        self._save_todo_index(Path(self._active_run_context["todo_index_path"]), self._active_todo_index)
        if is_new:
            self._render_todo_markdown(Path(self._active_run_context["todo_markdown_path"]), self._active_todo_index)

        return signature

    def _finalize_todo_artifacts(self, *, run_id: str, profile_id: str, run_dir: Path) -> Dict[str, Any]:
        if not self._active_run_context:
            return {
                "total_items": 0,
                "new_signatures": 0,
                "unique_signatures_in_run": 0,
                "global_open_signatures": 0,
                "by_category": {},
                "by_severity": {},
                "run_todo_items_path": str(run_dir / "todo-items.jsonl"),
                "todo_items_path": "",
                "todo_index_path": "",
                "todo_markdown_path": "",
                "top_signatures": [],
            }

        todo_index_path = Path(self._active_run_context["todo_index_path"])
        todo_markdown_path = Path(self._active_run_context["todo_markdown_path"])
        run_todo_summary_path = Path(self._active_run_context["run_todo_summary_path"])

        self._save_todo_index(todo_index_path, self._active_todo_index)
        self._render_todo_markdown(todo_markdown_path, self._active_todo_index)

        index_items = self._active_todo_index.get("items", {})
        run_signatures = sorted(
            self._active_run_todo_signatures,
            key=lambda sig: int(index_items.get(sig, {}).get("occurrences", 0)),
            reverse=True,
        )
        top_signatures: List[Dict[str, Any]] = []
        for signature in run_signatures[:10]:
            item = index_items.get(signature, {})
            top_signatures.append(
                {
                    "signature": signature,
                    "category": item.get("category"),
                    "severity": item.get("severity"),
                    "summary": item.get("summary"),
                    "occurrences": item.get("occurrences", 0),
                    "last_seen_at_utc": item.get("last_seen_at_utc"),
                }
            )

        summary = {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "profile_id": profile_id,
            "total_items": int(self._active_run_todo_counts.get("total_items", 0)),
            "new_signatures": int(self._active_run_todo_counts.get("new_signatures", 0)),
            "unique_signatures_in_run": len(self._active_run_todo_signatures),
            "global_open_signatures": len(index_items),
            "by_category": dict(sorted(self._active_run_todo_category_counts.items())),
            "by_severity": dict(sorted(self._active_run_todo_severity_counts.items())),
            "run_todo_items_path": str(self._active_run_context["run_todo_items_path"]),
            "todo_items_path": str(self._active_run_context["todo_items_path"]),
            "todo_index_path": str(todo_index_path),
            "todo_markdown_path": str(todo_markdown_path),
            "top_signatures": top_signatures,
        }

        self._write_json_atomic(run_todo_summary_path, summary)
        return summary

    def _render_todo_markdown(self, todo_markdown_path: Path, todo_index: Dict[str, Any]) -> None:
        items = todo_index.get("items", {})
        sorted_items = sorted(
            items.items(),
            key=lambda pair: (
                -int(pair[1].get("occurrences", 0)),
                str(pair[1].get("last_seen_at_utc") or ""),
            ),
        )

        lines: List[str] = []
        lines.append("# SAT EQB TODO Backlog")
        lines.append("")
        lines.append("Generated from runtime anomaly detection. Each entry should be triaged and either resolved in code or marked closed in the index.")
        lines.append("")
        lines.append(f"Updated: {_utc_now_iso()}")
        lines.append(f"Open signatures: {len(sorted_items)}")
        lines.append("")

        if not sorted_items:
            lines.append("No open TODO anomalies.")
        else:
            for signature, item in sorted_items:
                lines.append(f"## [{str(item.get('severity', 'warning')).upper()}] {item.get('category', 'uncategorized')}")
                lines.append(f"Signature: `{signature}`")
                lines.append(f"Occurrences: {item.get('occurrences', 0)}")
                lines.append(f"First seen: {item.get('first_seen_at_utc', '')}")
                lines.append(f"Last seen: {item.get('last_seen_at_utc', '')}")
                lines.append(f"Summary: {item.get('summary', '')}")
                lines.append(f"Recommended action: {item.get('recommended_action', '')}")
                sample_question_keys = ", ".join(item.get("sample_question_keys", [])[:6])
                if sample_question_keys:
                    lines.append(f"Sample question keys: {sample_question_keys}")
                sample_question_ids = ", ".join(item.get("sample_question_ids", [])[:6])
                if sample_question_ids:
                    lines.append(f"Sample question IDs: {sample_question_ids}")
                details_json = json.dumps(item.get("details", {}), ensure_ascii=False, sort_keys=True)
                if len(details_json) > 1000:
                    details_json = details_json[:997] + "..."
                lines.append(f"Details: `{details_json}`")
                lines.append("")

        todo_markdown_path.parent.mkdir(parents=True, exist_ok=True)
        todo_markdown_path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")

    def _compute_todo_signature(self, *, category: str, summary: str, details: Dict[str, Any]) -> str:
        payload = {"category": category, "summary": summary, "details": details}
        encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]

    def _write_run_progress(
        self,
        *,
        stage: str,
        run_id: str,
        profile_id: str,
        run_dir: Path,
        current_question_key: Optional[str] = None,
        current_question_id: Optional[str] = None,
        position: Optional[int] = None,
        total_candidates: Optional[int] = None,
    ) -> None:
        try:
            payload = {
                "schema_version": SCHEMA_VERSION,
                "progress_version": RUN_PROGRESS_VERSION,
                "run_id": run_id,
                "profile_id": profile_id,
                "stage": stage,
                "updated_at_utc": _utc_now_iso(),
                "selection": dict(self._run_stats.get("selection", {})),
                "processing": dict(self._run_stats.get("processing", {})),
                "question_breakdown": self._sanitize_for_json(self._run_stats.get("question_breakdown", {})),
                "assets": dict(self._run_stats.get("assets", {})),
                "bytes": dict(self._run_stats.get("bytes", {})),
                "dataset": dict(self._run_stats.get("dataset", {})),
                "todo": {
                    "total_items": int(self._active_run_todo_counts.get("total_items", 0)),
                    "unique_signatures_in_run": len(self._active_run_todo_signatures),
                },
                "current": {
                    "question_key": current_question_key,
                    "question_id": current_question_id,
                    "position": position,
                    "total_candidates": total_candidates,
                },
            }
            self._write_json_atomic(run_dir / "run-progress.json", payload)
            progress_stats = self._run_stats.setdefault("progress", {})
            progress_stats["checkpoints_written"] = int(progress_stats.get("checkpoints_written", 0)) + 1
        except (OSError, TypeError, ValueError):
            LOGGER.warning("Failed to write run progress checkpoint for run_id=%s", run_id, exc_info=True)

    @staticmethod
    def _append_unique(values: List[str], candidate: str, *, limit: int) -> None:
        if candidate in values:
            return
        values.append(candidate)
        if len(values) > limit:
            del values[0 : len(values) - limit]

    def _sanitize_for_json(self, value: Any, *, max_string_length: Optional[int] = 2000) -> Any:
        if isinstance(value, dict):
            return {
                str(key): self._sanitize_for_json(item, max_string_length=max_string_length)
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple, set)):
            return [self._sanitize_for_json(item, max_string_length=max_string_length) for item in value]
        if isinstance(value, (str, int, float, bool)) or value is None:
            if isinstance(value, str) and max_string_length is not None and len(value) > max_string_length:
                if max_string_length <= 3:
                    return value[:max_string_length]
                return value[: max_string_length - 3] + "..."
            return value
        return str(value)

    # ---------- Serialization ----------

    def _build_metadata(
        self,
        row: Dict[str, Any],
        standards_map: Dict[str, List[str]],
        *,
        source: str,
    ) -> QuestionMetadata:
        difficulty_code = self._clean_nullable(row.get("difficulty")) or ""
        skill_code = self._clean_nullable(row.get("skill_cd")) or ""
        question_id = str(row.get("questionId") or row.get("uId") or "")
        external_id = self._clean_nullable(row.get("external_id"))
        ibn = self._clean_nullable(row.get("ibn"))
        domain = str(row.get("primary_class_cd_desc") or "")
        domain_code = str(row.get("primary_class_cd") or "")
        difficulty = self._CODE_TO_DIFFICULTY.get(difficulty_code, difficulty_code or "Unknown")

        return QuestionMetadata(
            question_id=question_id,
            assessment=self.assessment,
            assessment_id=self._assessment_id_by_name[self.assessment],
            test=self.test,
            test_id=self._test_id_by_name[self.test],
            domain=domain,
            domain_code=domain_code,
            skill=str(row.get("skill_desc") or ""),
            skill_code=skill_code,
            difficulty=difficulty,
            score_band_range=row.get("score_band_range_cd"),
            external_id=external_id,
            ibn=ibn,
            program=self._clean_nullable(row.get("program")),
            create_date=row.get("createDate"),
            update_date=row.get("updateDate"),
            state_standards=standards_map.get(skill_code, []),
            original_url=build_original_question_url(
                source=source,
                assessment=self.assessment,
                test=self.test,
                question_id=question_id,
                external_id=external_id,
                ibn=ibn,
                domain=domain,
                domain_code=domain_code,
                skill_code=skill_code,
                difficulty=difficulty,
            ),
        )

    def _write_question_output(self, record: QuestionRecord, question_dir: Path) -> Dict[str, int]:
        question_dir.mkdir(parents=True, exist_ok=True)

        json_path = question_dir / "question.json"
        json_text = json.dumps(record.to_dict(), ensure_ascii=False, indent=2)
        json_path.write_text(json_text, encoding="utf-8")

        html_path = question_dir / "question.html"
        html_text = self._render_question_html(record)
        html_path.write_text(html_text, encoding="utf-8")

        markdown_path = question_dir / "question.md"
        markdown_text = render_question_markdown(record)
        markdown_path.write_text(markdown_text, encoding="utf-8")
        return {
            "json_bytes": len(json_text.encode("utf-8")),
            "html_bytes": len(html_text.encode("utf-8")),
            "markdown_bytes": len(markdown_text.encode("utf-8")),
        }

    def _render_question_html(self, record: QuestionRecord) -> str:
        meta = record.metadata
        correct_answers = ", ".join(record.content.correct_answers) if record.content.correct_answers else ""

        answer_list = ""
        if record.content.answer_options:
            items = []
            for option in record.content.answer_options:
                letter = html.escape(option.letter)
                items.append(f"<li><span class='option-letter'>{letter}.</span> {option.content_html}</li>")
            answer_list = "<section><h2>Answer Choices</h2><ol>" + "".join(items) + "</ol></section>"

        state_standards = ""
        if meta.state_standards:
            standards = " ".join(f"<li>{html.escape(value)}</li>" for value in meta.state_standards)
            state_standards = f"<tr><th>State Standards</th><td><ul>{standards}</ul></td></tr>"

        parsing_warnings = ""
        if record.parse_warnings:
            warning_items = "".join(f"<li>{html.escape(item)}</li>" for item in record.parse_warnings)
            parsing_warnings = f"<section class='section'><h2>Parse Warnings</h2><ul>{warning_items}</ul></section>"

        prompt_html = record.content.prompt_html or ""
        stem_html = record.content.stem_html or ""
        rationale_html = record.content.rationale_html or ""

        return f"""<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
  <title>Question {html.escape(meta.question_id)}</title>
  <style>
    :root {{ color-scheme: light; }}
    body {{
      margin: 24px;
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      line-height: 1.5;
      color: #1f2937;
      background: #f8fafc;
    }}
    main {{
      max-width: 1100px;
      margin: 0 auto;
      background: #ffffff;
      border: 1px solid #dbe3ee;
      border-radius: 12px;
      padding: 24px;
      box-shadow: 0 8px 24px rgba(15, 23, 42, 0.08);
      overflow-wrap: anywhere;
    }}
    h1 {{ margin-top: 0; }}
    h2 {{ margin-top: 28px; border-top: 1px solid #e5e7eb; padding-top: 18px; }}
    table {{ width: 100%; border-collapse: collapse; margin-top: 10px; }}
    th, td {{ border: 1px solid #e5e7eb; padding: 8px 10px; vertical-align: top; text-align: left; }}
    th {{ width: 220px; background: #f9fafb; }}
    ol {{ margin: 0; padding-left: 20px; }}
    ul {{ margin: 0; padding-left: 20px; }}
    .option-letter {{ font-weight: 700; margin-right: 6px; }}
    img, svg, table {{ max-width: 100%; height: auto; }}
    .section {{ margin-top: 14px; }}
  </style>
</head>
<body>
  <main>
    <h1>Question {html.escape(meta.question_id)}</h1>
    <table>
      <tr><th>Assessment</th><td>{html.escape(meta.assessment)}</td></tr>
      <tr><th>Test</th><td>{html.escape(meta.test)}</td></tr>
      <tr><th>Domain</th><td>{html.escape(meta.domain)}</td></tr>
      <tr><th>Skill</th><td>{html.escape(meta.skill)} ({html.escape(meta.skill_code)})</td></tr>
      <tr><th>Difficulty</th><td>{html.escape(meta.difficulty)}</td></tr>
      <tr><th>Question Source</th><td>{html.escape(record.source)}</td></tr>
      <tr><th>Original URL</th><td><a href="{html.escape(meta.original_url or '')}" target="_blank" rel="noreferrer">{html.escape(meta.original_url or '')}</a></td></tr>
      <tr><th>External ID</th><td>{html.escape(meta.external_id or "")}</td></tr>
      <tr><th>IBN</th><td>{html.escape(meta.ibn or "")}</td></tr>
      <tr><th>Correct Answer(s)</th><td>{html.escape(correct_answers)}</td></tr>
      {state_standards}
    </table>

    <section class=\"section\">{prompt_html}</section>
    <section class=\"section\">{stem_html}</section>
    {answer_list}
    <section class=\"section\"><h2>Rationale</h2>{rationale_html}</section>
    {parsing_warnings}
  </main>
</body>
</html>
"""

    # ---------- Persistent run state ----------

    def _resolve_root_dir(self, output_dir: Optional[str | Path]) -> Path:
        base = Path(output_dir) if output_dir else self.output_dir
        if base.name == "sat_eqb":
            return base
        return base / "sat_eqb"

    def _ensure_output_layout(self, root_dir: Path) -> Dict[str, Path]:
        data_dir = root_dir / "data"
        questions_dir = data_dir / "questions"
        data_path = root_dir / "data.jsonl"
        data_stats_path = root_dir / "data-stats.json"
        runs_dir = root_dir / "runs"
        todo_dir = root_dir / "todo"
        state_dir = root_dir / "state"
        profiles_dir = state_dir / "profiles"
        history_path = root_dir / "history.jsonl"

        for path in (questions_dir, runs_dir, profiles_dir, todo_dir):
            path.mkdir(parents=True, exist_ok=True)

        legacy_dataset_dir = root_dir / "dataset"
        if legacy_dataset_dir.exists():
            legacy_questions_dir = legacy_dataset_dir / "questions"
            if legacy_questions_dir.exists():
                for legacy_dir in sorted(legacy_questions_dir.glob("*")):
                    if not legacy_dir.is_dir():
                        continue
                    target_dir = questions_dir / legacy_dir.name
                    if target_dir.exists():
                        continue
                    target_dir.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        legacy_dir.rename(target_dir)
                    except OSError:
                        shutil.move(str(legacy_dir), str(target_dir))
                    LOGGER.info("Migrated question directory from %s to %s", legacy_dir, target_dir)

            legacy_dataset_jsonl = root_dir / "dataset.jsonl"
            if legacy_dataset_jsonl.exists() and not data_path.exists():
                try:
                    legacy_dataset_jsonl.rename(data_path)
                except OSError:
                    shutil.move(str(legacy_dataset_jsonl), str(data_path))
                LOGGER.info("Migrated dataset index from %s to %s", legacy_dataset_jsonl, data_path)

            legacy_dataset_stats = root_dir / "dataset-stats.json"
            if legacy_dataset_stats.exists() and not data_stats_path.exists():
                try:
                    legacy_dataset_stats.rename(data_stats_path)
                except OSError:
                    shutil.move(str(legacy_dataset_stats), str(data_stats_path))
                LOGGER.info("Migrated dataset stats from %s to %s", legacy_dataset_stats, data_stats_path)

        return {
            "root_dir": root_dir,
            "data_dir": data_dir,
            "data_path": data_path,
            "data_stats_path": data_stats_path,
            "questions_dir": questions_dir,
            "runs_dir": runs_dir,
            "todo_dir": todo_dir,
            "state_dir": state_dir,
            "profiles_dir": profiles_dir,
            "history_path": history_path,
        }

    def _build_profile_filters(self, *, include_state_standards: bool) -> Dict[str, Any]:
        return {
            "assessment": self.assessment,
            "assessment_id": self._assessment_id_by_name[self.assessment],
            "test": self.test,
            "test_id": self._test_id_by_name[self.test],
            "options": sorted(self.options),
            "difficulties": sorted(self.difficulties) if self.difficulties else None,
            "skills": {key: sorted(value) for key, value in (self.skills or {}).items()} if self.skills else None,
            "exclude_active_questions": self.exclude_active_questions,
            "state": self._state_selection,
            "include_state_standards": include_state_standards,
            "schema_version": SCHEMA_VERSION,
        }

    def _build_profile_id(self, profile_filters: Dict[str, Any]) -> str:
        payload = json.dumps(profile_filters, ensure_ascii=True, sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def _build_run_id(self, run_label: Optional[str]) -> str:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        suffix = hashlib.sha256(f"{time.time_ns()}-{os.getpid()}".encode("utf-8")).hexdigest()[:8]
        if run_label:
            safe = _slugify(run_label)
            if safe:
                return f"{timestamp}-{safe}-{suffix}"
        return f"{timestamp}-{suffix}"

    def _load_profile_state(self, profile_state_path: Path, *, profile_id: str, filters: Dict[str, Any]) -> Dict[str, Any]:
        if profile_state_path.exists():
            try:
                payload = json.loads(profile_state_path.read_text(encoding="utf-8"))
                if payload.get("profile_id") == profile_id and isinstance(payload.get("questions"), dict):
                    return payload
            except json.JSONDecodeError:
                LOGGER.warning("Profile state at %s is invalid JSON; rebuilding", profile_state_path)

        return {
            "state_version": PROFILE_STATE_VERSION,
            "profile_id": profile_id,
            "filters": filters,
            "created_at_utc": _utc_now_iso(),
            "updated_at_utc": _utc_now_iso(),
            "questions": {},
        }

    def _reset_profile_state(self, profile_state: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "state_version": PROFILE_STATE_VERSION,
            "profile_id": profile_state["profile_id"],
            "filters": profile_state["filters"],
            "created_at_utc": _utc_now_iso(),
            "updated_at_utc": _utc_now_iso(),
            "questions": {},
        }

    def _save_profile_state(self, profile_state_path: Path, profile_state: Dict[str, Any]) -> None:
        profile_state["updated_at_utc"] = _utc_now_iso()
        self._write_json_atomic(profile_state_path, profile_state)

    def _determine_question_relative_path(
        self,
        *,
        profile_state: Dict[str, Any],
        question_key: str,
        question_id: str,
    ) -> Path:
        entry = profile_state.get("questions", {}).get(question_key, {})
        existing = entry.get("data_path")
        if existing:
            existing_path = Path(existing)
            parts = existing_path.parts
            if len(parts) >= 3 and parts[0] == "dataset" and parts[1] == "questions":
                return Path("data") / "questions" / parts[-1]
            return existing_path

        digest = hashlib.sha1(question_key.encode("utf-8")).hexdigest()[:12]
        dir_name = f"{question_id or 'question'}-{digest}"
        return Path("data") / "questions" / dir_name

    def _record_success(
        self,
        *,
        profile_state: Dict[str, Any],
        question_key: str,
        row: Dict[str, Any],
        run_id: str,
        question_rel_path: Path,
    ) -> None:
        entry = profile_state.setdefault("questions", {}).setdefault(question_key, {})
        attempts = int(entry.get("attempt_count", 0)) + 1
        successes = int(entry.get("success_count", 0)) + 1

        entry.update(
            {
                "question_id": str(row.get("questionId") or row.get("uId") or ""),
                "external_id": self._clean_nullable(row.get("external_id")),
                "ibn": self._clean_nullable(row.get("ibn")),
                "status": "success",
                "attempt_count": attempts,
                "success_count": successes,
                "failure_count": int(entry.get("failure_count", 0)),
                "last_run_id": run_id,
                "last_attempt_at_utc": _utc_now_iso(),
                "last_success_at_utc": _utc_now_iso(),
                "last_error": None,
                "data_path": question_rel_path.as_posix(),
            }
        )

    def _record_failure(
        self,
        *,
        profile_state: Dict[str, Any],
        question_key: str,
        row: Dict[str, Any],
        run_id: str,
        error_message: str,
    ) -> None:
        entry = profile_state.setdefault("questions", {}).setdefault(question_key, {})
        attempts = int(entry.get("attempt_count", 0)) + 1
        failures = int(entry.get("failure_count", 0)) + 1

        entry.update(
            {
                "question_id": str(row.get("questionId") or row.get("uId") or ""),
                "external_id": self._clean_nullable(row.get("external_id")),
                "ibn": self._clean_nullable(row.get("ibn")),
                "status": "failed",
                "attempt_count": attempts,
                "success_count": int(entry.get("success_count", 0)),
                "failure_count": failures,
                "last_run_id": run_id,
                "last_attempt_at_utc": _utc_now_iso(),
                "last_failure_at_utc": _utc_now_iso(),
                "last_error": error_message,
            }
        )

    # ---------- Run stats ----------

    def _initial_run_stats(
        self,
        *,
        run_id: str,
        run_label: Optional[str],
        root_dir: Path,
        profile_id: str,
        restart: bool,
        save_output: bool,
        download_assets: bool,
        download_new: bool,
        download_failed: bool,
        continue_on_error: bool,
        initial_dataset_count: int,
    ) -> Dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "run": {
                "run_id": run_id,
                "run_label": run_label,
                "profile_id": profile_id,
                "root_dir": str(root_dir),
                "restart": restart,
                "save_output": save_output,
                "download_assets": download_assets,
                "download_new": download_new,
                "download_failed": download_failed,
                "continue_on_error": continue_on_error,
            },
            "timing": {
                "started_at_utc": _utc_now_iso(),
                "query_seconds": 0.0,
                "state_standards_seconds": 0.0,
                "digital_payload_prefetch_seconds": 0.0,
                "processing_seconds": 0.0,
                "finished_at_utc": None,
                "total_seconds": 0.0,
            },
            "selection": {
                "available_count": 0,
                "requested_count": 0,
                "candidate_count": 0,
                "skipped_success_count": 0,
                "skipped_new_disabled_count": 0,
                "skipped_failed_disabled_count": 0,
            },
            "processing": {
                "attempted_count": 0,
                "processed_count": 0,
                "success_count": 0,
                "failed_count": 0,
                "failed_by_error_type": {},
            },
            "sources": {
                "digital": 0,
                "legacy": 0,
            },
            "question_breakdown": {
                "attempted": {
                    "by_domain": {},
                    "by_difficulty": {},
                    "by_source": {},
                },
                "success": {
                    "by_domain": {},
                    "by_difficulty": {},
                    "by_source": {},
                    "by_question_type": {},
                },
                "failed": {
                    "by_domain": {},
                    "by_difficulty": {},
                    "by_source": {},
                },
            },
            "assets": {
                "files_count": 0,
                "total_bytes": 0,
                "by_source_type": {
                    "remote": 0,
                    "data_uri": 0,
                },
            },
            "bytes": {
                "record_json_total": 0,
                "content_html_total": 0,
                "raw_table_row_total": 0,
                "raw_detail_payload_total": 0,
                "written_question_json_total": 0,
                "written_question_html_total": 0,
                "written_question_markdown_total": 0,
            },
            "dataset": {
                "new_count": 0,
                "modified_count": 0,
                "unchanged_count": 0,
                "total_records_after_run": int(initial_dataset_count),
            },
            "parsing": {
                "warnings_count": 0,
            },
            "todo": {
                "total_items": 0,
                "new_signatures": 0,
                "unique_signatures_in_run": 0,
                "by_category": {},
                "by_severity": {},
            },
            "progress": {
                "checkpoints_written": 0,
            },
            "requests": {
                "max_requests_per_second": self.max_requests_per_second,
                "min_request_interval_seconds": self._min_request_interval_seconds,
                "throttle_sleep_seconds": 0.0,
                "total": 0,
                "failed": 0,
                "response_bytes_total": 0,
                "by_endpoint": {},
                "status_codes": {},
            },
        }

    def _record_request_attempt(self, endpoint: str) -> None:
        requests_block = self._run_stats.setdefault("requests", {})
        requests_block["total"] = int(requests_block.get("total", 0)) + 1

        endpoint_map = requests_block.setdefault("by_endpoint", {})
        endpoint_info = endpoint_map.setdefault(
            endpoint,
            {"count": 0, "failed": 0, "total_seconds": 0.0, "response_bytes": 0},
        )
        endpoint_info["count"] += 1

    def _record_request_response(
        self,
        endpoint: str,
        status_code: int,
        elapsed_seconds: float,
        *,
        response_bytes: int = 0,
    ) -> None:
        requests_block = self._run_stats.setdefault("requests", {})

        endpoint_map = requests_block.setdefault("by_endpoint", {})
        endpoint_info = endpoint_map.setdefault(
            endpoint,
            {"count": 0, "failed": 0, "total_seconds": 0.0, "response_bytes": 0},
        )
        endpoint_info["total_seconds"] = round(float(endpoint_info.get("total_seconds", 0.0)) + elapsed_seconds, 6)
        endpoint_info["response_bytes"] = int(endpoint_info.get("response_bytes", 0)) + int(max(response_bytes, 0))

        requests_block["response_bytes_total"] = int(requests_block.get("response_bytes_total", 0)) + int(
            max(response_bytes, 0)
        )

        status_codes = requests_block.setdefault("status_codes", {})
        code_key = str(status_code)
        status_codes[code_key] = int(status_codes.get(code_key, 0)) + 1

    def _record_request_exception(self, endpoint: str) -> None:
        requests_block = self._run_stats.setdefault("requests", {})
        requests_block["failed"] = int(requests_block.get("failed", 0)) + 1

        endpoint_map = requests_block.setdefault("by_endpoint", {})
        endpoint_info = endpoint_map.setdefault(
            endpoint,
            {"count": 0, "failed": 0, "total_seconds": 0.0, "response_bytes": 0},
        )
        endpoint_info["failed"] = int(endpoint_info.get("failed", 0)) + 1

    @staticmethod
    def _response_size_bytes(response: requests.Response) -> int:
        content_length = response.headers.get("Content-Length")
        if content_length and str(content_length).strip().isdigit():
            return int(str(content_length).strip())
        return len(response.content or b"")

    def _classify_endpoint(self, url: str) -> str:
        if url == self.LOOKUP_URL:
            return "lookup"
        if url == self.GET_QUESTIONS_URL:
            return "get_questions"
        if url == self.GET_QUESTION_URL:
            return "get_question"
        if url == self.PDF_DOWNLOAD_URL:
            return "pdf_download"
        if url == self.STATE_STANDARDS_URL:
            return "state_standards"
        if url.startswith(self.LEGACY_DISCLOSED_BASE_URL):
            return "legacy_disclosed"
        return "asset_or_other"

    def _update_asset_stats(self, record: QuestionRecord) -> None:
        assets_block = self._run_stats.setdefault("assets", {})
        assets_block["files_count"] = int(assets_block.get("files_count", 0)) + len(record.assets)

        total_bytes = int(assets_block.get("total_bytes", 0))
        by_source_type = assets_block.setdefault("by_source_type", {})

        for asset in record.assets:
            if asset.size_bytes:
                total_bytes += int(asset.size_bytes)
            source_type = asset.source_type or "unknown"
            by_source_type[source_type] = int(by_source_type.get(source_type, 0)) + 1

        assets_block["total_bytes"] = total_bytes

    def _classify_row_for_stats(self, row: Dict[str, Any]) -> Tuple[str, str, str]:
        domain = str(row.get("primary_class_cd_desc") or "Unknown").strip() or "Unknown"
        difficulty_code = self._clean_nullable(row.get("difficulty")) or ""
        difficulty = self._CODE_TO_DIFFICULTY.get(difficulty_code, difficulty_code or "Unknown")

        external_id = self._clean_nullable(row.get("external_id"))
        ibn = self._clean_nullable(row.get("ibn"))
        if external_id:
            source = "digital"
        elif ibn:
            source = "legacy"
        else:
            source = "unknown"
        return domain, difficulty, source

    def _record_question_breakdown(
        self,
        *,
        outcome: str,
        domain: str,
        difficulty: str,
        source: str,
        question_type: Optional[str] = None,
    ) -> None:
        breakdown = self._run_stats.setdefault("question_breakdown", {})
        outcome_bucket = breakdown.setdefault(outcome, {})
        self._increment_counter(outcome_bucket.setdefault("by_domain", {}), domain or "Unknown")
        self._increment_counter(outcome_bucket.setdefault("by_difficulty", {}), difficulty or "Unknown")
        self._increment_counter(outcome_bucket.setdefault("by_source", {}), source or "Unknown")
        if question_type:
            self._increment_counter(outcome_bucket.setdefault("by_question_type", {}), question_type)

    def _update_record_byte_stats(self, record: QuestionRecord, *, output_sizes: Dict[str, int]) -> None:
        bytes_block = self._run_stats.setdefault("bytes", {})
        record_payload = record.to_dict()
        record_json_bytes = len(json.dumps(record_payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        content_html_bytes = len(
            (record.content.prompt_html or "").encode("utf-8")
            + (record.content.stem_html or "").encode("utf-8")
            + (record.content.rationale_html or "").encode("utf-8")
            + "".join(option.content_html or "" for option in record.content.answer_options).encode("utf-8")
        )
        raw_table_row_bytes = len(
            json.dumps(record.raw_table_row or {}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
        raw_detail_payload_bytes = len(
            json.dumps(record.raw_detail_payload or {}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )

        bytes_block["record_json_total"] = int(bytes_block.get("record_json_total", 0)) + record_json_bytes
        bytes_block["content_html_total"] = int(bytes_block.get("content_html_total", 0)) + content_html_bytes
        bytes_block["raw_table_row_total"] = int(bytes_block.get("raw_table_row_total", 0)) + raw_table_row_bytes
        bytes_block["raw_detail_payload_total"] = int(bytes_block.get("raw_detail_payload_total", 0)) + raw_detail_payload_bytes
        bytes_block["written_question_json_total"] = int(bytes_block.get("written_question_json_total", 0)) + int(
            output_sizes.get("json_bytes", 0)
        )
        bytes_block["written_question_html_total"] = int(bytes_block.get("written_question_html_total", 0)) + int(
            output_sizes.get("html_bytes", 0)
        )
        bytes_block["written_question_markdown_total"] = int(
            bytes_block.get("written_question_markdown_total", 0)
        ) + int(output_sizes.get("markdown_bytes", 0))

    @staticmethod
    def _increment_counter(counter: Dict[str, int], key: str) -> None:
        normalized = key or "Unknown"
        counter[normalized] = int(counter.get(normalized, 0)) + 1

    @staticmethod
    def _increment_named_counter(counter: Dict[str, Any], key: str, *, amount: int = 1) -> None:
        counter[key] = int(counter.get(key, 0)) + amount

    # ---------- Logging + files ----------

    def _attach_run_log_handler(self, run_log_path: Path) -> logging.Handler:
        run_log_path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(run_log_path, encoding="utf-8")
        formatter = logging.Formatter(
            fmt="%(asctime)s %(levelname)s [%(name)s] %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%SZ",
        )
        handler.setFormatter(formatter)
        handler.setLevel(logging.INFO)

        LOGGER.addHandler(handler)
        LOGGER.setLevel(logging.INFO)
        return handler

    def _detach_run_log_handler(self, handler: logging.Handler) -> None:
        LOGGER.removeHandler(handler)
        handler.close()

    def _write_json_atomic(self, path: Path, payload: Dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(path)

    def _append_jsonl(self, path: Path, payload: Dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False))
            handle.write("\n")

    def _write_yaml_atomic(self, path: Path, payload: Dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        yaml_lines = _to_yaml_lines(payload)
        tmp_path.write_text("\n".join(yaml_lines).strip() + "\n", encoding="utf-8")
        tmp_path.replace(path)

    def _write_id_csv(self, path: Path, rows: Sequence[Dict[str, str]]) -> None:
        columns = ("question_key", "question_id", "external_id", "ibn")
        lines = [",".join(columns)]
        for row in rows:
            lines.append(",".join(_csv_escape(str(row.get(column, ""))) for column in columns))

        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        tmp_path.replace(path)

    def _write_run_todo_markdown(
        self,
        path: Path,
        *,
        todo_summary: Dict[str, Any],
        run_errors: Sequence[str],
        new_dataset_rows: Sequence[Dict[str, str]],
        modified_dataset_rows: Sequence[Dict[str, str]],
    ) -> None:
        lines: List[str] = []
        lines.append("# Run TODO Items")
        lines.append("")
        lines.append("This file highlights unanticipated scenarios, errors, and follow-up improvements identified during this run.")
        lines.append("")
        lines.append(f"Run ID: {self._active_run_context.get('run_id', '')}")
        lines.append(f"Generated at: {_utc_now_iso()}")
        lines.append("")

        lines.append("## Summary")
        lines.append(f"- TODO events: {todo_summary.get('total_items', 0)}")
        lines.append(f"- Unique TODO signatures this run: {todo_summary.get('unique_signatures_in_run', 0)}")
        lines.append(f"- New question IDs: {len(new_dataset_rows)}")
        lines.append(f"- Modified question IDs: {len(modified_dataset_rows)}")
        lines.append(f"- Run errors: {len(run_errors)}")
        lines.append(f"- Attempted questions: {self._run_stats.get('processing', {}).get('attempted_count', 0)}")
        lines.append(f"- Successful questions: {self._run_stats.get('processing', {}).get('success_count', 0)}")
        lines.append(f"- Failed questions: {self._run_stats.get('processing', {}).get('failed_count', 0)}")
        lines.append(f"- Downloaded assets: {self._run_stats.get('assets', {}).get('files_count', 0)}")
        lines.append(f"- Asset bytes: {self._run_stats.get('assets', {}).get('total_bytes', 0)}")
        lines.append("")

        lines.append("## Data Changes")
        if not new_dataset_rows and not modified_dataset_rows:
            lines.append("- No dataset additions or modifications were recorded in this run.")
        else:
            if new_dataset_rows:
                lines.append("- New question IDs:")
                for row in new_dataset_rows[:50]:
                    lines.append(f"  - {row.get('question_id', '')} ({row.get('question_key', '')})")
                if len(new_dataset_rows) > 50:
                    lines.append(f"  - ... and {len(new_dataset_rows) - 50} more")
            if modified_dataset_rows:
                lines.append("- Modified question IDs:")
                for row in modified_dataset_rows[:50]:
                    lines.append(f"  - {row.get('question_id', '')} ({row.get('question_key', '')})")
                if len(modified_dataset_rows) > 50:
                    lines.append(f"  - ... and {len(modified_dataset_rows) - 50} more")
        lines.append("")

        lines.append("## Unanticipated Scenarios and Improvements")
        top_signatures = todo_summary.get("top_signatures") or []
        if not top_signatures:
            lines.append("- No TODO anomaly signatures were recorded.")
        else:
            for item in top_signatures:
                lines.append(
                    f"- [{str(item.get('severity', 'warning')).upper()}] {item.get('category', 'uncategorized')}: "
                    f"{item.get('summary', '')} (occurrences={item.get('occurrences', 0)})"
                )
        lines.append("")

        lines.append("## Errors")
        if not run_errors:
            lines.append("- No run errors recorded.")
        else:
            for message in run_errors[:100]:
                lines.append(f"- {message}")
            if len(run_errors) > 100:
                lines.append(f"- ... and {len(run_errors) - 100} more")
        lines.append("")

        lines.append("## References")
        lines.append(f"- Global TODO backlog: {todo_summary.get('todo_markdown_path', '')}")
        lines.append(f"- Global TODO index: {todo_summary.get('todo_index_path', '')}")
        lines.append(f"- Run TODO JSONL: {todo_summary.get('run_todo_items_path', '')}")
        lines.append("")

        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
        tmp_path.replace(path)

    def _load_dataset_index(self, *, data_path: Path, questions_dir: Path) -> Dict[str, Dict[str, Any]]:
        index: Dict[str, Dict[str, Any]] = {}

        if data_path.exists():
            with data_path.open("r", encoding="utf-8") as handle:
                for line_number, raw_line in enumerate(handle, start=1):
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        LOGGER.warning("Ignoring malformed data JSONL line %d in %s", line_number, data_path)
                        continue

                    key = self._question_key_from_record_payload(payload)
                    if not key:
                        LOGGER.warning("Data line %d in %s is missing question key fields", line_number, data_path)
                        continue
                    index[key] = payload
            return index

        root_dir = questions_dir.parent.parent
        legacy_questions_dir = root_dir / "dataset" / "questions"
        search_dirs = [questions_dir]
        if legacy_questions_dir.exists():
            search_dirs.append(legacy_questions_dir)

        for scan_dir in search_dirs:
            for json_path in sorted(scan_dir.glob("*/question.json")):
                effective_json_path = json_path
                if scan_dir == legacy_questions_dir:
                    legacy_dir = json_path.parent
                    target_dir = questions_dir / legacy_dir.name
                    if not target_dir.exists():
                        target_dir.parent.mkdir(parents=True, exist_ok=True)
                        try:
                            legacy_dir.rename(target_dir)
                        except OSError:
                            shutil.move(str(legacy_dir), str(target_dir))
                        LOGGER.info("Migrated legacy question directory from %s to %s", legacy_dir, target_dir)
                    effective_json_path = target_dir / "question.json"

                try:
                    payload = json.loads(effective_json_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    LOGGER.warning("Unable to read existing question payload from %s", effective_json_path, exc_info=True)
                    continue

                key = self._question_key_from_record_payload(payload)
                if not key:
                    continue
                index[key] = payload

        if index:
            self._write_dataset_jsonl(data_path, index)

        return index

    def _write_dataset_jsonl(self, data_path: Path, dataset_index: Dict[str, Dict[str, Any]]) -> None:
        data_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = data_path.with_suffix(data_path.suffix + ".tmp")
        with tmp_path.open("w", encoding="utf-8") as handle:
            for question_key in sorted(dataset_index.keys()):
                payload = dataset_index[question_key]
                normalized_payload = self._sanitize_for_json(payload, max_string_length=None)
                normalized_payload["question_key"] = question_key
                handle.write(json.dumps(normalized_payload, ensure_ascii=False))
                handle.write("\n")
        tmp_path.replace(data_path)

    def _compute_global_dataset_stats(self, dataset_index: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
        by_assessment: Dict[str, int] = {}
        by_test: Dict[str, int] = {}
        by_domain: Dict[str, int] = {}
        by_difficulty: Dict[str, int] = {}
        by_source: Dict[str, int] = {}
        by_question_type: Dict[str, int] = {}
        by_asset_source_type: Dict[str, int] = {}
        by_asset_mime_type: Dict[str, int] = {}
        created_run_ids: Set[str] = set()
        modified_run_ids: Set[str] = set()

        total_assets = 0
        total_asset_bytes = 0
        records_with_assets = 0
        records_with_warnings = 0
        warnings_total = 0
        latest_modified_time: Optional[str] = None
        records_with_original_url = 0

        for payload in dataset_index.values():
            metadata = payload.get("metadata") if isinstance(payload, dict) else {}
            metadata = metadata if isinstance(metadata, dict) else {}

            assessment = str(metadata.get("assessment") or "Unknown")
            test = str(metadata.get("test") or "Unknown")
            domain = str(metadata.get("domain") or "Unknown")
            difficulty = str(metadata.get("difficulty") or "Unknown")
            source = str(payload.get("source") or "Unknown")

            content = payload.get("content") if isinstance(payload, dict) else {}
            content = content if isinstance(content, dict) else {}
            question_type = str(content.get("question_type") or "Unknown")

            self._increment_counter(by_assessment, assessment)
            self._increment_counter(by_test, test)
            self._increment_counter(by_domain, domain)
            self._increment_counter(by_difficulty, difficulty)
            self._increment_counter(by_source, source)
            self._increment_counter(by_question_type, question_type)

            original_url = self._clean_nullable(metadata.get("original_url"))
            if original_url:
                records_with_original_url += 1

            assets = payload.get("assets") if isinstance(payload, dict) else []
            assets = assets if isinstance(assets, list) else []
            if assets:
                records_with_assets += 1
            total_assets += len(assets)
            for asset in assets:
                if not isinstance(asset, dict):
                    continue
                source_type = str(asset.get("source_type") or "unknown")
                self._increment_counter(by_asset_source_type, source_type)

                mime_type = str(asset.get("mime_type") or "unknown")
                self._increment_counter(by_asset_mime_type, mime_type)

                size_bytes = asset.get("size_bytes")
                if isinstance(size_bytes, int):
                    total_asset_bytes += size_bytes

            parse_warnings = payload.get("parse_warnings") if isinstance(payload, dict) else []
            parse_warnings = parse_warnings if isinstance(parse_warnings, list) else []
            if parse_warnings:
                records_with_warnings += 1
                warnings_total += len(parse_warnings)

            lifecycle = payload.get("lifecycle") if isinstance(payload, dict) else {}
            lifecycle = lifecycle if isinstance(lifecycle, dict) else {}
            created_run_id = self._clean_nullable(lifecycle.get("created_run_id"))
            modified_run_id = self._clean_nullable(lifecycle.get("modified_run_id"))
            modified_time = self._clean_nullable(lifecycle.get("modified_time"))

            if created_run_id:
                created_run_ids.add(created_run_id)
            if modified_run_id:
                modified_run_ids.add(modified_run_id)
            if modified_time and (latest_modified_time is None or modified_time > latest_modified_time):
                latest_modified_time = modified_time

        return {
            "schema_version": SCHEMA_VERSION,
            "generated_at_utc": _utc_now_iso(),
            "total_records": len(dataset_index),
            "by_assessment": dict(sorted(by_assessment.items())),
            "by_test": dict(sorted(by_test.items())),
            "by_domain": dict(sorted(by_domain.items())),
            "by_difficulty": dict(sorted(by_difficulty.items())),
            "by_source": dict(sorted(by_source.items())),
            "by_question_type": dict(sorted(by_question_type.items())),
            "assets": {
                "records_with_assets": records_with_assets,
                "total_assets": total_assets,
                "total_asset_bytes": total_asset_bytes,
                "by_source_type": dict(sorted(by_asset_source_type.items())),
                "by_mime_type": dict(sorted(by_asset_mime_type.items())),
            },
            "parsing": {
                "records_with_warnings": records_with_warnings,
                "warnings_total": warnings_total,
            },
            "metadata_completeness": {
                "records_with_original_url": records_with_original_url,
                "records_missing_original_url": len(dataset_index) - records_with_original_url,
            },
            "lifecycle": {
                "created_run_ids_count": len(created_run_ids),
                "modified_run_ids_count": len(modified_run_ids),
                "latest_modified_time": latest_modified_time,
            },
        }

    def _apply_record_lifecycle(
        self,
        *,
        record: QuestionRecord,
        question_dir: Path,
        run_id: str,
        observed_at_utc: str,
    ) -> Tuple[str, QuestionRecord]:
        json_path = question_dir / "question.json"
        existing_file_present = json_path.exists()
        existing_payload: Optional[Dict[str, Any]] = None
        if existing_file_present:
            try:
                existing_payload = json.loads(json_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                LOGGER.warning("Could not read prior question payload from %s; treating as modified", json_path, exc_info=True)

        current_payload = record.to_dict()
        current_payload_core = self._strip_lifecycle_payload(current_payload)
        existing_payload_core = self._strip_lifecycle_payload(existing_payload) if isinstance(existing_payload, dict) else None

        existing_lifecycle = {}
        if isinstance(existing_payload, dict) and isinstance(existing_payload.get("lifecycle"), dict):
            existing_lifecycle = existing_payload["lifecycle"]

        lifecycle_complete = (
            bool(existing_lifecycle.get("created_run_id"))
            and bool(existing_lifecycle.get("modified_run_id"))
            and bool(existing_lifecycle.get("create_time"))
            and bool(existing_lifecycle.get("modified_time"))
        )

        if existing_payload is None:
            status = "modified" if existing_file_present else "new"
        elif not lifecycle_complete:
            status = "modified"
        else:
            previous_digest = self._payload_digest(existing_payload_core)
            current_digest = self._payload_digest(current_payload_core)
            status = "modified" if previous_digest != current_digest else "unchanged"

        created_run_id = str(existing_lifecycle.get("created_run_id") or run_id)
        create_time = str(existing_lifecycle.get("create_time") or observed_at_utc)
        if status == "unchanged":
            modified_run_id = str(existing_lifecycle.get("modified_run_id") or created_run_id)
            modified_time = str(existing_lifecycle.get("modified_time") or create_time)
        else:
            modified_run_id = run_id
            modified_time = observed_at_utc

        record.lifecycle = {
            "created_run_id": created_run_id,
            "modified_run_id": modified_run_id,
            "create_time": create_time,
            "modified_time": modified_time,
        }
        return status, record

    @staticmethod
    def _strip_lifecycle_payload(payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        if not isinstance(payload, dict):
            return {}
        cleaned = dict(payload)
        cleaned.pop("lifecycle", None)
        cleaned.pop("question_key", None)
        return cleaned

    @staticmethod
    def _payload_digest(payload: Dict[str, Any]) -> str:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _question_key_from_record_payload(payload: Dict[str, Any]) -> Optional[str]:
        metadata = payload.get("metadata") if isinstance(payload, dict) else None
        if not isinstance(metadata, dict):
            return None

        external_id = Scraper._clean_nullable(metadata.get("external_id"))
        if external_id:
            return f"external:{external_id}"

        ibn = Scraper._clean_nullable(metadata.get("ibn"))
        if ibn:
            return f"ibn:{ibn}"

        question_id = Scraper._clean_nullable(metadata.get("question_id"))
        if question_id:
            return f"question:{question_id}"
        return None

    # ---------- Utilities ----------

    @staticmethod
    def _question_key_from_row(row: Dict[str, Any]) -> str:
        external_id = Scraper._clean_nullable(row.get("external_id"))
        if external_id:
            return f"external:{external_id}"

        ibn = Scraper._clean_nullable(row.get("ibn"))
        if ibn:
            return f"ibn:{ibn}"

        qid = Scraper._clean_nullable(row.get("questionId")) or Scraper._clean_nullable(row.get("uId")) or "unknown"
        return f"question:{qid}"

    @staticmethod
    def _clean_nullable(value: Any) -> Optional[str]:
        if value is None:
            return None
        value_str = str(value).strip()
        return value_str or None


def _natural_sort_key(value: str) -> List[Any]:
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", value)]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slugify(value: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip())
    safe = safe.strip("-")
    return safe[:64]


def _to_yaml_lines(value: Any, *, indent: int = 0) -> List[str]:
    prefix = " " * indent

    if isinstance(value, dict):
        if not value:
            return [f"{prefix}{{}}"]
        lines: List[str] = []
        for key, item in value.items():
            safe_key = str(key)
            if isinstance(item, (dict, list)):
                lines.append(f"{prefix}{safe_key}:")
                lines.extend(_to_yaml_lines(item, indent=indent + 2))
            else:
                lines.append(f"{prefix}{safe_key}: {_yaml_scalar(item)}")
        return lines

    if isinstance(value, list):
        if not value:
            return [f"{prefix}[]"]
        lines = []
        for item in value:
            if isinstance(item, (dict, list)):
                lines.append(f"{prefix}-")
                lines.extend(_to_yaml_lines(item, indent=indent + 2))
            else:
                lines.append(f"{prefix}- {_yaml_scalar(item)}")
        return lines

    return [f"{prefix}{_yaml_scalar(value)}"]


def _yaml_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    return json.dumps(text, ensure_ascii=False)


def _csv_escape(value: str) -> str:
    if any(char in value for char in [",", "\"", "\n", "\r"]):
        return "\"" + value.replace("\"", "\"\"") + "\""
    return value
