#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence, Tuple
from xml.sax.saxutils import escape as xml_escape


@dataclass
class StandardStats:
    total: int = 0
    asset_questions: int = 0
    total_assets: int = 0
    difficulty: Counter[str] = field(default_factory=Counter)
    question_type: Counter[str] = field(default_factory=Counter)
    test: Counter[str] = field(default_factory=Counter)
    domain: Counter[str] = field(default_factory=Counter)
    score_band_sum: float = 0.0
    score_band_count: int = 0

    def add(
        self,
        *,
        difficulty: str,
        question_type: str,
        test: str,
        domain: str,
        score_band_range: int | None,
        asset_count: int,
    ) -> None:
        self.total += 1
        self.difficulty[difficulty] += 1
        self.question_type[question_type] += 1
        self.test[test] += 1
        self.domain[domain] += 1
        self.total_assets += asset_count
        if asset_count > 0:
            self.asset_questions += 1
        if score_band_range is not None:
            self.score_band_sum += float(score_band_range)
            self.score_band_count += 1

    @property
    def asset_rate(self) -> float:
        if self.total == 0:
            return 0.0
        return self.asset_questions / self.total

    @property
    def avg_assets(self) -> float:
        if self.total == 0:
            return 0.0
        return self.total_assets / self.total

    @property
    def avg_score_band(self) -> float | None:
        if self.score_band_count == 0:
            return None
        return self.score_band_sum / self.score_band_count


def _load_records(data_path: Path) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
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
                raise RuntimeError(f"Expected JSON object in {data_path} line {line_number}")
            records.append(payload)
    return records


def _clean_text(value: Any, *, default: str = "Unknown") -> str:
    text = str(value or "").strip()
    return text if text else default


def _normalize_question_type(value: Any) -> str:
    raw = _clean_text(value)
    lowered = raw.lower()
    if lowered in {"mcq", "multiple choice"}:
        return "Multiple Choice"
    if lowered in {"spr", "student produced response", "student-produced response"}:
        return "Student-Produced Response"
    return raw


def _normalize_difficulty(value: Any) -> str:
    lowered = _clean_text(value).lower()
    if lowered == "easy":
        return "Easy"
    if lowered == "medium":
        return "Medium"
    if lowered == "hard":
        return "Hard"
    return "Unknown"


def _iter_unique_state_standards(record: Dict[str, Any]) -> Iterable[str]:
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    seen: set[str] = set()
    for value in metadata.get("state_standards") or []:
        standard = _clean_text(value, default="")
        if not standard or standard in seen:
            continue
        seen.add(standard)
        yield standard


def _question_key(record: Dict[str, Any]) -> str:
    explicit = _clean_text(record.get("question_key"), default="")
    if explicit:
        return explicit
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    external_id = _clean_text(metadata.get("external_id"), default="")
    if external_id:
        return f"external:{external_id}"
    ibn = _clean_text(metadata.get("ibn"), default="")
    if ibn:
        return f"ibn:{ibn}"
    question_id = _clean_text(metadata.get("question_id"), default="")
    if question_id:
        return f"question:{question_id}"
    return "unknown"


