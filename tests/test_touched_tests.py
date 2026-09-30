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

    def test_a_deep_module_names_its_own_package_and_not_its_grandparents(
        self,
    ) -> None:
        # zauberzeug/nicegui: `nicegui.elements` is imported by most of the
        # suite, so naming it selected eight unrelated files.
        self.assertEqual(
            module_names("nicegui/elements/leaflet/leaflet.py"),
            ["nicegui.elements.leaflet.leaflet", "nicegui.elements.leaflet", "leaflet"],
        )

    def test_a_src_layout_prefix_is_not_part_of_the_import_path(self) -> None:
        self.assertEqual(module_names("src/thing.py"), ["thing"])

    def test_a_package_init_names_the_package(self) -> None:
        self.assertEqual(module_names("edgar/xbrl/__init__.py"), ["edgar.xbrl", "xbrl"])

    def test_a_stem_that_names_a_stdlib_module_is_dropped(self) -> None:
        # prefect's plugins/collections.py matched every `import collections`
        # in the suite; 21 files ran and timed out. Mailman #248.
        self.assertEqual(
            module_names("src/prefect/_internal/plugins/collections.py"),
            ["prefect._internal.plugins.collections", "prefect._internal.plugins"],
        )

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

    def test_a_testing_directory_inside_a_package_is_library_code(self) -> None:
        # zauberzeug/nicegui: nicegui/testing/user_interaction.py is the User
        # fixture; collecting it as a test file made the stage exit 2.
        (self.workspace / "edgar" / "__init__.py").write_text("", encoding="utf-8")
        self._test_file(
            "edgar/testing/helpers.py", "from edgar.xbrl.xbrl import get_all_statements\n"
        )
        self._test_file(
            "testing/test_top_level.py", "from edgar.xbrl.xbrl import get_all_statements\n"
        )
        selection = select_test_files(self.workspace, ["edgar/xbrl/xbrl.py"])
        self.assertEqual(
            [entry["path"] for entry in selection["selected"]],
            ["testing/test_top_level.py"],
        )

    def test_a_helper_under_the_tests_directory_is_not_collected(self) -> None:
        # pretix: `src/tests/testdummy/signals.py` names the module but pytest
        # collects nothing from it, so running it exits 5.
        self._test_file("tests/test_xbrl.py", "from edgar.xbrl import xbrl\n")
        self._test_file("tests/testdummy/signals.py", "from edgar.xbrl import xbrl\n")
        self._test_file("tests/conftest.py", "from edgar.xbrl import xbrl\n")
        selection = select_test_files(self.workspace, ["edgar/xbrl/xbrl.py"])
        self.assertEqual([e["path"] for e in selection["selected"]], ["tests/test_xbrl.py"])

    def test_testpaths_limits_the_search_to_the_collected_tree(self) -> None:
        # nilearn#6607 (#145): testpaths = ["nilearn"], yet
        # examples/.../plot_second_level_association_test.py was selected and
        # run as a test because its name ends in `_test.py`.
        self._test_file(
            "pyproject.toml", '[tool.pytest.ini_options]\ntestpaths = ["edgar", "tests"]\n'
        )
        self._test_file("tests/test_xbrl.py", "from edgar.xbrl import xbrl\n")
        self._test_file("examples/plot_association_test.py", "from edgar.xbrl import xbrl\n")
        selection = select_test_files(self.workspace, ["edgar/xbrl/xbrl.py"])
        self.assertEqual([e["path"] for e in selection["selected"]], ["tests/test_xbrl.py"])

    def test_testpaths_in_an_ini_file_is_honoured(self) -> None:
        self._test_file("setup.cfg", "[tool:pytest]\ntestpaths =\n    tests\n")
        self._test_file("tests/test_xbrl.py", "from edgar.xbrl import xbrl\n")
        self._test_file("examples/test_example.py", "from edgar.xbrl import xbrl\n")
        selection = select_test_files(self.workspace, ["edgar/xbrl/xbrl.py"])
        self.assertEqual([e["path"] for e in selection["selected"]], ["tests/test_xbrl.py"])

    def test_a_package_only_match_outside_the_package_is_left_out_and_named(self) -> None:
        # nilearn#6607: nilearn/_estimator_checks/tests/test_estimator_checks_nilearn.py
        # imports `nilearn.glm.first_level` among every estimator in the
        # library; matching `nilearn.glm` ran 1644 sklearn checks and the
        # stage timed out at 20 minutes twice.
        (self.workspace / "edgar" / "xbrl" / "tests").mkdir(parents=True)
        self._test_file("edgar/xbrl/tests/test_facts.py", "from edgar.xbrl.facts import F\n")
        self._test_file("tests/test_sweep.py", "from edgar.xbrl.facts import F\n")
        self._test_file("tests/test_direct.py", "from edgar.xbrl.xbrl import X\n")
        selection = select_test_files(self.workspace, ["edgar/xbrl/xbrl.py"])
        self.assertEqual(
            [e["path"] for e in selection["selected"]],
            ["tests/test_direct.py", "edgar/xbrl/tests/test_facts.py"],
        )
        self.assertEqual(selection["indirect"], ["tests/test_sweep.py"])

    def test_without_testpaths_the_whole_tree_is_searched(self) -> None:
        self._test_file("tests/test_xbrl.py", "from edgar.xbrl import xbrl\n")
        self._test_file("examples/test_example.py", "from edgar.xbrl import xbrl\n")
        selection = select_test_files(self.workspace, ["edgar/xbrl/xbrl.py"])
        self.assertEqual(
            sorted(e["path"] for e in selection["selected"]),
            ["examples/test_example.py", "tests/test_xbrl.py"],
        )

    def test_a_test_file_the_diff_changes_runs_even_without_naming_the_module(
        self,
    ) -> None:
        # pretix#6518: the changed test reached the module only through a
        # service function and was left out.
        self._test_file("tests/test_xbrl.py", "from edgar.xbrl import xbrl\n")
        self._test_file("tests/test_invoices.py", "from edgar.services import generate\n")
        selection = select_test_files(
            self.workspace, ["edgar/xbrl/xbrl.py", "tests/test_invoices.py"]
        )
        self.assertEqual(selection["source_files"], ["edgar/xbrl/xbrl.py"])
        self.assertEqual(
            [(e["path"], e["reason"]) for e in selection["selected"]],
            [
                ("tests/test_invoices.py", "changed by the diff"),
                ("tests/test_xbrl.py", "imports or names edgar.xbrl, xbrl"),
            ],
        )

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

    def test_the_cap_keeps_files_that_import_the_module_directly(self) -> None:
        # #127: alphabetical capping dropped tests/xbrl/test_statement_drilldown.py,
        # which imports edgar.xbrl.xbrl, in favour of files that only name `xbrl`.
        for index in range(3):
            self._test_file(f"tests/core/test_{index}.py", "from edgar import xbrl\n")
        self._test_file(
            "tests/xbrl/test_statement_drilldown.py",
            "from edgar.xbrl.xbrl import get_all_statements\n",
        )
        selection = select_test_files(self.workspace, ["edgar/xbrl/xbrl.py"], cap=2)
        self.assertEqual(
            selection["selected"][0]["path"], "tests/xbrl/test_statement_drilldown.py"
        )
        self.assertIn("tests/core/test_2.py", selection["omitted"])


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


