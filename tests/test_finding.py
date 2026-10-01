"""A finding record carries its reproducer and which conditions this host has.

The 2026-09-06 hunt found a rename that differs only in case deleting the file
it had just written. It needs a case-insensitive filesystem, which the Windows
host has, and a case-sensitive path comparison, which it does not, so the
reproducer modelled the second. `init-run --defect-report` takes the record and
the briefing keeps the distinction. Mailman #54.
"""
from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from mailman.artifacts import create_run
from mailman.cli import main
from mailman.finding import (
    FindingError,
    blank_finding,
    is_finding_file,
    load_finding,
    render_finding_markdown,
    validate_finding,
    write_finding,
)
from mailman.issue import capture_defect_report, load_issue_record

COMMIT = "a" * 40


def _finding() -> dict:
    record = blank_finding("openai/openai-agents-python", COMMIT)
    record.update(
        {
            "title": "A case-only rename deletes the moved file",
            "summary": (
                "WorkspaceEditor.apply_operation writes the moved file and then "
                "removes the source, guarded only by moved_destination != "
                "destination."
            ),
            "reproducer": {
                "command": ["python", "repro.py"],
                "script": "",
                "expected": "repro.py prints 'missing' because the file is gone",
            },
            "conditions": [
                {
                    "name": "case-insensitive filesystem",
                    "required": True,
                    "host_satisfies": True,
                    "how_checked": "wrote a.txt, opened A.TXT",
                },
                {
                    "name": "case-sensitive path comparison",
                    "required": True,
                    "host_satisfies": False,
                    "how_checked": "Path('a') != Path('A') is False on Windows",
                    "modelled_by": "a PurePosixPath comparison in repro.py",
                },
            ],
            "evidence": {"fuzz": "fuzz.json"},
        }
    )
    return record


def _run(path: Path, root: Path, commit: str = COMMIT):
    return create_run(
        repository="openai/openai-agents-python",
        defect_report=path,
        base_commit=commit,
        primary="claude",
        reviewer="claude",
        primary_model="claude-opus-5",
        reviewer_model="claude-opus-5",
        data_root=root / "runs",
    )


class ValidationTests(unittest.TestCase):
    def test_the_blank_template_names_every_missing_field(self) -> None:
        with self.assertRaises(FindingError) as raised:
            validate_finding(blank_finding())
        problems = " | ".join(raised.exception.problems)
        for expected in ("title", "summary", "repository", "reproducer needs",
                         "expected", "needs a name", "how the host was checked"):
            self.assertIn(expected, problems)

    def test_a_filled_record_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = write_finding(Path(directory) / "finding.json", _finding())
            self.assertTrue(is_finding_file(path))
            self.assertEqual(load_finding(path)["title"], _finding()["title"])

    def test_host_satisfies_takes_only_true_false_or_unknown(self) -> None:
        record = _finding()
        record["conditions"][0]["host_satisfies"] = "yes"
        with self.assertRaises(FindingError) as raised:
            validate_finding(record)
        self.assertIn("host_satisfies", str(raised.exception))
        record["conditions"][0]["host_satisfies"] = "unknown"
        validate_finding(record)

    def test_a_repeated_condition_name_is_refused(self) -> None:
        record = _finding()
        record["conditions"][1]["name"] = record["conditions"][0]["name"]
        with self.assertRaises(FindingError):
            validate_finding(record)

    def test_prose_and_other_json_are_not_finding_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            prose = Path(directory) / "defect.md"
            prose.write_text("A defect.", encoding="utf-8")
            other = Path(directory) / "other.json"
            other.write_text("{}", encoding="utf-8")
            self.assertFalse(is_finding_file(prose))
            self.assertFalse(is_finding_file(other))


class RenderingTests(unittest.TestCase):
    def test_the_briefing_says_which_condition_the_host_lacks(self) -> None:
        markdown = render_finding_markdown(_finding())
        self.assertIn(
            "| case-sensitive path comparison | yes | no | Path('a') != Path('A') is False on Windows "
            "| a PurePosixPath comparison in repro.py |",
            markdown,
        )
        self.assertIn("does not supply every required condition (case-sensitive path comparison)", markdown)
        self.assertIn("python repro.py", markdown)


class InitRunTests(unittest.TestCase):
    def test_init_run_and_capture_accept_a_finding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = write_finding(root / "finding.json", _finding())
            run, run_directory = _run(path, root)
            self.assertEqual(run.defect_report, str(path.resolve()))

            record = capture_defect_report(run_directory, source_file=path)

            self.assertEqual(record["title"], _finding()["title"])
            self.assertEqual(record["unmet_conditions"], ["case-sensitive path comparison"])
            self.assertEqual(load_issue_record(run_directory)["finding"]["base_commit"], COMMIT)
            briefing = (run_directory / "issue.md").read_text(encoding="utf-8")
            self.assertIn("# Self-reported defect: A case-only rename", briefing)
            self.assertIn("Conditions the defect needs", briefing)

    def test_an_invalid_finding_is_refused_at_init_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "finding.json"
            path.write_text(json.dumps(blank_finding()), encoding="utf-8")
            with self.assertRaises(FindingError):
                _run(path, root)

    def test_a_finding_from_another_commit_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = write_finding(root / "finding.json", _finding())
            with self.assertRaises(ValueError) as raised:
                _run(path, root, commit="b" * 40)
            self.assertIn("recorded at", str(raised.exception))


class CommandTests(unittest.TestCase):
    def test_init_writes_a_template_that_fails_until_filled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "finding.json"
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(["finding", str(path), "--init"]), 0)
                self.assertEqual(main(["finding", str(path)]), 1)
                self.assertEqual(main(["finding", str(path), "--init"]), 2)
            path.write_text(json.dumps(_finding()), encoding="utf-8")
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(["finding", str(path)]), 0)
            self.assertTrue(json.loads(output.getvalue())["valid"])


if __name__ == "__main__":
    unittest.main()
