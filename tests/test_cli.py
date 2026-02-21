from __future__ import annotations

import json
from pathlib import Path

from college_board_scraper import cli


def test_cli_uses_run_summary_root_dir_for_output_root(monkeypatch, capsys, tmp_path: Path) -> None:
    expected_root = str((tmp_path / "sat_eqb").resolve())

    class FakeScraper:
        def __init__(self, **_kwargs: object) -> None:
            self.last_errors = []
            self.last_run_summary = {}

        def scrape(self, **_kwargs: object) -> list[object]:
            self.last_run_summary = {
                "root_dir": expected_root,
                "todo": {},
            }
            return []

    monkeypatch.setattr(cli, "Scraper", FakeScraper)

    exit_code = cli.main(
        [
            "--assessment",
            "SAT",
            "--test",
            "Math",
            "--option",
            "Algebra",
            "--output-dir",
            str(tmp_path),
        ]
    )
    assert exit_code == 0

    stdout = capsys.readouterr().out
    payload = json.loads(stdout)
    assert payload["output_root"] == expected_root


def test_cli_rejects_conflicting_only_new_and_only_failed(monkeypatch, tmp_path: Path) -> None:
    class FakeScraper:
        def __init__(self, **_kwargs: object) -> None:
            self.last_errors = []
            self.last_run_summary = {}

        def scrape(self, **_kwargs: object) -> list[object]:
            return []

    monkeypatch.setattr(cli, "Scraper", FakeScraper)

    try:
        cli.main(
            [
                "--assessment",
                "SAT",
                "--test",
                "Math",
                "--option",
                "Algebra",
                "--output-dir",
                str(tmp_path),
                "--only-new",
                "--only-failed",
            ]
        )
    except ValueError as exc:
        assert "--only-new and --only-failed cannot both be set" in str(exc)
        return
    raise AssertionError("Expected ValueError for conflicting --only-new and --only-failed flags")