def _analysis(records: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    standard_stats: Dict[str, StandardStats] = defaultdict(StandardStats)
    standards_per_question: Counter[int] = Counter()

    difficulty_by_question_type: Dict[str, Counter[str]] = defaultdict(Counter)
    by_question_type: Counter[str] = Counter()
    by_difficulty: Counter[str] = Counter()
    by_test: Counter[str] = Counter()
    by_domain: Counter[str] = Counter()
    asset_count_distribution: Counter[int] = Counter()
    score_band_distribution: Counter[int] = Counter()
    question_asset_total = 0
    questions_with_assets = 0
    weighted_links = 0

    for record in records:
        metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
        content = record.get("content") if isinstance(record.get("content"), dict) else {}

        difficulty = _normalize_difficulty(metadata.get("difficulty"))
        question_type = _normalize_question_type(content.get("question_type"))
        test = _clean_text(metadata.get("test"))
        domain = _clean_text(metadata.get("domain"))

        score_band_raw = metadata.get("score_band_range")
        score_band_range: int | None
        if isinstance(score_band_raw, int):
            score_band_range = score_band_raw
        else:
            try:
                score_band_range = int(score_band_raw) if score_band_raw is not None else None
            except (TypeError, ValueError):
                score_band_range = None

        asset_count = len(record.get("assets") or [])
        question_asset_total += asset_count
        if asset_count > 0:
            questions_with_assets += 1
        asset_count_distribution[asset_count] += 1

        by_question_type[question_type] += 1
        by_difficulty[difficulty] += 1
        by_test[test] += 1
        by_domain[domain] += 1
        difficulty_by_question_type[question_type][difficulty] += 1
        if score_band_range is not None:
            score_band_distribution[score_band_range] += 1

        standards = list(_iter_unique_state_standards(record))
        standards_per_question[len(standards)] += 1
        for standard in standards:
            standard_stats[standard].add(
                difficulty=difficulty,
                question_type=question_type,
                test=test,
                domain=domain,
                score_band_range=score_band_range,
                asset_count=asset_count,
            )
            weighted_links += 1

    standard_counts = Counter({standard: stats.total for standard, stats in standard_stats.items()})
    top_standards = standard_counts.most_common(20)
    low_standards = sorted(
        ((standard, count) for standard, count in standard_counts.items() if count <= 50),
        key=lambda item: (item[1], item[0]),
    )

    hardest_standards: List[Tuple[str, float, int]] = []
    easiest_standards: List[Tuple[str, float, int]] = []
    for standard, stats in standard_stats.items():
        if stats.total < 40:
            continue
        hard_share = stats.difficulty.get("Hard", 0) / stats.total
        easy_share = stats.difficulty.get("Easy", 0) / stats.total
        difficulty_bias = hard_share - easy_share
        hardest_standards.append((standard, difficulty_bias, stats.total))
        easiest_standards.append((standard, difficulty_bias, stats.total))
    hardest_standards.sort(key=lambda item: item[1], reverse=True)
    easiest_standards.sort(key=lambda item: item[1])

    asset_heavy_standards = sorted(
        (
            (standard, stats.asset_rate, stats.total)
            for standard, stats in standard_stats.items()
            if stats.total >= 20
        ),
        key=lambda item: item[1],
        reverse=True,
    )

    high_score_band_standards = sorted(
        (
            (standard, stats.avg_score_band, stats.total)
            for standard, stats in standard_stats.items()
            if stats.total >= 25 and stats.avg_score_band is not None
        ),
        key=lambda item: float(item[1]),
        reverse=True,
    )

    return {
        "total_questions": len(records),
        "unique_standards": len(standard_stats),
        "weighted_links": weighted_links,
        "questions_with_assets": questions_with_assets,
        "question_asset_total": question_asset_total,
        "avg_standards_per_question": (
            sum(count * bucket for bucket, count in standards_per_question.items()) / max(1, len(records))
        ),
        "standards_per_question": dict(sorted(standards_per_question.items())),
        "by_question_type": dict(by_question_type),
        "by_difficulty": dict(by_difficulty),
        "by_test": dict(by_test),
        "by_domain": dict(by_domain),
        "asset_count_distribution": dict(sorted(asset_count_distribution.items())),
        "score_band_distribution": dict(sorted(score_band_distribution.items())),
        "standard_stats": standard_stats,
        "standard_counts": standard_counts,
        "top_standards": top_standards,
        "low_standards": low_standards,
        "hardest_standards": hardest_standards[:12],
        "easiest_standards": easiest_standards[:12],
        "asset_heavy_standards": asset_heavy_standards[:12],
        "high_score_band_standards": high_score_band_standards[:12],
        "difficulty_by_question_type": difficulty_by_question_type,
    }


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _format_markdown_table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    lines: List[str] = []
    lines.append("| " + " | ".join(headers) + " |")
    lines.append("| " + " | ".join("---" for _ in headers) + " |")
    for row in rows:
        cells = [str(cell).replace("|", r"\|") for cell in row]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def _write_horizontal_bar_svg(
    path: Path,
    *,
    title: str,
    items: Sequence[Tuple[str, float]],
    value_formatter,
    bar_color: str = "#3b82f6",
    width: int = 1280,
) -> None:
    row_height = 28
    chart_height = max(1, len(items)) * row_height
    margin_left = 380
    margin_right = 140
    margin_top = 72
    margin_bottom = 36
    inner_width = width - margin_left - margin_right
    height = margin_top + chart_height + margin_bottom

    max_value = max((value for _, value in items), default=1.0)
    if max_value <= 0:
        max_value = 1.0

    lines: List[str] = []
    lines.append(f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}">')
    lines.append('<rect width="100%" height="100%" fill="#ffffff" />')
    lines.append(
        f'<text x="{margin_left}" y="40" font-family="Arial, sans-serif" font-size="24" font-weight="700">{xml_escape(title)}</text>'
    )

    for index, (label, value) in enumerate(items):
        y = margin_top + index * row_height
        bar_width = 0 if max_value == 0 else (value / max_value) * inner_width
        lines.append(
            f'<text x="{margin_left - 10}" y="{y + 18}" text-anchor="end" font-family="Arial, sans-serif" font-size="13">{xml_escape(label)}</text>'
        )
        lines.append(
            f'<rect x="{margin_left}" y="{y + 4}" width="{bar_width:.2f}" height="18" fill="{bar_color}" opacity="0.88" />'
        )
        lines.append(
            f'<text x="{margin_left + bar_width + 8:.2f}" y="{y + 18}" font-family="Arial, sans-serif" font-size="12">{xml_escape(value_formatter(value))}</text>'
        )

    lines.append("</svg>")
    _write_text(path, "\n".join(lines) + "\n")


def _write_stacked_percent_svg(
    path: Path,
    *,
    title: str,
    items: Sequence[Tuple[str, Dict[str, int]]],
    segments: Sequence[str],
    colors: Dict[str, str],
    width: int = 1280,
) -> None:
    row_height = 32
    chart_height = max(1, len(items)) * row_height
    margin_left = 390
    margin_right = 140
    margin_top = 104
    margin_bottom = 36
    inner_width = width - margin_left - margin_right
    height = margin_top + chart_height + margin_bottom

    lines: List[str] = []
    lines.append(f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}">')
    lines.append('<rect width="100%" height="100%" fill="#ffffff" />')
    lines.append(
        f'<text x="{margin_left}" y="40" font-family="Arial, sans-serif" font-size="24" font-weight="700">{xml_escape(title)}</text>'
    )

    # legend
    legend_x = margin_left
    legend_y = 64
    for segment in segments:
        color = colors.get(segment, "#9ca3af")
        lines.append(
            f'<rect x="{legend_x}" y="{legend_y}" width="14" height="14" fill="{color}" opacity="0.9" />'
        )
        lines.append(
            f'<text x="{legend_x + 20}" y="{legend_y + 12}" font-family="Arial, sans-serif" font-size="12">{xml_escape(segment)}</text>'
        )
        legend_x += 140

    for index, (label, segment_counts) in enumerate(items):
        y = margin_top + index * row_height
        total = sum(segment_counts.get(segment, 0) for segment in segments)
        if total <= 0:
            total = 1
        lines.append(
            f'<text x="{margin_left - 10}" y="{y + 20}" text-anchor="end" font-family="Arial, sans-serif" font-size="13">{xml_escape(label)}</text>'
        )

        x_cursor = margin_left
        for segment in segments:
            count = segment_counts.get(segment, 0)
            width_segment = inner_width * (count / total)
            if width_segment <= 0:
                continue
            color = colors.get(segment, "#9ca3af")
            lines.append(
                f'<rect x="{x_cursor:.2f}" y="{y + 5}" width="{width_segment:.2f}" height="20" fill="{color}" opacity="0.88" />'
            )
            x_cursor += width_segment

        lines.append(
            f'<text x="{margin_left + inner_width + 8}" y="{y + 20}" font-family="Arial, sans-serif" font-size="12">{sum(segment_counts.values())}</text>'
        )

    lines.append("</svg>")
    _write_text(path, "\n".join(lines) + "\n")


def _render_report(
    *,
    analysis: Dict[str, Any],
    report_assets_dir: Path,
) -> str:
    total_questions = analysis["total_questions"]
    weighted_links = analysis["weighted_links"]
    unique_standards = analysis["unique_standards"]
    avg_standards_per_question = analysis["avg_standards_per_question"]
    questions_with_assets = analysis["questions_with_assets"]
    question_asset_total = analysis["question_asset_total"]

    top_standards_table = _format_markdown_table(
        ["Standard", "Questions", "Share of Questions"],
        [
            (standard, count, f"{(count / max(1, total_questions)) * 100:.1f}%")
            for standard, count in analysis["top_standards"][:20]
        ],
    )

    low_standards_table = _format_markdown_table(
        ["Standard", "Questions", "Coverage Label"],
        [
            (standard, count, "Low")
            for standard, count in analysis["low_standards"][:25]
        ],
    )

    hardest_table = _format_markdown_table(
        ["Standard", "Hard-Easy Bias", "Questions"],
        [
            (standard, f"{bias:+.2f}", count)
            for standard, bias, count in analysis["hardest_standards"]
        ],
    )

    easiest_table = _format_markdown_table(
        ["Standard", "Hard-Easy Bias", "Questions"],
        [
            (standard, f"{bias:+.2f}", count)
            for standard, bias, count in analysis["easiest_standards"]
        ],
    )

    asset_heavy_table = _format_markdown_table(
        ["Standard", "Asset Rate", "Avg Assets/Question", "Questions"],
        [
            (
                standard,
                f"{rate * 100:.1f}%",
                f"{analysis['standard_stats'][standard].avg_assets:.2f}",
                count,
            )
            for standard, rate, count in analysis["asset_heavy_standards"]
        ],
    )

    score_band_table = _format_markdown_table(
        ["Standard", "Avg Score Band", "Questions"],
        [
            (standard, f"{avg_score:.2f}", count)
            for standard, avg_score, count in analysis["high_score_band_standards"]
        ],
    )

    by_question_type_table = _format_markdown_table(
        ["Question Type", "Count", "Share"],
        [
            (
                qtype,
                count,
                f"{(count / max(1, total_questions)) * 100:.1f}%",
            )
            for qtype, count in sorted(
                analysis["by_question_type"].items(), key=lambda item: item[1], reverse=True
            )
        ],
    )

    difficulty_table = _format_markdown_table(
        ["Difficulty", "Count", "Share"],
        [
            (difficulty, count, f"{(count / max(1, total_questions)) * 100:.1f}%")
            for difficulty, count in sorted(
                analysis["by_difficulty"].items(), key=lambda item: item[1], reverse=True
            )
        ],
    )

    standards_per_question_table = _format_markdown_table(
        ["Standards per Question", "Questions", "Share"],
        [
            (
                standards_count,
                question_count,
                f"{(question_count / max(1, total_questions)) * 100:.1f}%",
            )
            for standards_count, question_count in analysis["standards_per_question"].items()
        ],
    )

    lines: List[str] = []
    lines.append("# State Standards Coverage Report")
    lines.append("")
    lines.append("This report analyzes the current downloaded SAT Question Bank dataset and highlights coverage balance, difficulty mix, and asset complexity by state standard.")
    lines.append("")
    lines.append("## Dataset Summary")
    lines.append("")
    lines.append(f"- Total questions analyzed: **{total_questions}**")
    lines.append(f"- Unique standards observed: **{unique_standards}**")
    lines.append(f"- Question-standard links (weighted): **{weighted_links}**")
    lines.append(f"- Average standards tagged per question: **{avg_standards_per_question:.2f}**")
    lines.append(
        f"- Questions with one or more assets: **{questions_with_assets}** ({(questions_with_assets / max(1, total_questions)) * 100:.1f}%)"
    )
    lines.append(f"- Total assets across all questions: **{question_asset_total}**")
    lines.append("")

    lines.append("## Coverage Imbalance")
    lines.append("")
    lines.append("### Top Standards by Question Count")
    lines.append("")
    lines.append(top_standards_table)
    lines.append("")
    lines.append(f"![Top standards by count]({report_assets_dir.as_posix()}/top_standards_counts.svg)")
    lines.append("")

    lines.append("### Low-Coverage Standards (<= 50 questions)")
    lines.append("")
    lines.append(low_standards_table if analysis["low_standards"] else "_No standards fell into the low-coverage threshold._")
    lines.append("")

    lines.append("## Difficulty Distribution by Standard")
    lines.append("")
    lines.append("### Hardest-Leaning Standards (Hard share minus Easy share)")
    lines.append("")
    lines.append(hardest_table if analysis["hardest_standards"] else "_Not enough data for this metric._")
    lines.append("")
    lines.append("### Easiest-Leaning Standards")
    lines.append("")
    lines.append(easiest_table if analysis["easiest_standards"] else "_Not enough data for this metric._")
    lines.append("")
    lines.append(
        f"![Difficulty mix for top standards]({report_assets_dir.as_posix()}/top_standards_difficulty_mix.svg)"
    )
    lines.append("")

    lines.append("## Difficulty and Answer-Type Interaction")
    lines.append("")
    lines.append(by_question_type_table)
    lines.append("")
    lines.append(difficulty_table)
    lines.append("")
    lines.append(
        f"![Difficulty distribution by question type]({report_assets_dir.as_posix()}/difficulty_by_question_type.svg)"
    )
    lines.append("")

    lines.append("## Asset Complexity by Standard")
    lines.append("")
    lines.append(asset_heavy_table if analysis["asset_heavy_standards"] else "_Not enough data for this metric._")
    lines.append("")
    lines.append(f"![Asset-heavy standards]({report_assets_dir.as_posix()}/asset_rate_by_standard.svg)")
    lines.append("")

    lines.append("## Score-Band Signal by Standard")
    lines.append("")
    lines.append(score_band_table if analysis["high_score_band_standards"] else "_Not enough data for this metric._")
    lines.append("")

    lines.append("## Tag Density")
    lines.append("")
    lines.append(standards_per_question_table)
    lines.append("")

    lines.append("## How This Helps Test Takers")
    lines.append("")
    lines.append("- Prioritize high-frequency standards first; they appear most often and are likely to yield the largest score impact.")
    lines.append("- Add a targeted practice block for low-coverage standards so rare skills do not become blind spots.")
    lines.append("- Use hardest-leaning standards as late-stage prep after foundational easy/medium coverage is stable.")
    lines.append("- If visual-heavy standards are a weakness, include timed drills with diagrams/charts to reduce interpretation overhead.")
    lines.append("- Track score-band-heavy standards to focus on questions that tend to cluster at higher challenge levels.")
    lines.append("")

    return "\n".join(lines).strip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Generate a standards-distribution report (Markdown + SVG charts) from sat_eqb/data.jsonl."
        )
    )
    parser.add_argument(
        "--root-dir",
        required=True,
        help="Path to sat_eqb output root (contains data.jsonl).",
    )
    parser.add_argument(
        "--report-path",
        default="state_standards.md",
        help="Output markdown report path (default: state_standards.md).",
    )
    parser.add_argument(
        "--assets-dir",
        default="report_assets",
        help="Directory for generated chart assets (default: report_assets).",
    )
    args = parser.parse_args()

    root_dir = Path(args.root_dir).resolve()
    data_path = root_dir / "data.jsonl"
    if not data_path.exists():
        raise RuntimeError(f"Missing dataset index: {data_path}")

    report_path = Path(args.report_path).resolve()
    assets_dir = Path(args.assets_dir).resolve()
    assets_dir.mkdir(parents=True, exist_ok=True)

    records = _load_records(data_path)
    analysis = _analysis(records)

    # Chart 1: top standards by count
    top_counts_items = analysis["top_standards"][:20]
    _write_horizontal_bar_svg(
        assets_dir / "top_standards_counts.svg",
        title="Top Standards by Question Count",
        items=[(standard, float(count)) for standard, count in top_counts_items],
        value_formatter=lambda value: f"{int(round(value))}",
        bar_color="#2563eb",
    )

    # Chart 2: stacked difficulty mix for top standards
    top_for_difficulty_mix = analysis["top_standards"][:12]
    _write_stacked_percent_svg(
        assets_dir / "top_standards_difficulty_mix.svg",
        title="Difficulty Mix for Top Standards",
        items=[
            (
                standard,
                dict(analysis["standard_stats"][standard].difficulty),
            )
            for standard, _count in top_for_difficulty_mix
        ],
        segments=("Easy", "Medium", "Hard"),
        colors={
            "Easy": "#16a34a",
            "Medium": "#d97706",
            "Hard": "#dc2626",
        },
    )

    # Chart 3: difficulty mix by question type
    _write_stacked_percent_svg(
        assets_dir / "difficulty_by_question_type.svg",
        title="Difficulty Distribution by Question Type",
        items=[
            (qtype, dict(counter))
            for qtype, counter in sorted(
                analysis["difficulty_by_question_type"].items(),
                key=lambda item: sum(item[1].values()),
                reverse=True,
            )
        ],
        segments=("Easy", "Medium", "Hard"),
        colors={
            "Easy": "#16a34a",
            "Medium": "#d97706",
            "Hard": "#dc2626",
        },
    )

    # Chart 4: asset-heavy standards
    asset_items = analysis["asset_heavy_standards"][:12]
    _write_horizontal_bar_svg(
        assets_dir / "asset_rate_by_standard.svg",
        title="Asset Rate by Standard (>=20 Questions)",
        items=[(standard, rate * 100.0) for standard, rate, _count in asset_items],
        value_formatter=lambda value: f"{value:.1f}%",
        bar_color="#0f766e",
    )

    report_text = _render_report(
        analysis=analysis,
        report_assets_dir=Path(args.assets_dir),
    )
    _write_text(report_path, report_text)

    print(
        json.dumps(
            {
                "root_dir": str(root_dir),
                "records_analyzed": len(records),
                "report_path": str(report_path),
                "assets_dir": str(assets_dir),
                "charts": [
                    str(assets_dir / "top_standards_counts.svg"),
                    str(assets_dir / "top_standards_difficulty_mix.svg"),
                    str(assets_dir / "difficulty_by_question_type.svg"),
                    str(assets_dir / "asset_rate_by_standard.svg"),
                ],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
