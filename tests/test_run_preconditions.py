"""Preconditions that used to surface as somebody else's error message.

A run written into a target checkout became a refused orchestration on a dirty
workspace (#11). A workspace path too long for Windows became a half-finished
git checkout (#12). Both are properties of the layout, knowable before any work
starts.
"""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from mailman.artifacts import (
    DATA_ROOT_VARIABLE,
    check_data_root,
    create_run,
    default_data_root,
)
from mailman.executor import execute
from mailman.workspace import (
    MINIMUM_PATH_BUDGET,
    WINDOWS_PATH_LIMIT,
    check_path_budget,
    path_budget,
)


BASE_COMMIT = "0" * 40


def _git_init(path: Path) -> None:
    subprocess.run(
        ["git", "init", "--quiet", str(path)],
        check=True,
        capture_output=True,
        shell=False,
    )


class DataRootTests(unittest.TestCase):
    def test_the_environment_variable_wins_over_the_current_directory(self) -> None:
        with patch.dict(os.environ, {DATA_ROOT_VARIABLE: r"C:\mailman\runs"}):
            self.assertEqual(default_data_root(), Path(r"C:\mailman\runs"))

    def test_without_the_variable_the_root_follows_the_current_directory(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                default_data_root(), Path.cwd() / ".mailman" / "runs"
            )

    def test_a_directory_in_no_working_tree_is_accepted(self) -> None:
        with TemporaryDirectory() as name:
            check_data_root(Path(name) / ".mailman" / "runs")

    def test_a_run_inside_a_target_checkout_is_refused(self) -> None:
        with TemporaryDirectory() as name:
            target = Path(name) / "target"
            target.mkdir()
            _git_init(target)
            with self.assertRaises(ValueError) as raised:
                check_data_root(target / ".mailman" / "runs")
            message = str(raised.exception)
            self.assertIn("working tree", message)
            self.assertIn(DATA_ROOT_VARIABLE, message)

    def test_create_run_refuses_before_it_writes_anything(self) -> None:
        with TemporaryDirectory() as name:
            target = Path(name) / "target"
            target.mkdir()
            _git_init(target)
            root = target / ".mailman" / "runs"
            with self.assertRaises(ValueError):
                create_run(
                    repository="https://github.com/example/project.git",
                    issue="https://github.com/example/project/issues/1",
                    base_commit=BASE_COMMIT,
                    primary="codex",
                    reviewer="claude",
                    data_root=root,
                )
            self.assertFalse(root.exists())

    def test_mailman_own_checkout_is_not_somebody_elses_working_tree(self) -> None:
        check_data_root(Path.cwd() / ".mailman" / "runs")


class PathBudgetTests(unittest.TestCase):
    def test_the_budget_is_what_is_left_of_the_windows_limit(self) -> None:
        destination = Path("C:/m/.mailman/runs/20260906T000000Z-aaaaaa/workspace")
        self.assertEqual(
            path_budget(destination),
            WINDOWS_PATH_LIMIT - len(str(destination)) - len(os.sep),
        )

    def test_a_short_workspace_path_passes_on_windows(self) -> None:
        check_path_budget(Path(r"C:\mailman\r\workspace"), system="Windows")

    def test_a_deep_workspace_path_is_refused_before_the_clone(self) -> None:
        deep = Path("C:/" + "/".join(["directory"] * 18) + "/workspace")
        with self.assertRaises(ValueError) as raised:
            check_path_budget(deep, system="Windows")
        message = str(raised.exception)
        self.assertIn("Filename too long", message)
        self.assertIn(str(MINIMUM_PATH_BUDGET), message)

    def test_the_limit_is_a_windows_one(self) -> None:
        deep = Path("/" + "/".join(["directory"] * 18) + "/workspace")
        self.assertLess(check_path_budget(deep, system="Linux"), MINIMUM_PATH_BUDGET)


class BytecodeTests(unittest.TestCase):
    def test_a_command_mailman_runs_writes_no_pycache(self) -> None:
        # `__pycache__` left in a target checkout makes it dirty, which fails
        # environment preparation for a change it did not make.
        with TemporaryDirectory() as name:
            program = (
                "import os; print(os.environ.get('PYTHONDONTWRITEBYTECODE'))"
            )
            result = execute(
                [sys.executable, "-c", program],
                working_directory=Path(name),
                timeout_seconds=60,
            )
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(result.stdout.strip(), "1")


if __name__ == "__main__":
    unittest.main()
