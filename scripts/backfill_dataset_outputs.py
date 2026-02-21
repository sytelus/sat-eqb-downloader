#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from college_board_scraper.core import Scraper
from college_board_scraper.markdown_export import render_question_markdown
from college_board_scraper.models import AnswerOption, DownloadedAsset, QuestionContent, QuestionMetadata, QuestionRecord
from college_board_scraper.urls import build_original_question_url


def _clean_nullable(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _to_int(value: Any, *, default: int = 0) -> int:
    if isinstance(value, int):
        return value
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_optional_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _derive_question_key(payload: Dict[str, Any]) -> Optional[str]:
    question_key = _clean_nullable(payload.get("question_key"))
    if question_key:
        return question_key

    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    external_id = _clean_nullable(metadata.get("external_id"))
    if external_id:
        return f"external:{external_id}"
    ibn = _clean_nullable(metadata.get("ibn"))
    if ibn:
        return f"ibn:{ibn}"
    question_id = _clean_nullable(metadata.get("question_id"))
    if question_id:
        return f"question:{question_id}"
    return None


def _record_from_payload(payload: Dict[str, Any]) -> QuestionRecord:
    metadata_dict = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    content_dict = payload.get("content") if isinstance(payload.get("content"), dict) else {}
    assets_list = payload.get("assets") if isinstance(payload.get("assets"), list) else []

    answer_options: list[AnswerOption] = []
    for item in content_dict.get("answer_options") or []:
        if not isinstance(item, dict):
            continue
        answer_options.append(
            AnswerOption(
                letter=str(item.get("letter") or ""),
                content_html=str(item.get("content_html") or ""),
            )
        )

    assets: list[DownloadedAsset] = []
    for item in assets_list:
        if not isinstance(item, dict):
            continue
        assets.append(
            DownloadedAsset(
                original_url=str(item.get("original_url") or ""),
                local_path=str(item.get("local_path") or ""),
                source_type=str(item.get("source_type") or ""),
                mime_type=_clean_nullable(item.get("mime_type")),
                size_bytes=_to_optional_int(item.get("size_bytes")),
                sha256=_clean_nullable(item.get("sha256")),
            )
        )

    metadata = QuestionMetadata(
        question_id=str(metadata_dict.get("question_id") or ""),
        assessment=str(metadata_dict.get("assessment") or ""),
        assessment_id=_to_int(metadata_dict.get("assessment_id")),
        test=str(metadata_dict.get("test") or ""),
        test_id=_to_int(metadata_dict.get("test_id")),
        domain=str(metadata_dict.get("domain") or ""),
        domain_code=str(metadata_dict.get("domain_code") or ""),
        skill=str(metadata_dict.get("skill") or ""),
        skill_code=str(metadata_dict.get("skill_code") or ""),
        difficulty=str(metadata_dict.get("difficulty") or ""),
        score_band_range=_to_optional_int(metadata_dict.get("score_band_range")),
        external_id=_clean_nullable(metadata_dict.get("external_id")),
        ibn=_clean_nullable(metadata_dict.get("ibn")),
        program=_clean_nullable(metadata_dict.get("program")),
        create_date=_to_optional_int(metadata_dict.get("create_date")),
        update_date=_to_optional_int(metadata_dict.get("update_date")),
        state_standards=[
            str(value).strip()
            for value in (metadata_dict.get("state_standards") or [])
            if str(value).strip()
        ],
        original_url=_clean_nullable(metadata_dict.get("original_url")),
    )

    content = QuestionContent(
        prompt_html=str(content_dict.get("prompt_html") or ""),
        stem_html=str(content_dict.get("stem_html") or ""),
        answer_options=answer_options,
        rationale_html=str(content_dict.get("rationale_html") or ""),
        correct_answers=[
            str(value).strip()
            for value in (content_dict.get("correct_answers") or [])
            if str(value).strip()
        ],
        question_type=_clean_nullable(content_dict.get("question_type")),
    )

    return QuestionRecord(
        metadata=metadata,
        source=str(payload.get("source") or ""),
        content=content,
        assets=assets,
        parse_warnings=[str(value) for value in (payload.get("parse_warnings") or [])],
        raw_table_row=payload.get("raw_table_row") if isinstance(payload.get("raw_table_row"), dict) else {},
        raw_detail_payload=payload.get("raw_detail_payload")
        if isinstance(payload.get("raw_detail_payload"), dict)
        else {},
        raw_payload=payload.get("raw_payload") if isinstance(payload.get("raw_payload"), dict) else {},
        lifecycle=payload.get("lifecycle") if isinstance(payload.get("lifecycle"), dict) else {},
    )


def _iter_dataset_rows(data_path: Path) -> Iterable[Dict[str, Any]]:
    with data_path.open("r", encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                raise RuntimeError(f"Invalid JSON in {data_path} line {line_number}")
            if not isinstance(payload, dict):
                raise RuntimeError(f"Expected object in {data_path} line {line_number}")
            yield payload


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Backfill existing SAT EQB dataset records with metadata.original_url and question.md output, "
            "then rewrite data.jsonl and data-stats.json."
        )
    )
    parser.add_argument(
        "--root-dir",
        required=True,
        help="Path to sat_eqb output root (contains data.jsonl and data/questions).",
    )
    parser.add_argument(
        "--refresh-original-url",
        action="store_true",
        help="Recompute original URL for all records even if already present.",
    )
    args = parser.parse_args()

    root_dir = Path(args.root_dir).resolve()
    data_path = root_dir / "data.jsonl"
    questions_dir = root_dir / "data" / "questions"

    if not data_path.exists():
        raise RuntimeError(f"Missing dataset index: {data_path}")
    if not questions_dir.exists():
        raise RuntimeError(f"Missing questions directory: {questions_dir}")

    dataset_index: dict[str, Dict[str, Any]] = {}
    for row in _iter_dataset_rows(data_path):
        question_key = _derive_question_key(row)
        if not question_key:
            continue
        row["question_key"] = question_key
        dataset_index[question_key] = row

    question_path_by_key: dict[str, Path] = {}
    for question_json in sorted(questions_dir.glob("*/question.json")):
        try:
            payload = json.loads(question_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        key = _derive_question_key(payload)
        if key:
            question_path_by_key[key] = question_json.parent

    updated_original_url_count = 0
    markdown_written_count = 0
    missing_question_dir_count = 0

    for question_key in sorted(dataset_index.keys()):
        payload = dataset_index[question_key]
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        source = str(payload.get("source") or "")

        current_original_url = _clean_nullable(metadata.get("original_url"))
        desired_original_url = build_original_question_url(
            source=source,
            assessment=str(metadata.get("assessment") or ""),
            test=str(metadata.get("test") or ""),
            question_id=str(metadata.get("question_id") or ""),
            external_id=_clean_nullable(metadata.get("external_id")),
            ibn=_clean_nullable(metadata.get("ibn")),
            domain=_clean_nullable(metadata.get("domain")),
            domain_code=_clean_nullable(metadata.get("domain_code")),
            skill_code=_clean_nullable(metadata.get("skill_code")),
            difficulty=_clean_nullable(metadata.get("difficulty")),
        )

        if args.refresh_original_url or not current_original_url:
            if metadata.get("original_url") != desired_original_url:
                metadata["original_url"] = desired_original_url
                updated_original_url_count += 1
        payload["metadata"] = metadata
        payload["raw_payload"] = payload.get("raw_detail_payload") or payload.get("raw_payload") or {}

        question_dir = question_path_by_key.get(question_key)
        if question_dir is None:
            missing_question_dir_count += 1
            continue

        record = _record_from_payload(payload)
        markdown_text = render_question_markdown(record)
        markdown_path = question_dir / "question.md"
        markdown_path.write_text(markdown_text, encoding="utf-8")
        markdown_written_count += 1

        _write_json(question_dir / "question.json", payload)

    # Rewrite data.jsonl deterministically.
    data_tmp = data_path.with_suffix(data_path.suffix + ".tmp")
    with data_tmp.open("w", encoding="utf-8") as handle:
        for question_key in sorted(dataset_index.keys()):
            row = dataset_index[question_key]
            row["question_key"] = question_key
            handle.write(json.dumps(row, ensure_ascii=False))
            handle.write("\n")
    data_tmp.replace(data_path)

    # Recompute data-stats.json using scraper helper.
    scraper_stub = Scraper.__new__(Scraper)
    stats_payload = scraper_stub._compute_global_dataset_stats(dataset_index)
    _write_json(root_dir / "data-stats.json", stats_payload)

    print(
        json.dumps(
            {
                "root_dir": str(root_dir),
                "total_records": len(dataset_index),
                "updated_original_url_count": updated_original_url_count,
                "markdown_written_count": markdown_written_count,
                "missing_question_dir_count": missing_question_dir_count,
                "data_path": str(data_path),
                "data_stats_path": str(root_dir / "data-stats.json"),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
