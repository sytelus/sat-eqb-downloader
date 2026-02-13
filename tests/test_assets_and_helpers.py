from __future__ import annotations

from pathlib import Path

import requests

from college_board_scraper.assets import AssetDownloader
from college_board_scraper.helpers import chunked
from college_board_scraper.models import QuestionContent, QuestionMetadata, QuestionRecord


_MINIMAL_METADATA = QuestionMetadata(
    question_id="q1",
    assessment="SAT",
    assessment_id=99,
    test="Math",
    test_id=2,
    domain="Algebra",
    domain_code="H",
    skill="Linear functions",
    skill_code="H.B.",
    difficulty="Easy",
    score_band_range=2,
    external_id=None,
    ibn="123",
    program="SAT",
    create_date=None,
    update_date=None,
)


def test_chunked_returns_expected_slices() -> None:
    values = [1, 2, 3, 4, 5]
    chunks = list(chunked(values, 2))
    assert chunks == [[1, 2], [3, 4], [5]]


def test_chunked_rejects_non_positive_size() -> None:
    try:
        list(chunked([1, 2], 0))
        raise AssertionError("Expected ValueError for chunk size=0")
    except ValueError:
        pass


def test_asset_downloader_rewrites_data_uri(tmp_path: Path) -> None:
    tiny_png = (
        "data:image/png;base64,"
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO7+1c4AAAAASUVORK5CYII="
    )

    record = QuestionRecord(
        metadata=_MINIMAL_METADATA,
        source="legacy",
        content=QuestionContent(
            prompt_html=f'<p><img src="{tiny_png}" alt="tiny"></p>',
            stem_html="",
            answer_options=[],
            rationale_html="",
            correct_answers=[],
            question_type="Multiple Choice",
        ),
        raw_payload={},
    )

    downloader = AssetDownloader(
        requests.Session(),
        site_base_url="https://satsuitequestionbank.collegeboard.org/",
    )

    question_dir = tmp_path / "question"
    rewritten = downloader.rewrite_question_assets(record, question_dir)

    assert rewritten.assets
    assert rewritten.assets[0].source_type == "data_uri"
    assert "assets/" in rewritten.content.prompt_html
    assert "data:image/png" not in rewritten.content.prompt_html

    asset_file = question_dir / rewritten.assets[0].local_path
    assert asset_file.exists()
    assert asset_file.stat().st_size > 0


def test_asset_downloader_reports_unsupported_asset_url(tmp_path: Path) -> None:
    anomalies = []

    record = QuestionRecord(
        metadata=_MINIMAL_METADATA,
        source="legacy",
        content=QuestionContent(
            prompt_html='<p><img src="ftp://example.com/asset.png" alt="unsupported"></p>',
            stem_html="",
            answer_options=[],
            rationale_html="",
            correct_answers=[],
            question_type="Multiple Choice",
        ),
        raw_payload={},
    )

    downloader = AssetDownloader(
        requests.Session(),
        site_base_url="https://satsuitequestionbank.collegeboard.org/",
        anomaly_callback=anomalies.append,
    )

    rewritten = downloader.rewrite_question_assets(record, tmp_path / "question", question_key="ibn:123")
    assert rewritten.assets == []
    assert anomalies
    assert anomalies[0]["category"] == "unsupported_asset_url"
    assert anomalies[0]["question_key"] == "ibn:123"
    assert anomalies[0]["question_id"] == "q1"