class _RunFixture(unittest.TestCase):
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


class RunTouchedTestsTests(_RunFixture):
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

    def test_a_passing_verdict_names_the_package_only_files_left_out(self) -> None:
        record = self._run(FakeExecutor(stdout="3 passed in 0.7s\n"))
        record["indirect"] = ["tests/test_sweep.py"]
        code, detail = touched_tests_verdict(record)
        self.assertIsNone(code)
        self.assertIn("left out 1 file(s)", detail)
        self.assertIn("tests/test_sweep.py", detail)

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

    def test_a_registered_network_marker_is_deselected(self) -> None:
        # #127: edgartools marks SEC-bound tests `network`; without an identity
        # they fail with IdentityNotSetError and blocked handoff-check.
        (self.workspace / "pyproject.toml").write_text(
            '[tool.pytest.ini_options]\nmarkers = [\n  "network: needs the SEC",\n'
            '  "fast",\n]\n',
            encoding="utf-8",
        )
        record = self._run(FakeExecutor())
        self.assertEqual(record["command"][-2:], ["-m", "not network"])
        self.assertEqual(record["marker_filter"], "not network")

    def test_a_network_marker_registered_in_conftest_is_deselected(self) -> None:
        (self.workspace / "tests" / "conftest.py").write_text(
            "def pytest_configure(config):\n"
            '    config.addinivalue_line("markers", "network: hits the network")\n',
            encoding="utf-8",
        )
        record = self._run(FakeExecutor())
        self.assertEqual(record["command"][-2:], ["-m", "not network"])

    def test_no_marker_filter_without_a_registered_network_marker(self) -> None:
        (self.workspace / "pytest.ini").write_text(
            "[pytest]\nmarkers =\n    slow: takes long\n", encoding="utf-8"
        )
        record = self._run(FakeExecutor())
        self.assertNotIn("not network", record["command"])
        self.assertIsNone(record["marker_filter"])


    def test_a_frozen_verification_deselect_carries_over_to_its_file(self) -> None:
        # #161: nox#302's verification deselected three tests Application
        # Control kills on this host; touched tests ran them and blocked.
        (self.run_directory / "prompts.json").write_text(
            json.dumps({"verification_command": [
                str(self.python), "-m", "pytest", "tests/test_xbrl.py",
                "--deselect", "tests/test_xbrl.py::test_host_blocked",
                "--deselect=tests/test_other.py::test_elsewhere",
            ]}),
            encoding="utf-8",
        )
        record = self._run(FakeExecutor())
        self.assertEqual(
            record["command"][-2:], ["--deselect", "tests/test_xbrl.py::test_host_blocked"]
        )
        self.assertEqual(record["deselected"], ["tests/test_xbrl.py::test_host_blocked"])

    def test_a_frozen_verification_marker_filter_carries_over(self) -> None:
        # nicegui run 20260930T113729Z-2bd5a4 verified with `-m 'not screen'`
        # (no Chrome on this host); touched tests ran the browser tests and
        # hung on chromedriver. Mailman #278.
        (self.run_directory / "prompts.json").write_text(
            json.dumps({"verification_command": [
                str(self.python), "-m", "pytest", "tests/test_xbrl.py",
                "-m", "not screen", "-q",
            ]}),
            encoding="utf-8",
        )
        record = self._run(FakeExecutor())
        self.assertEqual(record["command"][-2:], ["-m", "not screen"])
        self.assertEqual(record["marker_filter"], "not screen")

        (self.workspace / "pytest.ini").write_text(
            "[pytest]\nmarkers =\n    network: hits the network\n", encoding="utf-8"
        )
        record = self._run(FakeExecutor())
        self.assertEqual(record["command"][-2:], ["-m", "(not network) and (not screen)"])

    def test_no_prompts_record_means_nothing_deselected(self) -> None:
        record = self._run(FakeExecutor())
        self.assertNotIn("--deselect", record["command"])
        self.assertEqual(record["deselected"], [])


