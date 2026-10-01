"""The CI marker lane beside the touched tests: Mailman #302.

edgartools#1386 changed a parser internal. 18 tests reached it through
`Filing` and `EightK`, failed CI's `pytest -n auto -m 'fast'` lane, and were
never selected because none of them imports the changed module.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from mailman.touched_tests import (
    MARKER_LANE_CAP_VARIABLE,
    MARKER_LANE_TIMEOUT_SECONDS,
    marker_lane_cap_seconds,
    marker_lanes,
    run_touched_tests,
    touched_tests_verdict,
)
from tests.test_touched_tests import _result

FAST_WORKFLOW = """\
name: CI
on: [push]
jobs:
  test-fast:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Fast tests
        run: uv run pytest -n auto -m 'fast'
"""


def _write_workflow(workspace: Path, text: str, name: str = "ci.yml") -> None:
    workflows = workspace / ".github" / "workflows"
    workflows.mkdir(parents=True, exist_ok=True)
    (workflows / name).write_text(text, encoding="utf-8")


class LaneDetectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = TemporaryDirectory()
        self.workspace = Path(self._temporary.name)

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def test_a_marker_and_its_xdist_value_are_read_from_a_run_step(self) -> None:
        _write_workflow(self.workspace, FAST_WORKFLOW)
        lanes = marker_lanes(self.workspace)
        self.assertEqual(len(lanes), 1)
        self.assertEqual(lanes[0]["marker"], "fast")
        self.assertEqual(lanes[0]["xdist"], "auto")
        self.assertEqual(lanes[0]["paths"], [])
        self.assertEqual(lanes[0]["workflow"], ".github/workflows/ci.yml")

    def test_a_block_script_with_paths_and_continuations(self) -> None:
        (self.workspace / "tests" / "issues").mkdir(parents=True)
        _write_workflow(
            self.workspace,
            "jobs:\n"
            "  regression:\n"
            "    steps:\n"
            "      - run: |\n"
            "          pip install -e .\n"
            "          python -m pytest tests/issues --cov edgar \\\n"
            "            -m \"regression and not network\" --durations 10\n"
            "          echo done\n",
        )
        lanes = marker_lanes(self.workspace)
        self.assertEqual(len(lanes), 1)
        self.assertEqual(lanes[0]["marker"], "regression and not network")
        self.assertEqual(lanes[0]["paths"], ["tests/issues"])
        self.assertIsNone(lanes[0]["xdist"])

    def test_python_dash_m_pytest_is_not_a_marker(self) -> None:
        _write_workflow(self.workspace, "steps:\n  - run: python -m pytest -q\n")
        self.assertEqual(marker_lanes(self.workspace), [])

    def test_a_matrix_marker_and_a_later_command_are_ignored(self) -> None:
        _write_workflow(
            self.workspace,
            "steps:\n"
            "  - run: pytest -m ${{ matrix.marker }}\n"
            "  - run: pytest -q && python -m build\n",
        )
        self.assertEqual(marker_lanes(self.workspace), [])

    def test_duplicate_lanes_are_listed_once(self) -> None:
        _write_workflow(self.workspace, FAST_WORKFLOW, "a.yml")
        _write_workflow(self.workspace, FAST_WORKFLOW, "b.yaml")
        self.assertEqual(len(marker_lanes(self.workspace)), 1)

    def test_no_workflows_is_no_lane(self) -> None:
        self.assertEqual(marker_lanes(self.workspace), [])

    def test_the_cap_defaults_to_ten_minutes_and_can_be_set(self) -> None:
        with patch.dict(os.environ, {MARKER_LANE_CAP_VARIABLE: ""}):
            self.assertEqual(marker_lane_cap_seconds(), MARKER_LANE_TIMEOUT_SECONDS)
        self.assertEqual(MARKER_LANE_TIMEOUT_SECONDS, 600)
        with patch.dict(os.environ, {MARKER_LANE_CAP_VARIABLE: "90"}):
            self.assertEqual(marker_lane_cap_seconds(), 90.0)


class LaneExecutor:
    """Answers probes, git, and the lane on the candidate and at base."""

    def __init__(
        self,
        *,
        candidate: list[str],
        at_base: list[str] | None = None,
        again: list[str] | None = None,
        lane_exit: int | None = None,
        lane_timed_out: bool = False,
        xdist: bool = False,
    ) -> None:
        self.candidate = candidate
        self.at_base = at_base or []
        self.again = candidate if again is None else again
        self.lane_exit = lane_exit
        self.lane_timed_out = lane_timed_out
        self.xdist = xdist
        self.calls: list[dict[str, object]] = []

    def __call__(self, command, *, working_directory, timeout_seconds, **options):
        command = list(command)
        self.calls.append(
            {"command": command, "cwd": working_directory, "timeout": timeout_seconds,
             "environment": options.get("environment")}
        )
        if command[:3] == ["git", "worktree", "add"]:
            Path(command[4]).mkdir(parents=True, exist_ok=True)
            return _result(command)
        if command[0] == "git":
            return _result(command)
        if command[1:] == ["-c", "import pytest"]:
            return _result(command)
        if command[1:] == ["-c", "import xdist"]:
            return _result(command, exit_code=0 if self.xdist else 1)
        if command[1] == "-c":
            # The base tree's import origin for the changed package.
            return _result(command, stdout=str(Path(working_directory) / "edgar" / "__init__.py"))
        on_base = str(working_directory).endswith("lane-base")
        if "--continue-on-collection-errors" in command:
            if self.lane_timed_out and not on_base:
                return _result(command, exit_code=-1, timed_out=True)
            failing = self.at_base if on_base else self.candidate
            exit_code = self.lane_exit if self.lane_exit is not None and not on_base else (
                1 if failing else 0
            )
            return _result(
                command,
                exit_code=exit_code,
                stdout="".join(f"FAILED {node} - boom\n" for node in failing)
                + f"{len(failing)} failed, 70 passed in 3.1s\n",
            )
        # A rerun of the new failures on the candidate, by node id.
        return _result(
            command,
            exit_code=1 if self.again else 0,
            stdout="".join(f"FAILED {node} - boom\n" for node in self.again),
        )

    def lane_runs(self) -> list[dict[str, object]]:
        return [c for c in self.calls if "--continue-on-collection-errors" in c["command"]]


class LaneRunTests(unittest.TestCase):
    """The fake fixture: `tests/test_filing.py` reaches `edgar/parser.py` only via `edgar`."""

    regression = "tests/issues/test_filing.py::test_items"

    def setUp(self) -> None:
        self._temporary = TemporaryDirectory()
        root = Path(self._temporary.name)
        self.run_directory = root / "run"
        self.workspace = root / "workspace"
        (self.run_directory / "environment" / "Scripts").mkdir(parents=True)
        (self.run_directory / "environment" / "Scripts" / "python.exe").write_bytes(b"")
        (self.workspace / "edgar" / "documents").mkdir(parents=True)
        (self.workspace / "edgar" / "__init__.py").write_text("", encoding="utf-8")
        (self.workspace / "edgar" / "documents" / "parser.py").write_text(
            "x = 1\n", encoding="utf-8"
        )
        (self.workspace / "tests" / "issues").mkdir(parents=True)
        (self.workspace / "tests" / "issues" / "test_filing.py").write_text(
            "from edgar import Filing\n", encoding="utf-8"
        )
        _write_workflow(self.workspace, FAST_WORKFLOW)

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def _run(self, executor: LaneExecutor, **overrides):
        arguments = {
            "diff": "diff --git a/edgar/documents/parser.py b/edgar/documents/parser.py\n",
            "changed_paths": ["edgar/documents/parser.py"],
            "workspace": self.workspace,
            "base_commit": "abc123",
        }
        arguments.update(overrides)
        with patch("mailman.touched_tests.execute", executor), patch(
            "mailman.target_checks.execute", executor
        ):
            return run_touched_tests(self.run_directory, **arguments)

    def test_a_failure_only_the_lane_catches_blocks(self) -> None:
        executor = LaneExecutor(candidate=[self.regression])
        record = self._run(executor)
        self.assertEqual(record["reason"], "no-matching-tests")
        lane = record["marker_lane"]
        self.assertEqual(lane["status"], "ran")
        self.assertEqual(lane["regressions"], [self.regression])
        runs = executor.lane_runs()
        self.assertEqual(len(runs), 2)
        self.assertTrue(str(runs[1]["cwd"]).endswith("lane-base"))
        self.assertIn("fast", runs[0]["command"])
        self.assertEqual(runs[0]["timeout"], MARKER_LANE_TIMEOUT_SECONDS)
        code, detail = touched_tests_verdict(record)
        self.assertEqual(code, "touched-tests-failed")
        self.assertIn("-m fast", detail)
        self.assertIn(self.regression, detail)
        stored = json.loads(
            (self.run_directory / "touched-tests.json").read_text(encoding="utf-8")
        )
        self.assertEqual(stored["marker_lane"]["regressions"], [self.regression])

    def test_a_failure_shared_with_base_does_not_block(self) -> None:
        executor = LaneExecutor(candidate=[self.regression], at_base=[self.regression])
        record = self._run(executor)
        self.assertEqual(record["marker_lane"]["regressions"], [])
        self.assertEqual(record["marker_lane"]["failing_at_base"], [self.regression])
        code, detail = touched_tests_verdict(record)
        self.assertIsNone(code)
        self.assertIn("failing at base too", detail)

    def test_a_failure_that_passes_on_rerun_is_flaky(self) -> None:
        executor = LaneExecutor(candidate=[self.regression], again=[])
        record = self._run(executor)
        self.assertEqual(record["marker_lane"]["flaky"], [self.regression])
        self.assertIsNone(touched_tests_verdict(record)[0])

    def test_a_passing_lane_never_touches_the_base_tree(self) -> None:
        executor = LaneExecutor(candidate=[])
        record = self._run(executor)
        self.assertEqual(record["marker_lane"]["status"], "ran")
        self.assertEqual(len(executor.lane_runs()), 1)
        self.assertFalse(any(c["command"][0] == "git" for c in executor.calls))
        self.assertIsNone(touched_tests_verdict(record)[0])

    def test_xdist_is_left_out_when_the_environment_lacks_it(self) -> None:
        executor = LaneExecutor(candidate=[])
        self._run(executor)
        self.assertNotIn("-n", executor.lane_runs()[0]["command"])
        executor = LaneExecutor(candidate=[], xdist=True)
        self._run(executor)
        command = executor.lane_runs()[0]["command"]
        self.assertEqual(command[command.index("-n") + 1], "auto")

    def test_the_frozen_verification_deselects_and_marker_apply(self) -> None:
        (self.run_directory / "prompts.json").write_text(
            json.dumps(
                {
                    "verification_command": [
                        "python", "-m", "pytest", "-m", "not screen",
                        "--deselect", "tests/issues/test_dll.py::test_blocked",
                    ]
                }
            ),
            encoding="utf-8",
        )
        executor = LaneExecutor(candidate=[])
        record = self._run(executor)
        command = executor.lane_runs()[0]["command"]
        self.assertEqual(command[command.index("-m", 3) + 1], "(fast) and (not screen)")
        self.assertIn("tests/issues/test_dll.py::test_blocked", command)
        self.assertEqual(
            record["marker_lane"]["deselected"], ["tests/issues/test_dll.py::test_blocked"]
        )

    def test_a_lane_past_the_cap_falls_back_and_says_so(self) -> None:
        executor = LaneExecutor(candidate=[self.regression], lane_timed_out=True)
        record = self._run(executor, lane_cap_seconds=30)
        lane = record["marker_lane"]
        self.assertEqual((lane["status"], lane["reason"]), ("fallback", "exceeded-cap"))
        self.assertEqual(executor.lane_runs()[0]["timeout"], 30)
        code, detail = touched_tests_verdict(record)
        self.assertIsNone(code)
        self.assertIn("not used (exceeded-cap)", detail)

    def test_a_lane_that_cannot_be_collected_falls_back(self) -> None:
        executor = LaneExecutor(candidate=[], lane_exit=4)
        lane = self._run(executor)["marker_lane"]
        self.assertEqual((lane["status"], lane["reason"]), ("fallback", "not-collected"))
        executor = LaneExecutor(candidate=[], lane_exit=5)
        lane = self._run(executor)["marker_lane"]
        self.assertEqual(lane["reason"], "collected-nothing")

    def test_no_lane_and_no_base_commit_fall_back_without_running(self) -> None:
        executor = LaneExecutor(candidate=[])
        lane = self._run(executor, base_commit=None)["marker_lane"]
        self.assertEqual(lane["reason"], "no-base-commit")
        self.assertEqual(executor.lane_runs(), [])
        (self.workspace / ".github" / "workflows" / "ci.yml").unlink()
        lane = self._run(executor)["marker_lane"]
        self.assertEqual((lane["status"], lane["reason"]), ("fallback", "no-lane"))

    def test_a_base_that_imports_the_candidate_falls_back(self) -> None:
        executor = LaneExecutor(candidate=[self.regression])
        original = executor.__call__

        def candidate_origin(command, *, working_directory, timeout_seconds, **options):
            if list(command)[1] == "-c" and "find_spec" in command[2]:
                return _result(list(command), stdout=str(self.workspace / "edgar" / "__init__.py"))
            return original(
                command, working_directory=working_directory,
                timeout_seconds=timeout_seconds, **options,
            )

        with patch("mailman.touched_tests.execute", candidate_origin), patch(
            "mailman.target_checks.execute", candidate_origin
        ):
            record = run_touched_tests(
                self.run_directory,
                diff="d",
                changed_paths=["edgar/documents/parser.py"],
                workspace=self.workspace,
                base_commit="abc123",
            )
        self.assertEqual(record["marker_lane"]["reason"], "base-imports-elsewhere")


def _git(workspace: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments], cwd=workspace, capture_output=True, text=True, check=True
    ).stdout.strip()


class RealLaneTests(unittest.TestCase):
    """A real repository, git worktree and pytest: the edgartools#1386 shape."""

    def test_a_marked_test_reaching_the_change_indirectly_is_a_regression(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            run_directory = root / "run"
            run_directory.mkdir()
            (workspace / "pkg").mkdir(parents=True)
            (workspace / "tests").mkdir()
            (workspace / "pkg" / "__init__.py").write_text("", encoding="utf-8")
            (workspace / "pkg" / "core.py").write_text(
                "def value():\n    return 1\n", encoding="utf-8"
            )
            (workspace / "pkg" / "api.py").write_text(
                "from pkg.core import value\n\n\ndef answer():\n    return value()\n",
                encoding="utf-8",
            )
            (workspace / "tests" / "test_api.py").write_text(
                "import pytest\n\nfrom pkg.api import answer\n\n\n"
                "@pytest.mark.fast\ndef test_answer():\n    assert answer() == 1\n\n\n"
                "@pytest.mark.fast\ndef test_known_broken():\n    assert False\n\n\n"
                "def test_slow_unmarked():\n    assert answer() == 1\n",
                encoding="utf-8",
            )
            (workspace / "pytest.ini").write_text(
                "[pytest]\nmarkers =\n    fast: quick offline tests\n", encoding="utf-8"
            )
            _write_workflow(workspace, FAST_WORKFLOW)
            _git(workspace, "init", "--initial-branch=main")
            _git(workspace, "config", "user.name", "Fixture")
            _git(workspace, "config", "user.email", "fixture@example.invalid")
            _git(workspace, "add", "--", ".")
            _git(workspace, "commit", "-m", "base")
            base = _git(workspace, "rev-parse", "HEAD")
            (workspace / "pkg" / "core.py").write_text(
                "def value():\n    return 2\n", encoding="utf-8"
            )
            with patch(
                "mailman.touched_tests.environment_python", return_value=sys.executable
            ):
                record = run_touched_tests(
                    run_directory,
                    diff="diff --git a/pkg/core.py b/pkg/core.py\n",
                    changed_paths=["pkg/core.py"],
                    workspace=workspace,
                    base_commit=base,
                )
            self.assertEqual(record["reason"], "no-matching-tests")
            lane = record["marker_lane"]
            self.assertEqual(lane["status"], "ran", lane["detail"] + lane["output_tail"])
            self.assertIsNone(lane["xdist"])
            self.assertEqual(lane["regressions"], ["tests/test_api.py::test_answer"])
            self.assertEqual(lane["failing_at_base"], ["tests/test_api.py::test_known_broken"])
            self.assertTrue(lane["base_imports"].endswith("__init__.py"))
            self.assertEqual(touched_tests_verdict(record)[0], "touched-tests-failed")
            self.assertFalse((run_directory / "scratch" / "lane-base").exists())


if __name__ == "__main__":
    unittest.main()
