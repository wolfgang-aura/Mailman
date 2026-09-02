from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from mailman.workspace import WorkspaceState, inspect_workspace, prepare_workspace


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


class WorkspaceInspectionTests(unittest.TestCase):
    def test_names_the_paths_that_make_a_workspace_dirty(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory) / "workspace"
            workspace.mkdir()
            (workspace / "code.txt").write_text("base\n", encoding="utf-8")
            git(workspace, "init", "--initial-branch=main")
            git(workspace, "config", "user.name", "Fixture")
            git(workspace, "config", "user.email", "fixture@example.invalid")
            git(workspace, "add", "--", "code.txt")
            git(workspace, "commit", "-m", "base")
            (workspace / "code.txt").write_text("changed\n", encoding="utf-8")
            (workspace / "new.txt").write_text("added\n", encoding="utf-8")

            state = inspect_workspace(workspace)

            self.assertFalse(state.clean)
            self.assertEqual(len(state.changes), 2)
            description = state.describe_changes()
            self.assertIn("code.txt", description)
            self.assertIn("new.txt", description)

    def test_describe_changes_truncates_a_long_list(self) -> None:
        state = WorkspaceState(
            path=Path("."),
            head="a" * 40,
            clean=False,
            changes=tuple(f"M file{index}.py" for index in range(12)),
        )

        self.assertIn("and 2 more", state.describe_changes())


class WorkspacePreparationTests(unittest.TestCase):
    def test_clones_exact_commit_and_reuses_only_clean_match(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "source"
            source.mkdir()
            git(source, "init", "--initial-branch=main")
            git(source, "config", "user.name", "Fixture")
            git(source, "config", "user.email", "fixture@example.invalid")
            (source / "code.txt").write_text("base\n", encoding="utf-8")
            git(source, "add", "--", "code.txt")
            git(source, "commit", "-m", "base")
            base_commit = git(source, "rev-parse", "HEAD")
            (source / "code.txt").write_text("later\n", encoding="utf-8")
            git(source, "add", "--", "code.txt")
            git(source, "commit", "-m", "later")
            run_directory = root / "run"
            run_directory.mkdir()

            first = prepare_workspace(
                repository=str(source),
                base_commit=base_commit,
                run_directory=run_directory,
                timeout_seconds=30,
            )
            state = inspect_workspace(run_directory / "workspace")
            second = prepare_workspace(
                repository=str(source),
                base_commit=base_commit,
                run_directory=run_directory,
                timeout_seconds=30,
            )

            self.assertTrue(first["success"])
            self.assertFalse(first["reused"])
            self.assertEqual(state.head, base_commit)
            self.assertTrue(state.clean)
            self.assertTrue(second["success"])
            self.assertTrue(second["reused"])
            self.assertEqual(second["reuse_count"], 1)
            self.assertIsNotNone(second["clone"])
            self.assertIsNotNone(second["checkout"])
            record = json.loads(
                (run_directory / "workspace.json").read_text(encoding="utf-8")
            )
            self.assertEqual(record["head"], base_commit)
            self.assertEqual(record["reuse_count"], 1)

    def test_refuses_existing_dirty_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "source"
            source.mkdir()
            git(source, "init", "--initial-branch=main")
            git(source, "config", "user.name", "Fixture")
            git(source, "config", "user.email", "fixture@example.invalid")
            (source / "code.txt").write_text("base\n", encoding="utf-8")
            git(source, "add", "--", "code.txt")
            git(source, "commit", "-m", "base")
            base_commit = git(source, "rev-parse", "HEAD")
            run_directory = root / "run"
            run_directory.mkdir()
            prepare_workspace(
                repository=str(source),
                base_commit=base_commit,
                run_directory=run_directory,
                timeout_seconds=30,
            )
            (run_directory / "workspace" / "code.txt").write_text(
                "dirty\n", encoding="utf-8"
            )

            with self.assertRaisesRegex(ValueError, "does not match"):
                prepare_workspace(
                    repository=str(source),
                    base_commit=base_commit,
                    run_directory=run_directory,
                    timeout_seconds=30,
                )


if __name__ == "__main__":
    unittest.main()