def _collection_error(path: str, error: str) -> str:
    return (
        "==================================== ERRORS ====================================\n"
        f"_____________________ ERROR collecting {path} _____________________\n"
        f"ImportError while importing test module 'C:\\w\\{path}'.\n"
        "Traceback:\n"
        f"E   {error}\n"
        "=========================== short test summary info ============================\n"
        f"ERROR {path}\n"
        "!!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!\n"
        "1 error in 0.40s\n"
    )


class SequenceExecutor(FakeExecutor):
    """Answers each test run from a list of (exit_code, stdout) in order."""

    def __init__(self, runs: list[tuple[int, str]]) -> None:
        super().__init__()
        self.runs = list(runs)

    def __call__(self, command, *, working_directory, timeout_seconds, **_):
        if command[1:] == ["-c", "import pytest"]:
            return super().__call__(
                command, working_directory=working_directory, timeout_seconds=timeout_seconds
            )
        self.calls.append({"command": list(command), "cwd": working_directory})
        exit_code, stdout = self.runs.pop(0)
        return _result(list(command), exit_code=exit_code, stdout=stdout)


class CollectionErrorTests(_RunFixture):
    """#127: an optional extra missing at collection omits the file, not the stage."""

    def setUp(self) -> None:
        super().setUp()
        (self.workspace / "tests" / "test_xbrl_arrow.py").write_text(
            "import pyarrow\nfrom edgar.xbrl.xbrl import x\n", encoding="utf-8"
        )

    def test_a_missing_optional_extra_omits_the_file_and_reruns_the_rest(self) -> None:
        executor = SequenceExecutor(
            [
                (2, _collection_error(
                    "tests/test_xbrl_arrow.py",
                    "ModuleNotFoundError: No module named 'pyarrow'",
                )),
                (0, "3 passed in 0.7s\n"),
            ]
        )
        record = self._run(executor)
        runs = [call["command"] for call in executor.calls if "pytest" in call["command"]]
        self.assertEqual(len(runs), 2)
        self.assertIn("tests/test_xbrl_arrow.py", runs[0])
        self.assertNotIn("tests/test_xbrl_arrow.py", runs[1])
        self.assertIn("tests/test_xbrl.py", runs[1])
        self.assertEqual(record["exit_code"], 0)
        self.assertIn("tests/test_xbrl_arrow.py", record["omitted"])
        self.assertIn("pyarrow", record["omitted_reasons"]["tests/test_xbrl_arrow.py"])
        self.assertEqual(
            [entry["path"] for entry in record["selected"]], ["tests/test_xbrl.py"]
        )
        self.assertIsNone(touched_tests_verdict(record)[0])

    def test_a_dll_this_host_blocks_omits_the_file_and_reruns_the_rest(self) -> None:
        # biopython: Tests/test_SeqIO_features.py imports Bio.PDB, whose
        # ccealign DLL Application Control blocks here. Mailman #214.
        executor = SequenceExecutor(
            [
                (2, _collection_error(
                    "tests/test_xbrl_arrow.py",
                    "ImportError: DLL load failed while importing ccealign: An "
                    "Application Control policy has blocked this file.",
                )),
                (0, "3 passed in 0.7s\n"),
            ]
        )
        record = self._run(executor)
        self.assertEqual(record["exit_code"], 0)
        self.assertIn("tests/test_xbrl_arrow.py", record["omitted"])
        reason = record["omitted_reasons"]["tests/test_xbrl_arrow.py"]
        self.assertIn("ccealign", reason)
        self.assertIn("Application Control", reason)
        self.assertIsNone(touched_tests_verdict(record)[0])

    def test_an_import_error_from_the_targets_own_package_still_fails(self) -> None:
        # `cannot import name` from the package the diff changed may be the
        # diff's own breakage; it is not an optional extra.
        record = self._run(SequenceExecutor([
            (2, _collection_error(
                "tests/test_xbrl_arrow.py",
                "ImportError: cannot import name 'x' from 'edgar.xbrl.xbrl'",
            )),
        ]))
        self.assertEqual(record["omitted_reasons"], {})
        self.assertEqual(touched_tests_verdict(record)[0], "touched-tests-failed")

    def test_a_missing_module_inside_the_workspace_still_fails(self) -> None:
        record = self._run(SequenceExecutor([
            (2, _collection_error(
                "tests/test_xbrl_arrow.py",
                "ModuleNotFoundError: No module named 'edgar.xbrl.gone'",
            )),
        ]))
        self.assertEqual(record["omitted_reasons"], {})
        self.assertEqual(touched_tests_verdict(record)[0], "touched-tests-failed")

    def test_when_every_file_is_omitted_the_stage_has_not_run(self) -> None:
        output = _collection_error(
            "tests/test_xbrl.py", "ModuleNotFoundError: No module named 'pyarrow'"
        ) + _collection_error(
            "tests/test_xbrl_arrow.py", "ModuleNotFoundError: No module named 'pyarrow'"
        )
        record = self._run(SequenceExecutor([(2, output)]))
        self.assertFalse(record["ran"])
        self.assertEqual(record["reason"], "all-selected-omitted")
        code, detail = touched_tests_verdict(record)
        self.assertEqual(code, "touched-tests-not-run")
        self.assertIn("pyarrow", detail)


