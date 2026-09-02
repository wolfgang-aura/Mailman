from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from mailman.artifacts import create_run
from mailman.environment import (
    environment_command,
    load_environment_record,
    load_plan,
    prepare_environment,
)


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


def make_workspace(root: Path) -> Path:
    workspace = root / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "code.txt").write_text("base\n", encoding="utf-8")
    git(workspace, "init", "--initial-branch=main")
    git(workspace, "config", "user.name", "Fixture")
    git(workspace, "config", "user.email", "fixture@example.invalid")
    git(workspace, "add", "--", "code.txt")
    git(workspace, "commit", "-m", "base")
    return workspace


def make_run(root: Path):
    return create_run(
        repository="https://github.com/example/project.git",
        issue="https://github.com/example/project/issues/7",
        base_commit="c" * 40,
        primary="codex",
        reviewer="claude",
        data_root=root / "runs",
    )


class PlanValidationTests(unittest.TestCase):
    def _plan(self, data: dict) -> Path:
        directory = Path(tempfile.mkdtemp())
        path = directory / "plan.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_rejects_an_unknown_schema_version(self) -> None:
        path = self._plan({"schema_version": 2, "steps": []})
        with self.assertRaisesRegex(ValueError, "schema_version 1"):
            load_plan(path)

    def test_rejects_a_plan_without_steps(self) -> None:
        path = self._plan({"schema_version": 1, "steps": []})
        with self.assertRaisesRegex(ValueError, "at least one step"):
            load_plan(path)

    def test_rejects_an_unknown_working_directory(self) -> None:
        path = self._plan(
            {
                "schema_version": 1,
                "steps": [
                    {"name": "install", "command": ["echo"], "working_directory": "home"}
                ],
            }
        )
        with self.assertRaisesRegex(ValueError, "working_directory must be"):
            load_plan(path)

    def test_rejects_a_register_entry_without_an_executable(self) -> None:
        path = self._plan(
            {
                "schema_version": 1,
                "steps": [{"name": "install", "command": ["echo"]}],
                "register": [{"name": "python"}],
            }
        )
        with self.assertRaisesRegex(ValueError, "name and an executable"):
            load_plan(path)


class EnvironmentPreparationTests(unittest.TestCase):
    def test_runs_steps_and_registers_the_prepared_interpreter(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            workspace = make_workspace(root)
            _, run_directory = make_run(root)
            plan = {
                "schema_version": 1,
                "steps": [
                    {
                        "name": "write-marker",
                        "command": [
                            sys.executable,
                            "-c",
                            "import pathlib,sys;"
                            "pathlib.Path(sys.argv[1],'marker.txt')"
                            ".write_text('installed')",
                            "{environment}",
                        ],
                    }
                ],
                "register": [
                    {
                        "name": "python",
                        "executable": sys.executable,
                        "probe_arguments": ["--version"],
                    }
                ],
            }

            record = prepare_environment(
                run_directory, workspace=workspace, plan=plan, timeout_seconds=120
            )

            self.assertTrue(record["success"], record)
            self.assertTrue(record["workspace_clean"])
            self.assertEqual(
                (run_directory / "environment" / "marker.txt").read_text(), "installed"
            )
            toolchain = json.loads(
                (run_directory / "toolchain.json").read_text(encoding="utf-8")
            )
            self.assertIn("python", toolchain["tools"])
            self.assertEqual(load_environment_record(run_directory)["success"], True)

    def test_stops_at_the_first_failing_step(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            workspace = make_workspace(root)
            _, run_directory = make_run(root)
            plan = {
                "schema_version": 1,
                "steps": [
                    {"name": "fail", "command": [sys.executable, "-c", "raise SystemExit(3)"]},
                    {"name": "never", "command": [sys.executable, "-c", "print('no')"]},
                ],
            }

            record = prepare_environment(
                run_directory, workspace=workspace, plan=plan, timeout_seconds=120
            )

            self.assertFalse(record["success"])
            self.assertEqual(len(record["steps"]), 1)
            self.assertEqual(record["detail"], "step fail failed")
            self.assertIsNone(record["workspace_clean"])

    def test_fails_when_preparation_dirties_the_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            workspace = make_workspace(root)
            _, run_directory = make_run(root)
            plan = {
                "schema_version": 1,
                "steps": [
                    {
                        "name": "install-in-place",
                        "command": [
                            sys.executable,
                            "-c",
                            "import pathlib;pathlib.Path('build-artifact.txt')"
                            ".write_text('x')",
                        ],
                    }
                ],
            }

            record = prepare_environment(
                run_directory, workspace=workspace, plan=plan, timeout_seconds=120
            )

            self.assertFalse(record["success"])
            self.assertFalse(record["workspace_clean"])
            self.assertIn("left changes in the workspace", record["detail"])


    def test_re_prepares_a_workspace_that_already_holds_a_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            workspace = make_workspace(root)
            _, run_directory = make_run(root)
            (workspace / "code.txt").write_text("candidate\n", encoding="utf-8")
            (workspace / "new-module.py").write_text("x = 1\n", encoding="utf-8")
            plan = {
                "schema_version": 1,
                "steps": [
                    {
                        "name": "touch-nothing",
                        "command": [sys.executable, "-c", "pass"],
                    }
                ],
            }

            record = prepare_environment(
                run_directory, workspace=workspace, plan=plan, timeout_seconds=120
            )

            self.assertTrue(record["success"], record.get("detail"))
            self.assertFalse(record["workspace_clean"])
            self.assertTrue(record["workspace_unchanged"])

    def test_fails_when_preparation_edits_an_already_changed_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            workspace = make_workspace(root)
            _, run_directory = make_run(root)
            (workspace / "code.txt").write_text("candidate\n", encoding="utf-8")
            plan = {
                "schema_version": 1,
                "steps": [
                    {
                        "name": "edit-in-place",
                        "command": [
                            sys.executable,
                            "-c",
                            "import pathlib;pathlib.Path('code.txt')"
                            ".write_text('preparation wrote this')",
                        ],
                    }
                ],
            }

            record = prepare_environment(
                run_directory, workspace=workspace, plan=plan, timeout_seconds=120
            )

            self.assertFalse(record["success"])
            self.assertFalse(record["workspace_unchanged"])
            self.assertIn("left changes in the workspace", record["detail"])


class EnvironmentCommandTests(unittest.TestCase):
    def test_expands_the_environment_token_in_a_verification_command(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            run_directory = Path(temporary_directory)
            expanded = environment_command(
                run_directory, ["{environment}/bin/python", "-m", "pytest"]
            )
            self.assertTrue(expanded[0].endswith("environment/bin/python"))
            self.assertEqual(expanded[1:], ["-m", "pytest"])


if __name__ == "__main__":
    unittest.main()
