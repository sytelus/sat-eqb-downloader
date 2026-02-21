from __future__ import annotations

from urllib.parse import urlencode


WEBSITE_BASE_URL = "https://satsuiteeducatorquestionbank.collegeboard.org"


def build_original_question_url(
    *,
    source: str,
    assessment: str,
    test: str,
    question_id: str,
    external_id: str | None,
    ibn: str | None,
    domain: str | None,
    domain_code: str | None,
    skill_code: str | None,
    difficulty: str | None,
) -> str:
    """
    Build a deterministic official-site reference URL for a question.

    Notes:
    - College Board's current SPA does not expose a stable deep-link route that
      opens a specific question modal directly by ID.
    - This URL points to the official results route and includes identifying
      query parameters so users can reproduce context and locate the item.
    """

    normalized_source = (source or "").strip().lower()
    if normalized_source == "digital":
        route = "/digital/results"
    elif normalized_source == "legacy":
        route = "/results"
    else:
        route = "/digital/results"

    params: dict[str, str] = {}

    def _add(name: str, value: str | None) -> None:
        if value is None:
            return
        text = str(value).strip()
        if text:
            params[name] = text

    _add("assessment", assessment)
    _add("test", test)
    _add("question_id", question_id)
    _add("external_id", external_id)
    _add("ibn", ibn)
    _add("domain", domain)
    _add("domain_code", domain_code)
    _add("skill_code", skill_code)
    _add("difficulty", difficulty)

    query = urlencode(params)
    if not query:
        return f"{WEBSITE_BASE_URL}{route}"
    return f"{WEBSITE_BASE_URL}{route}?{query}"
