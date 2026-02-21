from __future__ import annotations

import re
from html import unescape
from typing import Iterable, Sequence

from bs4 import BeautifulSoup, NavigableString, Tag

from .models import QuestionRecord


def render_question_markdown(record: QuestionRecord) -> str:
    converter = _HtmlToMarkdownConverter()
    metadata = record.metadata
    content = record.content

    question_parts: list[str] = []
    for fragment in (content.prompt_html, content.stem_html):
        rendered = converter.convert(fragment)
        if rendered:
            question_parts.append(rendered)

    rationale_markdown = converter.convert(content.rationale_html)

    lines: list[str] = []
    lines.append(f"Original URL: {metadata.original_url or ''}")
    lines.append("")
    lines.append(f"# Question {metadata.question_id}")
    lines.append("")
    lines.append("## Question")
    lines.append("")
    if question_parts:
        lines.append("\n\n".join(question_parts).strip())
    else:
        lines.append("_No question body captured._")
    lines.append("")

    if content.answer_options:
        lines.append("## Answer Choices")
        lines.append("")
        for option in content.answer_options:
            option_markdown = converter.convert(option.content_html, inline=True).strip()
            if not option_markdown:
                option_markdown = "_(empty option)_"
            lines.append(f"- <input type=\"radio\" disabled> **{option.letter}.** {option_markdown}")
        lines.append("")
    else:
        lines.append("## Student Response")
        lines.append("")
        lines.append("<input type=\"text\" disabled placeholder=\"Enter your response\" />")
        lines.append("")

    lines.append("## Correct Answer")
    lines.append("")
    if content.correct_answers:
        lines.append(", ".join(str(answer).strip() for answer in content.correct_answers if str(answer).strip()))
    else:
        lines.append("_No explicit correct-answer value provided by source payload._")
    lines.append("")

    lines.append("## Rationale")
    lines.append("")
    lines.append(rationale_markdown if rationale_markdown else "_No rationale captured._")
    lines.append("")

    lines.append("## Metadata")
    lines.append("")
    lines.append("| Field | Value |")
    lines.append("| --- | --- |")
    metadata_rows = [
        ("Source", record.source),
        ("Assessment", metadata.assessment),
        ("Assessment ID", metadata.assessment_id),
        ("Test", metadata.test),
        ("Test ID", metadata.test_id),
        ("Domain", metadata.domain),
        ("Domain Code", metadata.domain_code),
        ("Skill", metadata.skill),
        ("Skill Code", metadata.skill_code),
        ("Difficulty", metadata.difficulty),
        ("Score Band Range", metadata.score_band_range),
        ("Question Type", content.question_type),
        ("Program", metadata.program),
        ("External ID", metadata.external_id),
        ("IBN", metadata.ibn),
        ("Create Date (epoch ms)", metadata.create_date),
        ("Update Date (epoch ms)", metadata.update_date),
        ("Asset Count", len(record.assets)),
        ("Parse Warning Count", len(record.parse_warnings)),
    ]
    for key, value in metadata_rows:
        lines.append(f"| {key} | {_escape_table_cell(value)} |")

    lines.append("")
    if metadata.state_standards:
        lines.append("State Standards:")
        lines.append("")
        for standard in metadata.state_standards:
            lines.append(f"- `{standard}`")
        lines.append("")

    if record.parse_warnings:
        lines.append("Parse Warnings:")
        lines.append("")
        for warning in record.parse_warnings:
            lines.append(f"- {warning}")
        lines.append("")

    return "\n".join(lines).strip() + "\n"


def _escape_table_cell(value: object) -> str:
    if value is None:
        return ""
    text = str(value)
    return text.replace("|", r"\|").replace("\n", " ")


