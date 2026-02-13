from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Dict, List, Set

from .core import Scraper
from .helpers import ScraperAmount


def _parse_skill_args(entries: List[str]) -> Dict[str, Set[str]]:
    parsed: Dict[str, Set[str]] = {}
    for entry in entries:
        if ":" not in entry:
            raise ValueError(
                f"Invalid --skill value '{entry}'. Expected format: 'Domain:skill A,skill B'"
            )
        domain, raw_skills = entry.split(":", 1)
        skills = {item.strip() for item in raw_skills.split(",") if item.strip()}
        if not skills:
            raise ValueError(f"No skills provided for domain '{domain}'.")
        parsed[domain.strip()] = skills
    return parsed


def build_parser() -> argparse.ArgumentParser:
    default_out_dir = os.getenv("OUT_DIR", ".")

    parser = argparse.ArgumentParser(
        description=(
            "Download SAT Suite Question Bank content and assets. "
            "All run data/history are persisted under $OUT_DIR/sat_eqb."
        )
    )
    parser.add_argument("--assessment", required=True, help="Assessment name, e.g. 'SAT'")
    parser.add_argument("--test", required=True, help="Test name, e.g. 'Math' or 'Reading and Writing'")
    parser.add_argument(
        "--option",
        action="append",
        required=True,
        help="Domain option to include. Repeat per option (e.g. --option 'Algebra').",
    )
    parser.add_argument(
        "--difficulty",
        action="append",
        default=[],
        help="Difficulty filter. Repeat for Easy/Medium/Hard.",
    )
    parser.add_argument(
        "--skill",
        action="append",
        default=[],
        help="Skill filter in the format 'Domain:skill A,skill B'. Repeat per domain.",
    )
    parser.add_argument("--state", default=None, help="Optional state code/name for state standards metadata.")
    parser.add_argument(
        "--amount",
        default="all",
        help="Number of questions to fetch, or 'all'.",
    )
    parser.add_argument(
        "--exclude-active-questions",
        action="store_true",
        help="Exclude items currently active in released digital SAT/PSAT forms.",
    )
    parser.add_argument(
        "--output-dir",
        default=default_out_dir,
        help=(
            "Base OUT_DIR for output. The scraper writes data/history to $OUT_DIR/sat_eqb "
            f"(default: {default_out_dir})."
        ),
    )
    parser.add_argument(
        "--download-new",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Download question content that has never been downloaded successfully for this profile.",
    )
    parser.add_argument(
        "--download-failed",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Retry question content that failed in previous runs for this profile.",
    )
    parser.add_argument(
        "--only-new",
        action="store_true",
        help="Convenience flag: equivalent to --download-new --no-download-failed.",
    )
    parser.add_argument(
        "--only-failed",
        action="store_true",
        help="Convenience flag: equivalent to --no-download-new --download-failed.",
    )
    parser.add_argument(
        "--restart",
        action="store_true",
        help="Clear saved progress for this filter profile and reprocess from the beginning.",
    )
    parser.add_argument(
        "--max-requests-per-second",
        type=float,
        default=3.0,
        help="Proactive request pacing to respect endpoint rate limits (default: 3.0).",
    )
    parser.add_argument(
        "--run-label",
        default=None,
        help="Optional short label included in the run ID and history records.",
    )
    parser.add_argument(
        "--include-state-standards",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Whether to fetch state standards metadata when --state is provided.",
    )
    parser.add_argument(
        "--download-assets",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Whether to download and relink assets referenced by question HTML.",
    )
    parser.add_argument(
        "--save-output",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Whether to write per-question JSON/HTML output files.",
    )
    parser.add_argument(
        "--continue-on-error",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Continue when a question fails, and log the failure for retry.",
    )
    return parser


def main(argv: List[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.only_new and args.only_failed:
        raise ValueError("--only-new and --only-failed cannot both be set")

    download_new = args.download_new
    download_failed = args.download_failed

    if args.only_new:
        download_new = True
        download_failed = False
    elif args.only_failed:
        download_new = False
        download_failed = True

    skills = _parse_skill_args(args.skill) if args.skill else None

    amount: int | ScraperAmount
    if str(args.amount).strip().lower() == "all":
        amount = ScraperAmount.ALL
    else:
        amount = int(args.amount)

    scraper = Scraper(
        assessment=args.assessment,
        test=args.test,
        options={item.strip() for item in args.option if item.strip()},
        difficulties={item.strip() for item in args.difficulty if item.strip()} or None,
        skills=skills,
        exclude_active_questions=args.exclude_active_questions,
        state=args.state,
        output_dir=Path(args.output_dir),
        max_requests_per_second=args.max_requests_per_second,
    )

    records = scraper.scrape(
        amount=amount,
        output_dir=Path(args.output_dir),
        save_output=args.save_output,
        download_assets=args.download_assets,
        continue_on_error=args.continue_on_error,
        include_state_standards=args.include_state_standards,
        restart=args.restart,
        download_new=download_new,
        download_failed=download_failed,
        run_label=args.run_label,
    )

    run_summary = scraper.last_run_summary
    summary = {
        "downloaded": len(records),
        "errors": scraper.last_errors,
        "run_summary": run_summary,
        "todo": run_summary.get("todo", {}),
        "output_root": str((Path(args.output_dir) / "sat_eqb").resolve()),
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
