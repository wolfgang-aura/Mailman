"""The touched-tests stage: https://github.com/wolfgang-aura/Mailman/issues/115.

edgartools#1329 failed CI on `tests/xbrl/test_statement_drilldown.py`, a file
that imports the module the diff changed and that the primary never ran. The
stage selects by the diff, runs in the run's own environment, and its record
is what `prepare-submission` and `handoff-check` refuse to proceed without.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from mailman.executor import CommandResult
from mailman.touched_tests import (
    TOUCHED_TESTS_FILENAME,
    load_touched_tests,
    module_names,
    parse_counts,
    run_touched_tests,
    select_test_files,
    touched_tests_verdict,
)


def _result(command: list[str], *, exit_code: int = 0, stdout: str = "", stderr: str = "",
            timed_out: bool = False) -> CommandResult:
    return CommandResult(
        command=command,
        working_directory=".",
        started_at="2026-09-17T00:00:00+00:00",
        duration_seconds=0.7,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        timed_out=timed_out,
        timeout_seconds=1200,
        environment={},
    )


class FakeExecutor:
    """Answers the pytest probe, then the test run, and remembers both."""

    def __init__(self, *, pytest_installed: bool = True, exit_code: int = 0,
                 stdout: str = "3 passed in 0.7s\n", stderr: str = "") -> None:
        self.pytest_installed = pytest_installed
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        self.calls: list[dict[str, object]] = []

    def __call__(self, command, *, working_directory, timeout_seconds, **_):
        self.calls.append(
            {"command": list(command), "cwd": working_directory, "timeout": timeout_seconds}
        )
        if command[1:] == ["-c", "import pytest"]:
            return _result(list(command), exit_code=0 if self.pytest_installed else 1)
        return _result(
            list(command), exit_code=self.exit_code, stdout=self.stdout, stderr=self.stderr
        )


class ModuleNameTests(unittest.TestCase):
    def test_a_nested_module_yields_its_path_its_package_and_its_stem(self) -> None:
        self.assertEqual(
            module_names("edgar/xbrl/xbrl.py"), ["edgar.xbrl.xbrl", "edgar.xbrl", "xbrl"]
        )

    def test_a_src_layout_prefix_is_not_part_of_the_import_path(self) -> None:
        self.assertEqual(module_names("src/thing.py"), ["thing"])

    def test_a_package_init_names_the_package(self) -> None:
        self.assertEqual(module_names("edgar/xbrl/__init__.py"), ["edgar.xbrl", "xbrl"])

    def test_a_non_python_file_has_no_module(self) -> None:
        self.assertEqual(module_names("docs/notes.md"), [])


class SelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = TemporaryDirectory()
        self.workspace = Path(self._temporary.name)
        (self.workspace / "edgar" / "xbrl").mkdir(parents=True)
        (self.workspace / "tests" / "xbrl").mkdir(parents=True)
        (self.workspace / "edgar" / "xbrl" / "xbrl.py").write_text(
            "def get_all_statements():\n    return []\n", encoding="utf-8"
        )

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def _test_file(self, relative: str, text: str) -> None:
        path = self.workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def test_a_file_importing_the_touched_module_is_picked_and_an_unrelated_one_is_not(
        self,
    ) -> None:
        self._test_file(
            "tests/xbrl/test_statement_drilldown.py",
            "from edgar.xbrl.xbrl import get_all_statements\n",
        )
        self._test_file("tests/test_other.py", "import os\n\ndef test_nothing():\n    pass\n")
        selection = select_test_files(self.workspace, ["edgar/xbrl/xbrl.py"])
        self.assertEqual(
            [entry["path"] for entry in selection["selected"]],
            ["tests/xbrl/test_statement_drilldown.py"],
        )
        self.assertIn("edgar.xbrl.xbrl", selection["selected"][0]["matched"])
        self.assertIn("edgar.xbrl.xbrl", selection["selected"][0]["reason"])
        self.assertFalse(selection["capped"])

    def test_a_dotted_path_inside_a_string_counts_as_a_reference(self) -> None:
        self._test_file(
            "tests/test_patched.py",
            "from unittest.mock import patch\npatch('edgar.xbrl.xbrl.get_all_statements')\n",
        )
        selection = select_test_files(self.workspace, ["edgar/xbrl/xbrl.py"])
        self.assertEqual(selection["selected"][0]["path"], "tests/test_patched.py")

    def test_a_bare_stem_imported_from_the_package_counts(self) -> None:
        self._test_file("tests/test_stem.py", "from edgar import xbrl\n")
        selection = select_test_files(self.workspace, ["edgar/xbrl/xbrl.py"])
        self.assertEqual(selection["selected"][0]["path"], "tests/test_stem.py")
        self.assertIn("xbrl", selection["selected"][0]["matched"])

    def test_a_changed_test_file_is_not_a_touched_module(self) -> None:
        self._test_file("tests/test_other.py", "import os\n")
        selection = select_test_files(self.workspace, ["tests/test_other.py"])
        self.assertEqual(selection["source_files"], [])
        self.assertEqual(selection["selected"], [])

    def test_the_cap_keeps_the_first_files_and_names_the_rest(self) -> None:
        for index in range(4):
            self._test_file(f"tests/test_{index}.py", "import edgar.xbrl.xbrl\n")
        selection = select_test_files(self.workspace, ["edgar/xbrl/xbrl.py"], cap=2)
        self.assertTrue(selection["capped"])
        self.assertEqual(selection["candidates"], 4)
        self.assertEqual(len(selection["selected"]), 2)
        self.assertEqual(selection["omitted"], ["tests/test_2.py", "tests/test_3.py"])


class CountParsingTests(unittest.TestCase):
    def test_a_pytest_summary_line_is_read(self) -> None:
        counts = parse_counts("pytest", "....\n12 passed, 1 failed, 2 skipped in 0.7s\n", "")
        self.assertEqual(counts, {"passed": 12, "failed": 1, "errors": 0, "skipped": 2})

    def test_a_pytest_all_green_line_is_read(self) -> None:
        counts = parse_counts("pytest", "=== 3 passed in 0.5s ===\n", "")
        self.assertEqual(counts["passed"], 3)
        self.assertEqual(counts["failed"], 0)

    def test_a_unittest_summary_is_read_from_stderr(self) -> None:
        counts = parse_counts(
            "unittest", "", "Ran 5 tests in 0.1s\n\nFAILED (failures=1, errors=2)\n"
        )
        self.assertEqual(counts, {"passed": 2, "failed": 1, "errors": 2, "skipped": 0})

    def test_no_summary_leaves_the_counts_unknown(self) -> None:
        counts = parse_counts("pytest", "Traceback\n", "")
        self.assertIsNone(counts["passed"])


class RunTouchedTestsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = TemporaryDirectory()
        root = Path(self._temporary.name)
        self.run_directory = root / "run"
        self.workspace = root / "workspace"
        (self.run_directory / "environment" / "Scripts").mkdir(parents=True)
        self.python = self.run_directory / "environment" / "Scripts" / "python.exe"
        self.python.write_bytes(b"")
        (self.workspace / "edgar" / "xbrl").mkdir(parents=True)
        (self.workspace / "tests").mkdir()
        (self.workspace / "edgar" / "xbrl" / "xbrl.py").write_text("x = 1\n", encoding="utf-8")
        (self.workspace / "tests" / "test_xbrl.py").write_text(
            "from edgar.xbrl.xbrl import x\n", encoding="utf-8"
        )
        (self.workspace / "tests" / "test_other.py").write_text("import os\n", encoding="utf-8")

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def _run(self, executor: FakeExecutor, **overrides):
        arguments = {
            "diff": "diff --git a/edgar/xbrl/xbrl.py b/edgar/xbrl/xbrl.py\n",
            "changed_paths": ["edgar/xbrl/xbrl.py"],
            "workspace": self.workspace,
        }
        arguments.update(overrides)
        with patch("mailman.touched_tests.execute", executor):
            return run_touched_tests(self.run_directory, **arguments)

    def test_the_record_holds_the_command_the_counts_and_the_selection(self) -> None:
        executor = FakeExecutor(stdout="3 passed in 0.7s\n")
        record = self._run(executor)
        self.assertTrue(record["ran"])
        self.assertEqual(record["runner"], "pytest")
        self.assertEqual(
            record["command"],
            [str(self.python), "-m", "pytest", "tests/test_xbrl.py", "-q", "-p", "no:cacheprovider"],
        )
        self.assertEqual(record["exit_code"], 0)
        self.assertEqual(record["passed"], 3)
        self.assertEqual(record["failed"], 0)
        self.assertEqual(record["duration_seconds"], 0.7)
        self.assertEqual([entry["path"] for entry in record["selected"]], ["tests/test_xbrl.py"])
        self.assertEqual(executor.calls[-1]["cwd"], self.workspace)
        self.assertEqual(executor.calls[-1]["timeout"], 20 * 60)
        stored = load_touched_tests(self.run_directory)
        self.assertEqual(stored["command"], record["command"])
        self.assertIsNone(touched_tests_verdict(record)[0])

    def test_a_failing_run_is_recorded_as_a_failure(self) -> None:
        record = self._run(FakeExecutor(exit_code=1, stdout="2 passed, 1 failed in 0.7s\n"))
        self.assertEqual(record["exit_code"], 1)
        self.assertEqual(record["failed"], 1)
        code, detail = touched_tests_verdict(record)
        self.assertEqual(code, "touched-tests-failed")
        self.assertIn("failed 1", detail)

    def test_unittest_is_used_when_pytest_is_not_installed(self) -> None:
        executor = FakeExecutor(pytest_installed=False, stderr="Ran 2 tests in 0.1s\n\nOK\n")
        record = self._run(executor)
        self.assertEqual(record["runner"], "unittest")
        self.assertEqual(record["command"][:3], [str(self.python), "-m", "unittest"])
        self.assertEqual(record["passed"], 2)

    def test_the_cap_is_recorded(self) -> None:
        for index in range(3):
            (self.workspace / "tests" / f"test_more_{index}.py").write_text(
                "import edgar.xbrl.xbrl\n", encoding="utf-8"
            )
        record = self._run(FakeExecutor(), cap=2)
        self.assertTrue(record["capped"])
        self.assertEqual(record["candidates"], 4)
        self.assertEqual(len(record["selected"]), 2)
        self.assertEqual(len(record["omitted"]), 2)
        self.assertEqual(len([p for p in record["command"] if p.startswith("tests/")]), 2)

    def test_a_missing_environment_python_is_a_not_run_record(self) -> None:
        self.python.unlink()
        executor = FakeExecutor()
        record = self._run(executor)
        self.assertFalse(record["ran"])
        self.assertEqual(record["reason"], "no-environment-python")
        self.assertEqual(executor.calls, [])
        self.assertEqual(touched_tests_verdict(record)[0], "touched-tests-not-run")

    def test_a_diff_touching_only_tests_has_nothing_to_run_and_passes(self) -> None:
        executor = FakeExecutor()
        record = self._run(executor, changed_paths=["tests/test_other.py"])
        self.assertTrue(record["ran"])
        self.assertEqual(record["reason"], "no-source-change")
        self.assertEqual(executor.calls, [])
        self.assertIsNone(touched_tests_verdict(record)[0])

    def test_a_record_for_another_diff_is_no_record(self) -> None:
        record = self._run(FakeExecutor())
        code, _ = touched_tests_verdict(record, expected_diff_sha256="0" * 64)
        self.assertEqual(code, "touched-tests-not-run")
        self.assertTrue((self.run_directory / TOUCHED_TESTS_FILENAME).is_file())
        self.assertEqual(touched_tests_verdict(None)[0], "touched-tests-not-run")
        stored = json.loads(
            (self.run_directory / TOUCHED_TESTS_FILENAME).read_text(encoding="utf-8")
        )
        self.assertEqual(stored["diff_sha256"], record["diff_sha256"])


if __name__ == "__main__":
    unittest.main()