class _HtmlToMarkdownConverter:
    _INLINE_WRAP_TAGS = {"span", "small", "label", "font"}
    _BLOCK_TAGS = {"p", "div", "section", "article", "blockquote", "header", "footer"}

    _MATH_OPERATOR_MAP = {
        "−": "-",
        "–": "-",
        "—": "-",
        "×": r" \times ",
        "·": r" \cdot ",
        "÷": r" \div ",
        "±": r" \pm ",
        "≤": r" \le ",
        "≥": r" \ge ",
        "≠": r" \ne ",
        "≈": r" \approx ",
        "∞": r"\infty",
        "→": r" \to ",
        "⇒": r" \Rightarrow ",
        "∑": r"\sum",
        "∏": r"\prod",
        "∫": r"\int",
        "∈": r" \in ",
        "∉": r" \notin ",
        "∪": r" \cup ",
        "∩": r" \cap ",
        "∠": r"\angle ",
        "π": r"\pi",
        "θ": r"\theta",
        "°": r"^\circ",
    }

    def convert(self, html_fragment: str, *, inline: bool = False) -> str:
        if not html_fragment or not str(html_fragment).strip():
            return ""

        soup = BeautifulSoup(html_fragment, "html.parser")
        self._replace_math_tags(soup)

        rendered_parts: list[str] = []
        for child in soup.contents:
            rendered = self._render_node(child, inline=inline)
            if rendered:
                rendered_parts.append(rendered)

        text = "".join(rendered_parts)
        text = self._clean_markdown(text, inline=inline)
        return text

    def _render_node(self, node: NavigableString | Tag, *, inline: bool) -> str:
        if isinstance(node, NavigableString):
            return self._normalize_text(str(node))

        if not isinstance(node, Tag):
            return ""

        name = node.name.lower()
        if name in {"script", "style", "noscript"}:
            return ""

        if name == "br":
            return "\n"
        if name == "hr":
            return "\n---\n\n"
        if name in self._INLINE_WRAP_TAGS:
            return self._render_children(node, inline=True)
        if name in {"strong", "b"}:
            return f"**{self._render_children(node, inline=True).strip()}**"
        if name in {"em", "i"}:
            return f"*{self._render_children(node, inline=True).strip()}*"
        if name == "code":
            return f"`{self._render_children(node, inline=True).strip()}`"
        if name == "pre":
            content = node.get_text("\n", strip=False).rstrip()
            return f"\n```\n{content}\n```\n\n"
        if name == "a":
            text = self._render_children(node, inline=True).strip() or "link"
            href = (node.get("href") or "").strip()
            if not href:
                return text
            return f"[{text}]({href})"
        if name == "img":
            src = (node.get("src") or "").strip()
            alt = (node.get("alt") or "").strip()
            if not src:
                return alt
            return f"![{alt}]({src})"
        if name == "sup":
            value = self._render_children(node, inline=True).strip()
            return f"^{{{value}}}" if value else ""
        if name == "sub":
            value = self._render_children(node, inline=True).strip()
            return f"_{{{value}}}" if value else ""
        if name == "math":
            # `math` tags are replaced during preprocessing.
            return self._render_children(node, inline=True)
        if name == "table":
            return self._render_table(node)
        if name == "ul":
            return self._render_list(node, ordered=False)
        if name == "ol":
            return self._render_list(node, ordered=True)
        if name == "li":
            return self._render_children(node, inline=True)
        if name in {"thead", "tbody", "tfoot", "tr", "td", "th"}:
            return self._render_children(node, inline=True)
        if name == "input":
            input_type = (node.get("type") or "text").strip()
            placeholder = (node.get("placeholder") or "").strip()
            placeholder_part = f' placeholder="{placeholder}"' if placeholder else ""
            return f"<input type=\"{input_type}\" disabled{placeholder_part} />"
        if name in self._BLOCK_TAGS:
            block = self._render_children(node, inline=False).strip()
            if not block:
                return ""
            return f"{block}\n\n"
        if name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            level = int(name[1])
            prefix = "#" * max(1, min(level, 6))
            text = self._render_children(node, inline=True).strip()
            if not text:
                return ""
            return f"{prefix} {text}\n\n"

        return self._render_children(node, inline=inline)

    def _render_children(self, node: Tag, *, inline: bool) -> str:
        return "".join(self._render_node(child, inline=inline) for child in node.contents)

    def _render_list(self, node: Tag, *, ordered: bool) -> str:
        items = [child for child in node.children if isinstance(child, Tag) and child.name.lower() == "li"]
        if not items:
            return ""

        lines: list[str] = []
        for index, item in enumerate(items, start=1):
            text = self._clean_markdown(self._render_children(item, inline=True), inline=True).strip()
            marker = f"{index}. " if ordered else "- "
            lines.append(f"{marker}{text or '_(empty item)_'}")
        return "\n".join(lines) + "\n\n"

    def _render_table(self, node: Tag) -> str:
        rows: list[list[str]] = []
        for tr in node.find_all("tr"):
            cells = tr.find_all(["th", "td"])
            if not cells:
                continue
            row: list[str] = []
            for cell in cells:
                text = self._clean_markdown(self._render_children(cell, inline=True), inline=True).strip()
                row.append(text)
            rows.append(row)

        if not rows:
            return ""

        width = max(len(row) for row in rows)
        normalized_rows = [row + [""] * (width - len(row)) for row in rows]
        header = normalized_rows[0]
        body = normalized_rows[1:] if len(normalized_rows) > 1 else []

        lines = [
            "| " + " | ".join(self._escape_cell(cell) for cell in header) + " |",
            "| " + " | ".join("---" for _ in header) + " |",
        ]
        for row in body:
            lines.append("| " + " | ".join(self._escape_cell(cell) for cell in row) + " |")
        return "\n".join(lines) + "\n\n"

    def _escape_cell(self, text: str) -> str:
        return text.replace("|", r"\|")

    def _replace_math_tags(self, soup: BeautifulSoup) -> None:
        for math_tag in list(soup.find_all("math")):
            latex = self._mathml_to_latex(math_tag).strip()
            if not latex:
                fallback_text = (math_tag.get("alttext") or math_tag.get_text(" ", strip=True) or "").strip()
                latex = self._fallback_text_to_latex(fallback_text)
            if not latex:
                continue

            display_mode = self._is_display_math(math_tag)
            wrapped = f"$${latex}$$" if display_mode else f"${latex}$"
            math_tag.replace_with(NavigableString(wrapped))

    def _is_display_math(self, math_tag: Tag) -> bool:
        display_attr = (math_tag.get("display") or "").strip().lower()
        if display_attr == "block":
            return True

        parent = math_tag.parent
        if not isinstance(parent, Tag):
            return False
        if parent.name.lower() not in {"p", "div"}:
            return False

        significant_children = [
            child
            for child in parent.contents
            if not (isinstance(child, NavigableString) and not str(child).strip())
        ]
        return len(significant_children) == 1 and significant_children[0] is math_tag

    def _mathml_to_latex(self, node: NavigableString | Tag) -> str:
        if isinstance(node, NavigableString):
            return self._normalize_math_text(str(node))
        if not isinstance(node, Tag):
            return ""

        name = node.name.lower()

        if name in {"math", "mrow"}:
            return "".join(self._mathml_to_latex(child) for child in node.children)
        if name in {"mi", "mn"}:
            return self._normalize_math_text(node.get_text("", strip=True))
        if name == "mo":
            token = self._normalize_math_text(node.get_text("", strip=True))
            return self._MATH_OPERATOR_MAP.get(token, token)
        if name == "mtext":
            text = self._normalize_math_text(node.get_text(" ", strip=True))
            return rf"\text{{{text}}}" if text else ""
        if name == "mfrac":
            children = self._math_children(node)
            if len(children) == 2:
                numerator = self._mathml_to_latex(children[0]) or "?"
                denominator = self._mathml_to_latex(children[1]) or "?"
                return rf"\frac{{{numerator}}}{{{denominator}}}"
        if name == "msup":
            children = self._math_children(node)
            if len(children) == 2:
                base = self._wrap_latex_atom(self._mathml_to_latex(children[0]) or "?")
                exponent = self._mathml_to_latex(children[1]) or "?"
                return rf"{base}^{{{exponent}}}"
        if name == "msub":
            children = self._math_children(node)
            if len(children) == 2:
                base = self._wrap_latex_atom(self._mathml_to_latex(children[0]) or "?")
                subscript = self._mathml_to_latex(children[1]) or "?"
                return rf"{base}_{{{subscript}}}"
        if name == "msubsup":
            children = self._math_children(node)
            if len(children) == 3:
                base = self._wrap_latex_atom(self._mathml_to_latex(children[0]) or "?")
                subscript = self._mathml_to_latex(children[1]) or "?"
                superscript = self._mathml_to_latex(children[2]) or "?"
                return rf"{base}_{{{subscript}}}^{{{superscript}}}"
        if name == "msqrt":
            body = "".join(self._mathml_to_latex(child) for child in node.children) or "?"
            return rf"\sqrt{{{body}}}"
        if name == "mroot":
            children = self._math_children(node)
            if len(children) == 2:
                body = self._mathml_to_latex(children[0]) or "?"
                index = self._mathml_to_latex(children[1]) or "?"
                return rf"\sqrt[{index}]{{{body}}}"
        if name == "mfenced":
            open_char = node.get("open", "(") or "("
            close_char = node.get("close", ")") or ")"
            separators = node.get("separators", ",") or ","
            children = self._math_children(node)
            parts = [self._mathml_to_latex(child) for child in children]
            separator = separators[0] if separators else ","
            body = separator.join(parts)
            return rf"\left{open_char}{body}\right{close_char}"
        if name in {"mover", "munder", "munderover"}:
            children = self._math_children(node)
            if len(children) >= 2:
                base = self._wrap_latex_atom(self._mathml_to_latex(children[0]) or "?")
                under = self._mathml_to_latex(children[1]) or "?"
                if name == "mover":
                    return rf"{base}^{{{under}}}"
                if name == "munder":
                    return rf"{base}_{{{under}}}"
                over = self._mathml_to_latex(children[2]) if len(children) > 2 else "?"
                return rf"{base}_{{{under}}}^{{{over}}}"
        if name == "mtable":
            rows: list[str] = []
            for row_tag in node.find_all("mtr", recursive=False):
                cells = [self._mathml_to_latex(cell) for cell in row_tag.find_all("mtd", recursive=False)]
                rows.append(" & ".join(cells))
            if rows:
                return r"\begin{matrix}" + r" \\ ".join(rows) + r"\end{matrix}"
        if name == "semantics":
            for child in node.children:
                if isinstance(child, Tag) and child.name.lower() != "annotation":
                    return self._mathml_to_latex(child)
            return "".join(self._mathml_to_latex(child) for child in node.children)
        if name in {"annotation", "annotation-xml"}:
            return self._normalize_math_text(node.get_text(" ", strip=True))

        return "".join(self._mathml_to_latex(child) for child in node.children)

    def _math_children(self, node: Tag) -> list[Tag]:
        return [child for child in node.children if isinstance(child, Tag)]

    def _wrap_latex_atom(self, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            return "{}"
        if re.fullmatch(r"[A-Za-z0-9]|\\[A-Za-z]+", trimmed):
            return trimmed
        if trimmed.startswith("{") and trimmed.endswith("}"):
            return trimmed
        return f"{{{trimmed}}}"

    def _fallback_text_to_latex(self, text: str) -> str:
        normalized = self._normalize_math_text(text)
        if not normalized:
            return ""
        normalized = normalized.replace("StartFraction", r"\frac").replace("EndFraction", "")
        normalized = re.sub(r"\s+", " ", normalized).strip()
        return normalized

    def _normalize_math_text(self, text: str) -> str:
        text = unescape(text or "")
        text = text.replace("\xa0", " ")
        return text.strip()

    def _normalize_text(self, text: str) -> str:
        text = unescape(text)
        text = text.replace("\xa0", " ")
        text = re.sub(r"\bStartFragment\b", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\bEndFragment\b", "", text, flags=re.IGNORECASE)
        return text

    def _clean_markdown(self, text: str, *, inline: bool) -> str:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        if inline:
            text = re.sub(r"\s+", " ", text)
        else:
            lines = [line.rstrip() for line in text.split("\n")]
            text = "\n".join(lines)
        return text.strip()
