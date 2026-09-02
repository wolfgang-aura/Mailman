from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from mailman.review_page import parse_diff, render_run_page, write_run_page


DIFF = """diff --git a/pkg/thing.py b/pkg/thing.py
index 1111111..2222222 100644
--- a/pkg/thing.py
+++ b/pkg/thing.py
@@ -10,3 +10,4 @@ def thing():
     first
-    removed line
+    added line
+    another added line
     last
"""


def write_run(root: Path, **overrides: object) -> Path:
    """A run directory with the files the page reads, and nothing else."""
    run_directory = root / "20260902T000000Z-aaaaaa"
    (run_directory / "export").mkdir(parents=True)
    (run_directory / "commands").mkdir()
    (run_directory / "agent-executions").mkdir()
    run = {
        "run_id": "20260902T000000Z-aaaaaa",
        "status": "READY_FOR_HUMAN_REVIEW",
        "repository": "https://github.com/example/project.git",
        "base_commit": "a" * 40,
        "primary": {"agent": "claude", "model": "claude-model-id"},
        "reviewer": {"agent": "codex", "model": "codex-model-id"},
        "human_decision": None,
    }
    run.update(overrides)
    (run_directory / "run.json").write_text(json.dumps(run), encoding="utf-8")
    (run_directory / "issue.json").write_text(
        json.dumps(
            {
                "title": "Thing breaks under load",
                "state": "OPEN",
                "reference": {
                    "number": 7,
                    "url": "https://github.com/example/project/issues/7",
                },
            }
        ),
        encoding="utf-8",
    )
    (run_directory / "orchestration.json").write_text(
        json.dumps(
            {
                "final_status": "READY_FOR_HUMAN_REVIEW",
                "revisions_used": 0,
                "review_cycles": 1,
                "steps": [
                    {
                        "name": "verification:primary",
                        "at": "2026-09-02T00:01:00+00:00",
                        "ok": True,
                        "detail": "verification exited with code 0",
                    },
                    {
                        "name": "verdict",
                        "at": "2026-09-02T00:02:00+00:00",
                        "ok": True,
                        "detail": "reviewer verdict APPROVE",
                        "data": {"verdict": "APPROVE"},
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    (run_directory / "export" / "changes.diff").write_text(DIFF, encoding="utf-8")
    (run_directory / "commands" / "0001.json").write_text(
        json.dumps(
            {
                "command": ["python", "-m", "pytest", "-q"],
                "exit_code": 0,
                "timed_out": False,
                "stdout": "12 passed",
                "stderr": "",
            }
        ),
        encoding="utf-8",
    )
    return run_directory


class DiffParsingTests(unittest.TestCase):
    def test_a_file_reports_its_own_added_and_removed_counts(self) -> None:
        files = parse_diff(DIFF)

        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].path, "pkg/thing.py")
        self.assertEqual((files[0].added, files[0].removed), (2, 1))

    def test_line_numbers_advance_on_the_side_the_line_belongs_to(self) -> None:
        rows = parse_diff(DIFF)[0].rows
        kinds = [row[0] for row in rows]

        self.assertEqual(kinds, ["meta", "ctx", "del", "add", "add", "ctx"])
        # context row carries both sides, an addition only the new side
        self.assertEqual(rows[1][1:3], ("10", "10"))
        self.assertEqual(rows[2][1:3], ("11", ""))
        self.assertEqual(rows[3][1:3], ("", "11"))
        self.assertEqual(rows[5][1:3], ("12", "13"))

    def test_an_empty_diff_yields_no_files(self) -> None:
        self.assertEqual(parse_diff(""), [])


class ReviewPageTests(unittest.TestCase):
    def test_the_page_leads_with_the_decision_a_human_has_to_make(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            run_directory = write_run(Path(temporary_directory))
            page = render_run_page(run_directory)

        self.assertIn("READY_FOR_HUMAN_REVIEW", page)
        self.assertIn("APPROVE", page)
        self.assertIn("1 of 1 passed", page)
        self.assertIn("Thing breaks under load", page)
        self.assertIn("https://github.com/example/project/issues/7", page)
        # the patch comes before the evidence that supports it
        self.assertLess(page.index("The patch"), page.index("Timeline"))

    def test_agent_text_cannot_inject_markup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            run_directory = write_run(Path(temporary_directory))
            (run_directory / "primary-report.md").write_text(
                "<script>alert('run')</script>", encoding="utf-8"
            )
            page = render_run_page(run_directory)

        self.assertNotIn("<script>alert", page)
        self.assertIn("&lt;script&gt;alert", page)

    def test_host_paths_are_shortened_to_the_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            run_directory = write_run(Path(temporary_directory))
            (run_directory / "commands" / "0001.json").write_text(
                json.dumps(
                    {
                        "command": [
                            str(run_directory.resolve() / "environment" / "python.exe"),
                            "-m",
                            "pytest",
                        ],
                        "exit_code": 1,
                        "timed_out": False,
                        "stdout": "",
                        "stderr": "boom",
                    }
                ),
                encoding="utf-8",
            )
            page = render_run_page(run_directory)

        self.assertNotIn(str(run_directory.resolve()), page)
        self.assertIn("&lt;run&gt;/environment/python.exe", page)

    def test_the_readable_account_comes_before_the_code(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            run_directory = write_run(Path(temporary_directory))
            (run_directory / "reviewer-report.md").write_text(
                "No required changes.\n\nMAILMAN-VERDICT: APPROVE\n", encoding="utf-8"
            )
            page = render_run_page(run_directory)

        self.assertLess(page.index("The review"), page.index("The diagnosis"))
        self.assertLess(page.index("The diagnosis"), page.index("The patch"))
        self.assertLess(page.index("The patch"), page.index("Timeline"))
        # the verdict is a pill at the top, so the contract line is not repeated
        self.assertNotIn("MAILMAN-VERDICT: APPROVE</p>", page)

    def test_a_report_is_rendered_as_prose_not_as_raw_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            run_directory = write_run(Path(temporary_directory))
            (run_directory / "primary-report.md").write_text(
                "## Summary\n\n**Root cause**: `send()` returns early.\n\n"
                "- reproduced it\n- fixed it\n",
                encoding="utf-8",
            )
            page = render_run_page(run_directory)

        self.assertIn("<h4>Summary</h4>", page)
        self.assertIn("<strong>Root cause</strong>", page)
        self.assertIn("<code>send()</code>", page)
        self.assertIn("<li>reproduced it</li>", page)
        self.assertNotIn("## Summary", page)

    def test_a_run_with_no_patch_says_so_instead_of_failing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            run_directory = write_run(Path(temporary_directory))
            (run_directory / "export" / "changes.diff").unlink()
            (run_directory / "orchestration.json").unlink()
            page = render_run_page(run_directory)

        self.assertIn("No patch was exported", page)
        self.assertIn("No orchestration record", page)

    def test_write_run_page_defaults_into_the_run_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            run_directory = write_run(Path(temporary_directory))
            destination = write_run_page(run_directory)
            written = destination.read_text(encoding="utf-8")

        self.assertEqual(destination.name, "review.html")
        self.assertIn("<!DOCTYPE html>", written)


if __name__ == "__main__":
    unittest.main()
