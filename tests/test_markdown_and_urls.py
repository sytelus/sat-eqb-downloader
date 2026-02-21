from __future__ import annotations

from pathlib import Path

from college_board_scraper.markdown_export import render_question_markdown
from college_board_scraper.models import AnswerOption, QuestionContent, QuestionMetadata, QuestionRecord
from college_board_scraper.urls import build_original_question_url


def _metadata_stub() -> QuestionMetadata:
    return QuestionMetadata(
        question_id="abc123ef",
        assessment="SAT",
        assessment_id=99,
        test="Math",
        test_id=2,
        domain="Algebra",
        domain_code="H",
        skill="Linear equations in one variable",
        skill_code="H.A.",
        difficulty="Medium",
        score_band_range=4,
        external_id="11111111-2222-3333-4444-555555555555",
        ibn=None,
        program="SAT",
        create_date=1700000000000,
        update_date=1700000000000,
        state_standards=["A-CED.2", "A-REI.1"],
        original_url="https://satsuiteeducatorquestionbank.collegeboard.org/digital/results?question_id=abc123ef",
    )


def test_build_original_question_url_includes_identifiers() -> None:
    url = build_original_question_url(
        source="digital",
        assessment="SAT",
        test="Math",
        question_id="abc123ef",
        external_id="11111111-2222-3333-4444-555555555555",
        ibn=None,
        domain="Algebra",
        domain_code="H",
        skill_code="H.A.",
        difficulty="Medium",
    )

    assert url.startswith("https://satsuiteeducatorquestionbank.collegeboard.org/digital/results?")
    assert "question_id=abc123ef" in url
    assert "external_id=11111111-2222-3333-4444-555555555555" in url
    assert "domain_code=H" in url


def test_render_question_markdown_includes_math_choices_and_metadata() -> None:
    record = QuestionRecord(
        metadata=_metadata_stub(),
        source="digital",
        content=QuestionContent(
            prompt_html="",
            stem_html="<p><math><mfrac><mn>1</mn><mn>2</mn></mfrac></math></p><p>Solve for <math><mi>x</mi></math>.</p>",
            answer_options=[
                AnswerOption(letter="A", content_html="<p>1</p>"),
                AnswerOption(letter="B", content_html="<p>2</p>"),
            ],
            rationale_html="<p>Use substitution.</p>",
            correct_answers=["A"],
            question_type="mcq",
        ),
    )

    markdown = render_question_markdown(record)

    assert "Original URL:" in markdown
    assert "$$\\frac{1}{2}$$" in markdown
    assert "$x$" in markdown
    assert "<input type=\"radio\" disabled>" in markdown
    assert "## Metadata" in markdown
    assert "| External ID | 11111111-2222-3333-4444-555555555555 |" in markdown
