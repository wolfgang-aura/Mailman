from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from mailman.artifacts import create_run, write_run
from mailman.export import export_patch
from mailman.models import RunStatus


def git(workspace: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(workspace), *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        shell=False,
    )
    return completed.stdout.strip()


def make_workspace(root: Path) -> tuple[Path, str]:
    workspace = root / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "code.py").write_text("def slugify(value):\n    return value\n", encoding="utf-8")
    git(workspace, "init", "--initial-branch=main")
    git(workspace, "config", "user.name", "Fixture")
    git(workspace, "config", "user.email", "fixture@example.invalid")
    git(workspace, "add", "--", "code.py")
    git(workspace, "commit", "-m", "base")
    return workspace, git(workspace, "rev-parse", "HEAD")


def make_ready_run(root: Path, base_commit: str):
    run, run_directory = create_run(
        repository="https://github.com/example/project.git",
        issue="https://github.com/example/project/issues/7",
        base_commit=base_commit,
        primary="codex",
        reviewer="claude",
        data_root=root / "runs",
    )
    run.transition(RunStatus.PRIMARY_RUNNING, "test")
    run.transition(RunStatus.REVIEW_PENDING, "test")
    run.transition(RunStatus.VERIFICATION_PENDING, "test")
    run.transition(RunStatus.READY_FOR_HUMAN_REVIEW, "test")
    write_run(run, run_directory)
    return run, run_directory


class ExportTests(unittest.TestCase):
    def test_exports_tracked_and_new_files_with_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            workspace, base_commit = make_workspace(root)
            run, run_directory = make_ready_run(root, base_commit)
            (run_directory / "issue.json").write_text(
                json.dumps(
                    {
                        "reference": {
                            "owner": "example",
                            "repository": "project",
                            "number": 7,
                            "url": run.issue,
                        },
                        "title": "Crash on empty input",
                    }
                ),
                encoding="utf-8",
            )
            (run_directory / "verification.json").write_text(
                json.dumps(
                    [
                        {
                            "command": ["python", "-m", "pytest"],
                            "exit_code": 0,
                            "timed_out": False,
                            "duration_seconds": 1.5,
                        }
                    ]
                ),
                encoding="utf-8",
            )
            (run_directory / "reviewer-report.md").write_text(
                "All three tests pass.\n\nMAILMAN-VERDICT: APPROVE\n", encoding="utf-8"
            )
            (workspace / "code.py").write_text(
                "def slugify(value):\n    return value.strip('-')\n", encoding="utf-8"
            )
            (workspace / "test_code.py").write_text("def test_it():\n    pass\n", encoding="utf-8")

            record = export_patch(
                run,
                run_directory,
                workspace=workspace,
                destination=root / "export",
            )

            self.assertEqual(record["branch"], "mailman/issue-7")
            self.assertEqual(sorted(record["changed_files"]), ["code.py", "test_code.py"])
            diff = (root / "export" / "changes.diff").read_text(encoding="utf-8")
            self.assertIn("value.strip('-')", diff)
            self.assertIn("test_code.py", diff)
            summary = (root / "export" / "summary.md").read_text(encoding="utf-8")
            self.assertIn("READY_FOR_HUMAN_REVIEW", summary)
            self.assertIn("`python -m pytest`", summary)
            self.assertIn("MAILMAN-VERDICT: APPROVE", summary)
            self.assertIn("Nothing has been pushed", summary)
            pull_request = (root / "export" / "pull-request.md").read_text(encoding="utf-8")
            self.assertIn("Closes #7.", pull_request)
            self.assertIn("Crash on empty input", pull_request)

    def test_refuses_an_unfinished_run_unless_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            workspace, base_commit = make_workspace(root)
            run, run_directory = create_run(
                repository="https://github.com/example/project.git",
                issue="https://github.com/example/project/issues/7",
                base_commit=base_commit,
                primary="codex",
                reviewer="claude",
                data_root=root / "runs",
            )
            (workspace / "code.py").write_text("changed\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "not READY_FOR_HUMAN_REVIEW"):
                export_patch(
                    run, run_directory, workspace=workspace, destination=root / "export"
                )

            record = export_patch(
                run,
                run_directory,
                workspace=workspace,
                destination=root / "export",
                require_ready=False,
            )
            self.assertEqual(record["status_at_export"], "INITIALIZED")
            self.assertEqual(record["branch"], f"mailman/run-{run.run_id}")

    def test_refuses_an_empty_change(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            workspace, base_commit = make_workspace(root)
            run, run_directory = make_ready_run(root, base_commit)

            with self.assertRaisesRegex(ValueError, "no change against base commit"):
                export_patch(
                    run, run_directory, workspace=workspace, destination=root / "export"
                )

    def test_refuses_a_diff_that_looks_like_a_credential(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            workspace, base_commit = make_workspace(root)
            run, run_directory = make_ready_run(root, base_commit)
            (workspace / "code.py").write_text(
                "TOKEN = 'ghp_abcdefghijklmnopqrstuvwxyz0123456789'\n", encoding="utf-8"
            )

            with self.assertRaisesRegex(ValueError, "credential pattern"):
                export_patch(
                    run, run_directory, workspace=workspace, destination=root / "export"
                )
            self.assertFalse((root / "export" / "changes.diff").exists())


if __name__ == "__main__":
    unittest.main()
