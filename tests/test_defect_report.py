"""A run can start from a defect the operator found, with no upstream issue.

The repositories that merge outside work fastest often have no usable issue
tracker, because their contributors open a pull request instead of filing.
`pmorissette/ffn` merged five outside authors in fourteen days while holding
four open issues, none of them a defect. Requiring an issue URL ruled those
targets out entirely. See https://github.com/wolfgang-aura/Mailman/issues/45.

Nothing here loosens a gate. The duplicate search and the reproduction carry
the whole evidence burden once there is no thread to read.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from mailman.artifacts import create_run, load_run
from mailman.claims import read_claims, render_claims
from mailman.issue import capture_defect_report, load_issue_record
from mailman.targeting import NO_CLAIM_CHECK, assess_target

REPORT = """The information ratio is computed against a scalar risk-free rate
even when the caller passes a Series, so a DataFrame input silently uses the
first column's rate for every column.
"""


def _report_file(directory: Path) -> Path:
    path = directory / "defect.md"
    path.write_text(REPORT, encoding="utf-8")
    return path


class CreateRunTests(unittest.TestCase):
    def test_defect_report_run_records_the_file_and_no_issue(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            report = _report_file(root)
            run, run_directory = create_run(
                repository="https://github.com/example/project.git",
                defect_report=report,
                base_commit="a" * 40,
                primary="claude",
                reviewer="claude",
                primary_model="claude-opus-5",
                reviewer_model="claude-opus-5",
                data_root=root / "runs",
            )

            self.assertIsNone(run.issue)
            self.assertEqual(run.defect_report, str(report.resolve()))
            loaded, _ = load_run(run.run_id, root / "runs")
            self.assertIsNone(loaded.issue)
            self.assertEqual(loaded.defect_report, str(report.resolve()))
            self.assertIn("defect report", (run_directory / "issue.md").read_text())

    def test_an_issue_and_a_defect_report_together_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            with self.assertRaises(ValueError) as raised:
                create_run(
                    repository="https://github.com/example/project.git",
                    issue="https://github.com/example/project/issues/7",
                    defect_report=_report_file(root),
                    base_commit="a" * 40,
                    primary="claude",
                    reviewer="claude",
                    primary_model="claude-opus-5",
                    reviewer_model="claude-opus-5",
                    data_root=root / "runs",
                )
            self.assertIn("exactly one", str(raised.exception))

    def test_neither_an_issue_nor_a_defect_report_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            with self.assertRaises(ValueError):
                create_run(
                    repository="https://github.com/example/project.git",
                    base_commit="a" * 40,
                    primary="claude",
                    reviewer="claude",
                    primary_model="claude-opus-5",
                    reviewer_model="claude-opus-5",
                    data_root=root / "runs",
                )

    def test_a_defect_report_that_does_not_exist_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            with self.assertRaises(ValueError) as raised:
                create_run(
                    repository="https://github.com/example/project.git",
                    defect_report=root / "absent.md",
                    base_commit="a" * 40,
                    primary="claude",
                    reviewer="claude",
                    primary_model="claude-opus-5",
                    reviewer_model="claude-opus-5",
                    data_root=root / "runs",
                )
            self.assertIn("readable file", str(raised.exception))


class CaptureTests(unittest.TestCase):
    def test_capture_marks_the_briefing_self_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            record = capture_defect_report(
                root, source_file=_report_file(root), title="Information ratio"
            )

            self.assertTrue(record["success"])
            self.assertTrue(record["self_reported"])
            self.assertEqual(record["source"], "defect-report")
            self.assertIsNone(record["reference"])
            markdown = (root / "issue.md").read_text(encoding="utf-8")
            self.assertIn("# Self-reported defect: Information ratio", markdown)
            self.assertIn("no upstream issue", markdown)
            self.assertIn("risk-free rate", markdown)
            self.assertEqual(load_issue_record(root)["self_reported"], True)

    def test_an_empty_defect_report_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            path = root / "defect.md"
            path.write_text("   \n", encoding="utf-8")
            with self.assertRaises(ValueError):
                capture_defect_report(root, source_file=path)


class ClaimsTests(unittest.TestCase):
    def _captured(self, root: Path) -> None:
        capture_defect_report(root, source_file=_report_file(root))

    def test_claims_records_that_no_thread_exists(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self._captured(root)

            def refuse(*args, **kwargs):  # pragma: no cover - must never run
                raise AssertionError("claims must not call the GitHub CLI here")

            record = read_claims(root, execute=refuse, executable="gh")

            self.assertTrue(record["success"])
            self.assertTrue(record["self_reported"])
            self.assertEqual(record["comments_read"], 0)
            self.assertEqual(record["claims"], [])
            self.assertIn("no upstream issue", record["detail"])
            self.assertIn("defect report", render_claims(record))

    def test_check_target_does_not_block_on_a_missing_claim_check(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self._captured(root)
            read_claims(root, execute=lambda *a, **k: None, executable="gh")
            (root / "duplicate-search.json").write_text(
                json.dumps({"success": True}), encoding="utf-8"
            )
            (root / "target-intel.json").write_text(
                json.dumps({"success": True, "assessment": {}}), encoding="utf-8"
            )
            (root / "reproduction.json").write_text(
                json.dumps(
                    {"success": True, "machine_checked": True, "reproduced": True}
                ),
                encoding="utf-8",
            )

            assessment = assess_target(root)

            self.assertNotIn(NO_CLAIM_CHECK, assessment.blocking)
            self.assertTrue(assessment.may_start)
            self.assertIn("no upstream issue", assessment.summary())


if __name__ == "__main__":
    unittest.main()
