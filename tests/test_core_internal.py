from __future__ import annotations

import json
from pathlib import Path

import pytest
import requests

from college_board_scraper.core import Scraper
from college_board_scraper.models import AnswerOption, QuestionContent, QuestionMetadata, QuestionRecord


def _scraper_stub() -> Scraper:
    scraper = Scraper.__new__(Scraper)
    scraper.max_requests_per_second = 3.0
    scraper._min_request_interval_seconds = 1.0 / 3.0
    scraper._run_stats = {}
    scraper._active_run_todo_counts = {"total_items": 0}
    scraper._active_run_todo_signatures = set()
    return scraper


def test_ensure_output_layout_uses_data_paths_and_migrates_legacy_dataset(tmp_path: Path) -> None:
    scraper = _scraper_stub()
    root_dir = tmp_path / "sat_eqb"

    legacy_question_dir = root_dir / "dataset" / "questions" / "legacy-q1"
    legacy_question_dir.mkdir(parents=True, exist_ok=True)
    (legacy_question_dir / "question.json").write_text('{"metadata": {"question_id": "1"}}', encoding="utf-8")
    (root_dir / "dataset.jsonl").write_text("{}", encoding="utf-8")
    (root_dir / "dataset-stats.json").write_text("{}", encoding="utf-8")

    paths = scraper._ensure_output_layout(root_dir)

    assert paths["data_dir"] == root_dir / "data"
    assert paths["questions_dir"] == root_dir / "data" / "questions"
    assert paths["data_path"] == root_dir / "data.jsonl"
    assert paths["data_stats_path"] == root_dir / "data-stats.json"
    assert (root_dir / "data" / "questions" / "legacy-q1" / "question.json").exists()
    assert (root_dir / "data.jsonl").exists()
    assert (root_dir / "data-stats.json").exists()


def test_determine_question_relative_path_migrates_legacy_dataset_reference() -> None:
    scraper = _scraper_stub()

    profile_state = {
        "questions": {
            "external:abc": {"data_path": "dataset/questions/q1-dir"},
            "external:def": {"data_path": "data/questions/q2-dir"},
        }
    }

    migrated_path = scraper._determine_question_relative_path(
        profile_state=profile_state,
        question_key="external:abc",
        question_id="1",
    )
    unchanged_path = scraper._determine_question_relative_path(
        profile_state=profile_state,
        question_key="external:def",
        question_id="2",
    )

    assert migrated_path == Path("data/questions/q1-dir")
    assert unchanged_path == Path("data/questions/q2-dir")


def test_compute_global_dataset_stats_counts_assets_breakdowns_and_lifecycle() -> None:
    scraper = _scraper_stub()

    dataset_index = {
        "external:a": {
            "metadata": {
                "assessment": "SAT",
                "test": "Math",
                "domain": "Algebra",
                "difficulty": "Easy",
                "original_url": "https://example.org/q1",
            },
            "source": "digital",
            "content": {"question_type": "MCQ"},
            "assets": [
                {"source_type": "remote", "mime_type": "image/png", "size_bytes": 10},
                {"source_type": "data_uri", "mime_type": "image/svg+xml", "size_bytes": 20},
            ],
            "parse_warnings": ["warn-a"],
            "lifecycle": {
                "created_run_id": "run-1",
                "modified_run_id": "run-2",
                "modified_time": "2026-01-01T00:00:00+00:00",
            },
        },
        "ibn:b": {
            "metadata": {
                "assessment": "SAT",
                "test": "Reading and Writing",
                "domain": "Craft and Structure",
                "difficulty": "Hard",
            },
            "source": "legacy",
            "content": {"question_type": "SPR"},
            "assets": [],
            "parse_warnings": [],
            "lifecycle": {
                "created_run_id": "run-1",
                "modified_run_id": "run-1",
                "modified_time": "2026-01-02T00:00:00+00:00",
            },
        },
    }

    stats = scraper._compute_global_dataset_stats(dataset_index)

    assert stats["total_records"] == 2
    assert stats["by_assessment"]["SAT"] == 2
    assert stats["by_source"]["digital"] == 1
    assert stats["by_source"]["legacy"] == 1
    assert stats["assets"]["total_assets"] == 2
    assert stats["assets"]["total_asset_bytes"] == 30
    assert stats["assets"]["by_source_type"]["remote"] == 1
    assert stats["assets"]["by_source_type"]["data_uri"] == 1
    assert stats["assets"]["by_mime_type"]["image/png"] == 1
    assert stats["parsing"]["records_with_warnings"] == 1
    assert stats["parsing"]["warnings_total"] == 1
    assert stats["metadata_completeness"]["records_with_original_url"] == 1
    assert stats["metadata_completeness"]["records_missing_original_url"] == 1
    assert stats["lifecycle"]["created_run_ids_count"] == 1
    assert stats["lifecycle"]["modified_run_ids_count"] == 2
    assert stats["lifecycle"]["latest_modified_time"] == "2026-01-02T00:00:00+00:00"