class TestDirectoryTests(_RunFixture):
    """#215: suites that load data relative to their own directory run from it."""

    def test_a_root_run_that_fails_wholesale_is_rerun_from_the_test_directory(self) -> None:
        # biopython: CI runs `cd Tests && python run_tests.py`; from the root,
        # test_GenBank.py failed about 80 of 95 tests on missing data files.
        executor = SequenceExecutor([
            (1, "FAILED tests/test_xbrl.py::test_a - FileNotFoundError\n"
                "80 failed, 15 passed in 1.0s\n"),
            (0, "95 passed in 1.0s\n"),
        ])
        record = self._run(executor)
        self.assertEqual(record["exit_code"], 0)
        self.assertEqual(record["working_directory"], "tests")
        self.assertEqual(record["command"][3], "test_xbrl.py")
        self.assertEqual(Path(executor.calls[-1]["cwd"]), self.workspace / "tests")
        self.assertEqual(record["directory_retry"]["root"]["failed"], 80)
        self.assertIsNone(touched_tests_verdict(record)[0])

    def test_the_root_run_stands_when_the_test_directory_does_no_better(self) -> None:
        executor = SequenceExecutor([
            (1, "FAILED tests/test_xbrl.py::test_a - boom\n1 failed, 2 passed in 1.0s\n"),
            (1, "FAILED test_xbrl.py::test_a - boom\n1 failed, 2 passed in 1.0s\n"),
        ])
        record = self._run(executor)
        self.assertEqual(record["exit_code"], 1)
        self.assertEqual(record["working_directory"], ".")
        self.assertEqual(record["command"][3], "tests/test_xbrl.py")
        self.assertEqual(touched_tests_verdict(record)[0], "touched-tests-failed")


