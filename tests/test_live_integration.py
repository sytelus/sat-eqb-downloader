from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from college_board_scraper import Scraper


RUN_LIVE_TESTS = os.getenv("RUN_LIVE_TESTS") == "1"


pytestmark = pytest.mark.skipif(
    not RUN_LIVE_TESTS,
    reason="Set RUN_LIVE_TESTS=1 to run network integration tests",
)


def test_live_download_includes_assets_and_metadata(tmp_path: Path) -> None:
    scraper = Scraper(
        assessment="SAT",
        test="Math",
        options={"Algebra"},
        state="CA",
        exclude_active_questions=True,
        output_dir=tmp_path,
    )

    records = scraper.scrape(
        amount=6,
        output_dir=tmp_path,
        save_output=True,
        download_assets=True,
        continue_on_error=False,
    )

    assert len(records) == 6
    assert not scraper.last_errors

    summary = scraper.last_run_summary
    run_dir = Path(summary["run_dir"])
    assert run_dir.exists()
    assert (run_dir / "run-summary.json").exists()
    assert (run_dir / "run-stats.json").exists()
    assert (run_dir / "stats.yaml").exists()
    assert (run_dir / "run-progress.json").exists()
    assert (run_dir / "todo-summary.json").exists()
    assert (run_dir / "todo-items.jsonl").exists()
    assert (run_dir / "todo-items.md").exists()
    assert (run_dir / "new_ids.csv").exists()
    assert (run_dir / "modified_ids.csv").exists()
    assert "todo" in summary
    assert "todo_markdown_path" in summary["todo"]
    assert Path(summary["todo"]["todo_markdown_path"]).exists()
    assert "dataset" in summary
    assert "dataset_path" in summary
    assert Path(summary["dataset_path"]).exists()

    root_dir = Path(summary["root_dir"])
    assert root_dir == tmp_path / "sat_eqb"
    assert (root_dir / "history.jsonl").exists()
    assert (root_dir / "state" / "latest-run.json").exists()
    assert (root_dir / "todo" / "TODO.md").exists()
    assert (root_dir / "todo" / "todo-index.json").exists()
    assert (root_dir / "todo" / "todo-items.jsonl").exists()

    state_payload = json.loads(Path(summary["profile_state_path"]).read_text(encoding="utf-8"))
    question_state = state_payload["questions"]

    for record in records:
        assert record.metadata.question_id
        assert record.metadata.skill_code
        assert record.metadata.domain
        assert record.metadata.difficulty in {"Easy", "Medium", "Hard"}
        assert record.metadata.external_id or record.metadata.ibn
        assert record.content.stem_html

        if record.metadata.external_id:
            question_key = f"external:{record.metadata.external_id}"
        else:
            question_key = f"ibn:{record.metadata.ibn}"

        data_path = question_state[question_key]["data_path"]
        question_dir = root_dir / data_path
        assert question_dir.exists()
        assert (question_dir / "question.json").exists()
        question_payload = json.loads((question_dir / "question.json").read_text(encoding="utf-8"))
        lifecycle = question_payload.get("lifecycle", {})
        assert lifecycle.get("created_run_id")
        assert lifecycle.get("modified_run_id")
        assert lifecycle.get("create_time")
        assert lifecycle.get("modified_time")

        for asset in record.assets:
            asset_path = question_dir / asset.local_path
            assert asset_path.exists()

    second_records = scraper.scrape(
        amount=6,
        output_dir=tmp_path,
        save_output=True,
        download_assets=True,
        continue_on_error=False,
    )
    assert len(second_records) == 0
    second_summary = scraper.last_run_summary
    assert second_summary["selection"]["candidate_count"] == 0
    assert second_summary["selection"]["skipped_success_count"] >= 6


def test_live_long_content_not_truncated(tmp_path: Path) -> None:
    scraper = Scraper(
        assessment="SAT",
        test="Math",
        options={"Algebra", "Advanced Math", "Problem-Solving and Data Analysis", "Geometry and Trigonometry"},
        output_dir=tmp_path,
    )

    records = scraper.scrape(
        amount=40,
        output_dir=tmp_path,
        save_output=False,
        download_assets=False,
        continue_on_error=False,
    )

    assert len(records) == 40
    max_stem_length = max(len(record.content.stem_html or "") for record in records)
    max_rationale_length = max(len(record.content.rationale_html or "") for record in records)

    # Legacy long-form questions regularly exceed this threshold.
    assert max_stem_length > 5000
    assert max_rationale_length > 5000
