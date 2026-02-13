from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class AnswerOption:
    letter: str
    content_html: str


@dataclass
class DownloadedAsset:
    original_url: str
    local_path: str
    source_type: str
    mime_type: Optional[str] = None
    size_bytes: Optional[int] = None
    sha256: Optional[str] = None


@dataclass
class QuestionContent:
    prompt_html: str = ""
    stem_html: str = ""
    answer_options: List[AnswerOption] = field(default_factory=list)
    rationale_html: str = ""
    correct_answers: List[str] = field(default_factory=list)
    question_type: Optional[str] = None


@dataclass
class QuestionMetadata:
    question_id: str
    assessment: str
    assessment_id: int
    test: str
    test_id: int
    domain: str
    domain_code: str
    skill: str
    skill_code: str
    difficulty: str
    score_band_range: Optional[int]
    external_id: Optional[str]
    ibn: Optional[str]
    program: Optional[str]
    create_date: Optional[int]
    update_date: Optional[int]
    state_standards: List[str] = field(default_factory=list)


@dataclass
class QuestionRecord:
    metadata: QuestionMetadata
    source: str
    content: QuestionContent
    assets: List[DownloadedAsset] = field(default_factory=list)
    parse_warnings: List[str] = field(default_factory=list)
    raw_table_row: Dict[str, Any] = field(default_factory=dict)
    raw_detail_payload: Dict[str, Any] = field(default_factory=dict)
    raw_payload: Dict[str, Any] = field(default_factory=dict)
    lifecycle: Dict[str, Optional[str]] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        # Backwards-compatible alias: keep `raw_payload` synchronized with detail payload.
        payload["raw_payload"] = payload["raw_detail_payload"]
        return payload