if __name__ == "__main__":
    unittest.main()


class BaselineExecutor(FakeExecutor):
    """The candidate run fails two nodes; the base run fails `at_base`."""

    def __init__(self, *, at_base: list[str], again: list[str] | None = None) -> None:
        super().__init__(
            exit_code=1,
            stdout=(
                "FAILED tests/test_xbrl.py::test_engine - ValueError: no netCDF\n"
                "ERROR tests/test_xbrl.py::test_groups - ImportError\n"
                "1 failed, 5 passed, 1 error in 0.7s\n"
            ),
        )
        self.at_base = at_base
        # Nodes that fail again when rerun on the candidate; all by default.
        self.again = again

    def __call__(self, command, *, working_directory, timeout_seconds, **_):
        if command[:3] == ["git", "worktree", "add"]:
            Path(command[4]).mkdir(parents=True, exist_ok=True)
            self.calls.append({"command": list(command), "cwd": working_directory})
            return _result(list(command))
        if command[0] == "git":
            return _result(list(command))
        if "-rfE" in command:
            self.calls.append({"command": list(command), "cwd": working_directory})
            on_base = str(working_directory).endswith("touched-base")
            failing = self.at_base if on_base else (
                command[3:-4] if self.again is None else self.again
            )
            return _result(
                list(command),
                exit_code=1 if failing else 0,
                stdout="".join(f"FAILED {node} - boom\n" for node in failing),
            )
        return super().__call__(
            command, working_directory=working_directory, timeout_seconds=timeout_seconds
        )