def test_response_size_bytes_prefers_header_but_falls_back_to_content() -> None:
    response_with_length = requests.Response()
    response_with_length._content = b"abc"
    response_with_length.headers = {"Content-Length": "15"}
    assert Scraper._response_size_bytes(response_with_length) == 15

    response_without_length = requests.Response()
    response_without_length._content = b"abcdef"
    response_without_length.headers = {}
    assert Scraper._response_size_bytes(response_without_length) == 6


def test_write_run_progress_does_not_raise_on_write_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    scraper = _scraper_stub()
    scraper._run_stats = {
        "selection": {},
        "processing": {},
        "question_breakdown": {},
        "assets": {},
        "bytes": {},
        "dataset": {},
        "progress": {"checkpoints_written": 0},
    }

    def _raise_write(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated write failure")

    monkeypatch.setattr(scraper, "_write_json_atomic", _raise_write)

    scraper._write_run_progress(
        stage="processing",
        run_id="run-1",
        profile_id="profile-1",
        run_dir=tmp_path,
    )

    assert scraper._run_stats["progress"]["checkpoints_written"] == 0


def test_load_dataset_index_skips_invalid_question_json(tmp_path: Path) -> None:
    scraper = _scraper_stub()
    root = tmp_path / "sat_eqb"
    questions_dir = root / "data" / "questions"
    bad_question = questions_dir / "bad-q"
    bad_question.mkdir(parents=True, exist_ok=True)
    (bad_question / "question.json").write_text("{not-json", encoding="utf-8")

    data_path = root / "data.jsonl"
    dataset_index = scraper._load_dataset_index(data_path=data_path, questions_dir=questions_dir)

    assert dataset_index == {}
    assert not data_path.exists()


def test_write_dataset_jsonl_preserves_long_content_strings(tmp_path: Path) -> None:
    scraper = _scraper_stub()
    data_path = tmp_path / "data.jsonl"
    long_stem = "X" * 7000
    dataset_index = {
        "external:abc": {
            "metadata": {"external_id": "abc", "question_id": "q1"},
            "content": {"stem_html": long_stem},
            "source": "digital",
        }
    }

    scraper._write_dataset_jsonl(data_path, dataset_index)

    rows = [json.loads(line) for line in data_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(rows) == 1
    assert rows[0]["content"]["stem_html"] == long_stem
    assert not rows[0]["content"]["stem_html"].endswith("...")


def test_write_question_output_writes_markdown_file(tmp_path: Path) -> None:
    scraper = _scraper_stub()
    metadata = QuestionMetadata(
        question_id="q1",
        assessment="SAT",
        assessment_id=99,
        test="Math",
        test_id=2,
        domain="Algebra",
        domain_code="H",
        skill="Linear equations in one variable",
        skill_code="H.A.",
        difficulty="Easy",
        score_band_range=2,
        external_id="00000000-1111-2222-3333-444444444444",
        ibn=None,
        program="SAT",
        create_date=1700000000000,
        update_date=1700000000000,
        state_standards=["A-CED.2"],
        original_url="https://satsuiteeducatorquestionbank.collegeboard.org/digital/results?question_id=q1",
    )
    record = QuestionRecord(
        metadata=metadata,
        source="digital",
        content=QuestionContent(
            prompt_html="<p>Prompt</p>",
            stem_html="<p>Stem</p>",
            answer_options=[AnswerOption(letter="A", content_html="<p>1</p>")],
            rationale_html="<p>Rationale</p>",
            correct_answers=["A"],
            question_type="mcq",
        ),
    )

    output_sizes = scraper._write_question_output(record, tmp_path / "q1")

    assert (tmp_path / "q1" / "question.json").exists()
    assert (tmp_path / "q1" / "question.html").exists()
    assert (tmp_path / "q1" / "question.md").exists()
    markdown = (tmp_path / "q1" / "question.md").read_text(encoding="utf-8")
    assert "Original URL:" in markdown
    assert output_sizes["markdown_bytes"] > 0


def test_sanitize_for_json_default_truncates_long_strings() -> None:
    scraper = _scraper_stub()
    payload = {"value": "a" * 3000}
    sanitized = scraper._sanitize_for_json(payload)
    assert len(sanitized["value"]) == 2000
    assert sanitized["value"].endswith("...")


def test_sanitize_for_json_can_disable_truncation() -> None:
    scraper = _scraper_stub()
    payload = {"value": "a" * 3000}
    sanitized = scraper._sanitize_for_json(payload, max_string_length=None)
    assert len(sanitized["value"]) == 3000


def test_inspect_content_layout_ignores_plain_instructional_text(monkeypatch: pytest.MonkeyPatch) -> None:
    scraper = _scraper_stub()
    recorded: list[str] = []

    def _capture(**kwargs: object) -> None:
        category = str(kwargs.get("category"))
        recorded.append(category)

    monkeypatch.setattr(scraper, "_record_todo_item", _capture)
    content = QuestionContent(
        prompt_html="<p>This instructional app supports language learning.</p>",
        stem_html="",
        answer_options=[],
        rationale_html="<p>The paper includes footnotes in discussion text.</p>",
        correct_answers=[],
        question_type="mcq",
    )

    scraper._inspect_content_layout(
        content=content,
        source="digital",
        question_key="external:test",
        question_id="q1",
    )

    assert "instruction_marker_detected" not in recorded
    assert "footnote_marker_detected" not in recorded


def test_inspect_content_layout_flags_structural_instruction_and_footnote_markers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scraper = _scraper_stub()
    recorded: list[str] = []

    def _capture(**kwargs: object) -> None:
        category = str(kwargs.get("category"))
        recorded.append(category)

    monkeypatch.setattr(scraper, "_record_todo_item", _capture)
    content = QuestionContent(
        prompt_html="<h2>Instructions:</h2><p>Read the text.</p>",
        stem_html='<p>Example<a href=\"#fn1\">1</a></p>',
        answer_options=[],
        rationale_html="",
        correct_answers=[],
        question_type="mcq",
    )

    scraper._inspect_content_layout(
        content=content,
        source="digital",
        question_key="external:test2",
        question_id="q2",
    )

    assert "instruction_marker_detected" in recorded
    assert "footnote_marker_detected" in recorded


def test_inspect_content_layout_ignores_resolved_footnote_refs(monkeypatch: pytest.MonkeyPatch) -> None:
    scraper = _scraper_stub()
    recorded: list[str] = []

    def _capture(**kwargs: object) -> None:
        category = str(kwargs.get("category"))
        recorded.append(category)

    monkeypatch.setattr(scraper, "_record_todo_item", _capture)
    content = QuestionContent(
        prompt_html='<p><a href=\"#fn1\">1</a></p><p id=\"fn1\">note text</p>',
        stem_html="",
        answer_options=[],
        rationale_html="",
        correct_answers=[],
        question_type="mcq",
    )

    scraper._inspect_content_layout(
        content=content,
        source="digital",
        question_key="external:test3",
        question_id="q3",
    )

    assert "footnote_marker_detected" not in recorded
