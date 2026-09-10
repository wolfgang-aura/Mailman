from __future__ import annotations

import json
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
            self.assertIn("Mailman runs it after your stage", primary)
            self.assertIn("smallest focused test", primary)
            self.assertNotIn("Run it yourself", primary)

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

    def test_prompts_include_precomputed_scope_and_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            run, run_directory = make_run(Path(temporary_directory) / "runs")
            (run_directory / "issue.md").write_text("# Issue\n\nCrash.\n", encoding="utf-8")
            (run_directory / "prescreen.json").write_text(
                '{"symbols": ["Parser.parse", "tests/test_parser.py"]}',
                encoding="utf-8",
            )
            (run_directory / "reproduction.json").write_text(
                json.dumps(
                    {
                        "success": True,
                        "reproduced": True,
                        "command": ["python", "repro.py"],
                        "exit_code": 1,
                        "timed_out": False,
                    }
                ),
                encoding="utf-8",
            )

            primary_path, reviewer_path = write_task_prompts(
                run, run_directory, verification_command=["python", "-m", "pytest"]
            )

            for path in (primary_path, reviewer_path):
                prompt = path.read_text(encoding="utf-8")
                self.assertIn("Pre-screened scope", prompt)
                self.assertIn("Parser.parse", prompt)
                self.assertIn("Baseline already proved", prompt)
                self.assertIn("python repro.py", prompt)
                self.assertIn("Do not spend time", prompt)

    def test_prompt_shows_the_exact_reproducer_and_requires_alignment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            run, run_directory = make_run(Path(temporary_directory) / "runs")
            (run_directory / "issue.md").write_text(
                "# Proxy issue\n\nDocker reports the bridge IP.\n", encoding="utf-8"
            )
            snapshot = run_directory / "reproduction-artifacts" / "test" / "repro.py"
            snapshot.parent.mkdir(parents=True)
            snapshot.write_text(
                "assert session_ip == '172.19.0.1'\n", encoding="utf-8"
            )
            (run_directory / "reproduction.json").write_text(
                json.dumps(
                    {
                        "success": True,
                        "reproduced": True,
                        "command": ["python", "-m", "pytest", "test/repro.py"],
                        "exit_code": 0,
                        "timed_out": False,
                        "artifacts": [
                            {
                                "source": "test/repro.py",
                                "snapshot": "reproduction-artifacts/test/repro.py",
                                "sha256": "a" * 64,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            primary_path, _ = write_task_prompts(
                run, run_directory, verification_command=["python", "-m", "pytest"]
            )
            prompt = primary_path.read_text(encoding="utf-8")

            self.assertIn("assert session_ip == '172.19.0.1'", prompt)
            self.assertIn("MAILMAN-REPRODUCTION-MISMATCH:", prompt)
            self.assertIn("Before running any command", prompt)

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
