from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mailman.artifacts import create_run
from mailman.prompts import write_task_prompts


def make_run(root: Path):
    return create_run(
        repository="https://github.com/example/project.git",
        issue="https://github.com/example/project/issues/7",
        base_commit="b" * 40,
        primary="codex",
        reviewer="claude",
        data_root=root,
    )


class TaskPromptTests(unittest.TestCase):
    def test_primary_prompt_names_the_pre_approved_command(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            run, run_directory = make_run(Path(temporary_directory) / "runs")
            (run_directory / "issue.md").write_text(
                "# example/project#7: Crash on empty input",
                encoding="utf-8",
            )

            primary_path, reviewer_path = write_task_prompts(
                run, run_directory, verification_command=["python", "-m", "pytest"]
            )

            primary = primary_path.read_text(encoding="utf-8")
            self.assertIn("Run it yourself as `python -m pytest`", primary)
            self.assertIn("pre-approved", primary)
            self.assertIn("compound command", primary)
            # The reviewer has no such allowance, so it is not told it has one.
            self.assertNotIn(
                "pre-approved", reviewer_path.read_text(encoding="utf-8")
            )

    def test_refuses_to_build_prompts_from_the_issue_placeholder(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            run, run_directory = make_run(Path(temporary_directory) / "runs")
            with self.assertRaisesRegex(ValueError, "placeholder is still in place"):
                write_task_prompts(run, run_directory)

    def test_builds_both_prompts_from_the_captured_issue(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            run, run_directory = make_run(Path(temporary_directory) / "runs")
            (run_directory / "issue.md").write_text(
                "# example/project#7: Crash on empty input\n\nIt crashes.\n",
                encoding="utf-8",
            )

            primary_path, reviewer_path = write_task_prompts(
                run, run_directory, verification_command=["python", "-m", "pytest"]
            )

            primary = primary_path.read_text(encoding="utf-8")
            reviewer = reviewer_path.read_text(encoding="utf-8")
            for prompt in (primary, reviewer):
                self.assertIn("Crash on empty input", prompt)
                self.assertIn("python -m pytest", prompt)
                self.assertIn(run.base_commit, prompt)
            self.assertIn("Do not push, open a pull request", primary)
            self.assertIn("Do not edit any", reviewer)
            self.assertIn(f"git diff {run.base_commit}", reviewer)

    def test_prompts_state_the_evidence_rule_without_a_command(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            run, run_directory = make_run(Path(temporary_directory) / "runs")
            (run_directory / "issue.md").write_text("# Issue\n\nBody.\n", encoding="utf-8")

            primary_path, _ = write_task_prompts(run, run_directory)

            self.assertIn(
                "exit code proves nothing on its own",
                primary_path.read_text(encoding="utf-8"),
            )


if __name__ == "__main__":
    unittest.main()