class BaselineFailureTests(_RunFixture):
    """pydata/xarray#10639 (#180): failures the base commit shares do not block."""

    nodes = ["tests/test_xbrl.py::test_engine", "tests/test_xbrl.py::test_groups"]

    def _run_with_base(self, executor: BaselineExecutor):
        with patch("mailman.target_checks.execute", executor):
            return self._run(executor, base_commit="abc123")

    def test_failures_that_also_fail_at_base_pass_the_stage(self) -> None:
        executor = BaselineExecutor(at_base=self.nodes)
        record = self._run_with_base(executor)
        self.assertEqual(record["baseline"]["failing_at_base"], self.nodes)
        self.assertEqual(record["baseline"]["new"], [])
        base_run = [c for c in executor.calls if "-rfE" in c["command"]][0]
        self.assertEqual(base_run["command"][3:5], self.nodes)
        self.assertTrue(str(base_run["cwd"]).endswith("touched-base"))
        code, detail = touched_tests_verdict(record)
        self.assertIsNone(code)
        self.assertIn("also fail at the base", detail)

    def test_a_failure_new_in_the_patch_still_blocks(self) -> None:
        executor = BaselineExecutor(at_base=self.nodes[1:])
        record = self._run_with_base(executor)
        code, detail = touched_tests_verdict(record)
        self.assertEqual(code, "touched-tests-failed")
        self.assertIn("New since the base commit: tests/test_xbrl.py::test_engine", detail)

    def test_a_failure_that_passes_at_base_and_on_rerun_is_flaky(self) -> None:
        # #183: pyinstaller#9121's onefile test failed once, passed at base
        # and passed on rerun with the patch.
        executor = BaselineExecutor(at_base=self.nodes[1:], again=[])
        record = self._run_with_base(executor)
        self.assertEqual(record["baseline"]["flaky"], self.nodes[:1])
        self.assertEqual(record["baseline"]["new"], [])
        rerun = [c for c in executor.calls if "-rfE" in c["command"]][-1]
        self.assertEqual(rerun["command"][3:4], self.nodes[:1])
        self.assertFalse(str(rerun["cwd"]).endswith("touched-base"))
        code, detail = touched_tests_verdict(record)
        self.assertIsNone(code)
        self.assertIn("flaky: tests/test_xbrl.py::test_engine", detail)
        self.assertIn("also fail at the base commit: tests/test_xbrl.py::test_groups", detail)

    def test_past_the_node_limit_the_same_files_run_at_base(self) -> None:
        # scverse/anndata#2348 (#202): 2249 nodes failed for want of awkward,
        # at the base commit too. Past the limit the files run, not the ids.
        many = [f"tests/test_xbrl.py::test_case_{index}" for index in range(60)]
        executor = BaselineExecutor(at_base=many)
        FakeExecutor.__init__(
            executor,
            exit_code=1,
            stdout="".join(f"FAILED {node} - no awkward\n" for node in many)
            + "60 failed, 5 passed in 0.7s\n",
        )
        record = self._run_with_base(executor)
        self.assertEqual(record["baseline"]["new"], [])
        base_run = [c for c in executor.calls if "-rfE" in c["command"]][0]
        self.assertNotIn(many[0], base_run["command"])
        self.assertIn("tests/test_xbrl.py", base_run["command"])
        code, detail = touched_tests_verdict(record)
        self.assertIsNone(code)
        self.assertIn("and 50 more", detail)

    def test_without_a_base_commit_nothing_is_compared(self) -> None:
        executor = BaselineExecutor(at_base=self.nodes)
        record = self._run(executor)
        self.assertIsNone(record["baseline"])
        self.assertEqual(touched_tests_verdict(record)[0], "touched-tests-failed")
