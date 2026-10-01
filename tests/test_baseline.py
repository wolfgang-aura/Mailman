"""`mailman baseline`: the target's own suite at its head, before any change.

On 2026-09-06 the install failed on Python 3.14 and worked on 3.13, and all 17
suite failures were one already-reported issue. Each of those was a hand step.
Mailman #54. Every command and GitHub call here is faked.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from mailman import baseline
from mailman.executor import CommandResult

COMMIT = "c" * 40


def _result(command: list[str], exit_code: int = 0, stdout: str = "", stderr: str = "",
            timed_out: bool = False) -> CommandResult:
    return CommandResult(
        command=list(command), working_directory=".", started_at="", duration_seconds=0.0,
        exit_code=exit_code, stdout=stdout, stderr=stderr, timed_out=timed_out,
        timeout_seconds=60, environment={},
    )


LAUNCHER = """\
 -V:3.15 *        C:\\Python315\\python.exe
 -V:3.14t         C:\\Python314\\python3.14t.exe
 -V:3.14          C:\\Python314\\python.exe
 -V:Astral\\CPython3.12.14 C:\\uv\\cpython-3.12\\python.exe
 -V:3.13          C:\\Python313\\python.exe
 -V:3.13          C:\\Other\\python.exe
 -V:ContinuumAnalytics/Anaconda39-64 C:\\conda\\envs
"""

WORKFLOW = """\
jobs:
  test:
    strategy:
      matrix:
        python-version:
          - "3.12"
          - "3.14"
          - "pypy3.10"
    steps:
      - uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.python-version }}
  lint:
    steps:
      - uses: actions/setup-python@v5
        with:
          python-version: "3.13"
"""


class InterpreterTests(unittest.TestCase):
    def test_workflow_versions_are_read_and_pypy_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            folder = workspace / ".github" / "workflows"
            folder.mkdir(parents=True)
            (folder / "ci.yml").write_text(WORKFLOW, encoding="utf-8")
            (folder / "docs.yaml").write_text("python-version: ['3.11', '3.10']\n", encoding="utf-8")
            (folder / "other.yml").write_text("runs-on: ubuntu-latest\n", encoding="utf-8")
            found = baseline.ci_python_versions(workspace)
        self.assertEqual(found["versions"], ["3.10", "3.11", "3.12", "3.13", "3.14"])
        self.assertEqual(found["sources"], [".github/workflows/ci.yml", ".github/workflows/docs.yaml"])

    def test_the_requires_python_floor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            self.assertIsNone(baseline.requires_python_floor(workspace))
            (workspace / "pyproject.toml").write_text(
                '[project]\nname = "x"\nrequires-python = ">=3.10,<4"\n', encoding="utf-8"
            )
            self.assertEqual(baseline.requires_python_floor(workspace), (3, 10))

    def test_the_launcher_listing_keeps_the_first_gil_build_per_version(self) -> None:
        self.assertEqual(
            baseline.parse_launcher_listing(LAUNCHER),
            {
                (3, 15): "C:\\Python315\\python.exe",
                (3, 14): "C:\\Python314\\python.exe",
                (3, 12): "C:\\uv\\cpython-3.12\\python.exe",
                (3, 13): "C:\\Python313\\python.exe",
            },
        )

    def test_candidates_start_at_the_ci_interpreter_and_step_down(self) -> None:
        available = {(3, 13): "a", (3, 12): "b", (3, 9): "c"}
        self.assertEqual(
            baseline.candidate_versions(["3.11", "3.14"], None, available),
            [(3, 14), (3, 13), (3, 12)],
        )
        self.assertEqual(
            baseline.candidate_versions([], (3, 10), available), [(3, 13), (3, 12)]
        )
        self.assertEqual(baseline.candidate_versions([], None, {}), [])


def _plan(workspace: Path, destination: Path, *, python: str) -> dict[str, Any]:
    plan = {"schema_version": 1, "steps": [{"name": "venv", "command": [python, "-m", "venv", "environment"]}]}
    destination.write_text(json.dumps(plan), encoding="utf-8")
    return plan


class _Prepare:
    """3.14 fails for want of a wheel; anything else installs."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, attempt: Path, **_: Any) -> dict[str, Any]:
        self.calls.append(attempt.name)
        if attempt.name == "python-3.14":
            record = {"success": False, "steps": [{
                "name": "install", "ok": False,
                "command": {"exit_code": 1, "timed_out": False, "stdout": "",
                            "stderr": "building aiohttp\nerror: no cp314 wheel for aiohttp"},
            }]}
        else:
            python = attempt / "environment" / "Scripts" / "python.exe"
            python.parent.mkdir(parents=True)
            python.write_text("", encoding="utf-8")
            record = {"success": True, "steps": []}
        (attempt / "environment.json").write_text(json.dumps(record), encoding="utf-8")
        return record


class BuildTests(unittest.TestCase):
    INTERPRETERS = {(3, 14): "C:\\Python314\\python.exe", (3, 13): "C:\\Python313\\python.exe"}
    CI = {"versions": ["3.12", "3.15"], "sources": [".github/workflows/ci.yml"]}

    def test_the_step_down_records_why_the_ci_interpreter_was_not_used(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepare = _Prepare()
            built = baseline.build_environment(
                root, root / "workspace", ci=self.CI, floor=None,
                interpreters=self.INTERPRETERS, prepare=prepare, draft=_plan,
            )
            self.assertEqual(prepare.calls, ["python-3.14", "python-3.13"])
            self.assertEqual([row["python"] for row in built["attempts"]], ["3.15", "3.14", "3.13"])
            self.assertEqual(built["ci_interpreter"], "3.15")
            self.assertEqual(built["used"], "3.13")
            self.assertTrue(built["differs_from_ci"])
            self.assertIn("Python 3.15: not installed on this host", built["reason"])
            self.assertIn("Python 3.14: step install failed (exit 1): building aiohttp / "
                          "error: no cp314 wheel for aiohttp", built["reason"])
            self.assertTrue(built["executable"].endswith("python.exe"))

            again = _Prepare()
            rebuilt = baseline.build_environment(
                root, root / "workspace", ci=self.CI, floor=None,
                interpreters=self.INTERPRETERS, prepare=again, draft=_plan,
            )
            self.assertEqual(again.calls, ["python-3.14"])
            self.assertTrue(rebuilt["attempts"][-1]["reused"])

    def test_no_interpreter_that_builds_is_a_reason_not_a_crash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            built = baseline.build_environment(
                root, root / "workspace", ci={"versions": ["3.14"], "sources": []}, floor=None,
                interpreters={(3, 14): "C:\\Python314\\python.exe"}, prepare=_Prepare(), draft=_plan,
            )
        self.assertIsNone(built["executable"])
        self.assertTrue(built["reason"].startswith("no interpreter built the environment"))


SUITE_OUTPUT = """\
....F..E
=========================== short test summary info ===========================
FAILED tests/test_paths.py::test_symlink_copy[posix] - OSError: [WinError 1314] A required privilege is not held
FAILED tests/test_paths.py::test_symlink_copy[nt] - OSError: [WinError 1314]
ERROR tests/test_net.py::TestClient::test_timeout
FAILED tests/test_parse.py::test_unicode - AssertionError
4 failed, 8398 passed, 12 skipped, 1 error in 412.00s
"""


class SuiteTests(unittest.TestCase):
    def test_failures_come_from_the_short_summary(self) -> None:
        failures = baseline.suite_failures(SUITE_OUTPUT)
        self.assertEqual(
            [(row["nodeid"], row["kind"]) for row in failures],
            [("tests/test_paths.py::test_symlink_copy[posix]", "failed"),
             ("tests/test_paths.py::test_symlink_copy[nt]", "failed"),
             ("tests/test_net.py::TestClient::test_timeout", "error"),
             ("tests/test_parse.py::test_unicode", "failed")],
        )
        self.assertEqual(baseline.test_name(failures[0]["nodeid"]), "test_symlink_copy")
        self.assertEqual(baseline.test_name("tests/test_import.py"), "test_import.py")

    def test_the_suite_runs_once_more_with_a_cache_when_cache_dir_is_strict(self) -> None:
        commands: list[list[str]] = []

        def run(command: list[str], **_: Any) -> CommandResult:
            commands.append(command)
            if len(commands) == 1:
                return _result(command, 4, stderr="ERROR: Unknown config option: cache_dir")
            return _result(command, 1, stdout=SUITE_OUTPUT)

        with tempfile.TemporaryDirectory() as directory:
            ran = baseline.run_suite(Path(directory), Path(directory), "py.exe", run=run)
            self.assertTrue((Path(directory) / "suite-output.txt").is_file())
        self.assertEqual(len(commands), 2)
        self.assertIn("no:cacheprovider", commands[0])
        self.assertNotIn("no:cacheprovider", commands[1])
        self.assertTrue(ran["completed"])
        self.assertEqual(ran["counts"]["passed"], 8398)
        self.assertEqual(len(ran["failures"]), 4)

    def test_a_timed_out_suite_is_not_a_baseline(self) -> None:
        def run(command: list[str], **_: Any) -> CommandResult:
            return _result(command, None, stdout="....", timed_out=True)

        with tempfile.TemporaryDirectory() as directory:
            ran = baseline.run_suite(Path(directory), Path(directory), "py.exe", run=run)
        self.assertFalse(ran["completed"])


class _FakeGh:
    def __init__(self, *, search: dict[str, list[dict[str, Any]]] | None,
                 listing: list[dict[str, Any]] | None) -> None:
        self.search = search
        self.listing = listing
        self.paths: list[str] = []
        self.failures: list[str] = []
        self.rate_limited = False

    def json(self, path: str) -> Any:
        self.paths.append(path)
        if self.search is None:
            self.failures.append(path)
            return None
        query = unquote(path)
        for name, rows in self.search.items():
            if f'"{name}"' in query:
                return {"items": rows}
        return {"items": []}

    def every_page(self, path: str, *, pages: int) -> Any:
        self.paths.append(path)
        if self.listing is None:
            self.failures.append(path)
        return self.listing


SYMLINK_ISSUE = {
    "number": 17, "title": "test_symlink_copy fails on Windows",
    "body": "FAILED tests/test_paths.py::test_symlink_copy[nt]", "html_url": "https://x/17",
}
LOOSE_HIT = {"number": 9, "title": "test_unicode_names is slow", "body": "", "html_url": "https://x/9"}


def _failures() -> list[dict[str, Any]]:
    return baseline.suite_failures(SUITE_OUTPUT)


class MatchTests(unittest.TestCase):
    def test_a_search_hit_that_names_the_test_is_known(self) -> None:
        failures = _failures()
        gh = _FakeGh(search={"test_symlink_copy": [SYMLINK_ISSUE], "test_unicode": [LOOSE_HIT]},
                     listing=None)
        lookup = baseline.match_failures(failures, "owner/repo", gh)
        self.assertEqual([row["status"] for row in failures],
                         [baseline.KNOWN, baseline.KNOWN, baseline.UNEXPLAINED, baseline.UNEXPLAINED])
        self.assertEqual(failures[0]["issues"][0]["number"], 17)
        self.assertFalse(failures[0]["issues"][0]["names_nodeid"])
        self.assertTrue(failures[1]["issues"][0]["names_nodeid"])
        self.assertEqual(lookup["searched"], ["test_symlink_copy", "test_timeout", "test_unicode"])
        self.assertTrue(all(path.startswith("search/issues?") for path in gh.paths))
        self.assertIn("repo:owner/repo is:issue is:open", unquote(gh.paths[0]))

    def test_a_failed_search_falls_back_to_the_open_issue_listing(self) -> None:
        failures = _failures()
        pull = dict(SYMLINK_ISSUE, number=18, pull_request={})
        gh = _FakeGh(search=None, listing=[SYMLINK_ISSUE, pull])
        lookup = baseline.match_failures(failures, "owner/repo", gh)
        self.assertEqual(failures[0]["status"], baseline.KNOWN)
        self.assertEqual([row["number"] for row in failures[0]["issues"]], [17])
        self.assertEqual(failures[0]["issues"][0]["found_by"], "listing")
        self.assertEqual(failures[3]["status"], baseline.UNEXPLAINED)
        self.assertEqual(lookup["listed_issues"], 1)

    def test_when_neither_lookup_works_a_failure_is_unchecked(self) -> None:
        failures = _failures()
        lookup = baseline.match_failures(failures, "owner/repo", _FakeGh(search=None, listing=None))
        self.assertEqual({row["status"] for row in failures}, {baseline.UNCHECKED})
        self.assertTrue(lookup["unread"])


class HeadTests(unittest.TestCase):
    def test_the_head_is_read_from_ls_remote(self) -> None:
        def run(command: list[str], **_: Any) -> CommandResult:
            return _result(command, stdout=f"{COMMIT}\tHEAD\n")

        self.assertEqual(baseline.resolve_head("u", working_directory=Path("."), run=run), COMMIT)

    def test_an_unreadable_head_is_an_error(self) -> None:
        def run(command: list[str], **_: Any) -> CommandResult:
            return _result(command, 128, stderr="fatal: repository not found")

        with self.assertRaises(ValueError) as raised:
            baseline.resolve_head("u", working_directory=Path("."), run=run)
        self.assertIn("repository not found", str(raised.exception))


class RecordTests(unittest.TestCase):
    def _record(self, root: Path, *, suite_output: str, timed_out: bool = False) -> dict[str, Any]:
        gh_calls: list[list[str]] = []

        def run(command: list[str], **_: Any) -> CommandResult:
            if command[:2] == ["git", "ls-remote"]:
                return _result(command, stdout=f"{COMMIT}\tHEAD\n")
            if command[:2] == ["gh", "api"]:
                gh_calls.append(command)
                path = unquote(command[2])
                rows = [SYMLINK_ISSUE] if "test_symlink_copy" in path else []
                return _result(command, stdout=json.dumps({"items": rows}))
            raise AssertionError(f"unexpected command {command}")

        def clone(*, repository: str, base_commit: str, run_directory: Path, **_: Any) -> dict[str, Any]:
            self.assertEqual(repository, "https://github.com/owner/repo.git")
            self.assertEqual(base_commit, COMMIT)
            (run_directory / "workspace").mkdir()
            return {"success": True}

        def build(directory: Path, workspace: Path, **_: Any) -> dict[str, Any]:
            return {"used": "3.13", "executable": "C:\\env\\python.exe", "differs_from_ci": True,
                    "reason": "Python 3.14: no wheel"}

        def suite(directory: Path, workspace: Path, python: str, **_: Any) -> dict[str, Any]:
            def fake(command: list[str], **__: Any) -> CommandResult:
                return _result(command, None if timed_out else 1, stdout=suite_output, timed_out=timed_out)
            return baseline.run_suite(directory, workspace, python, run=fake)

        record = baseline.record_baseline(
            "owner/repo", root=root / "runs", run=run, clone=clone, interpreters={},
            build=build, suite=suite,
        )
        record["gh_calls"] = gh_calls
        return record

    def test_a_baseline_is_written_with_counts_and_known_failures(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record = self._record(root, suite_output=SUITE_OUTPUT)
            written = baseline.load_baseline(Path(record["path"]).parent)
            expected = root.resolve() / "baselines" / f"owner-repo-{COMMIT[:12]}" / "baseline.json"
            self.assertEqual(Path(record["path"]), expected)
        self.assertTrue(record["success"])
        self.assertEqual(record["base_commit"], COMMIT)
        self.assertEqual(record["base_commit_source"], "default-branch head (git ls-remote HEAD)")
        self.assertEqual((record["passed"], record["failed"], record["errors"], record["skipped"]),
                         (8398, 4, 1, 12))
        self.assertEqual((record["known"], record["unexplained"], record["unchecked"]), (2, 2, 0))
        self.assertEqual(written["interpreter"]["used"], "3.13")
        self.assertTrue(all(call[2].startswith("search/issues?") for call in record["gh_calls"]))

    def test_a_suite_that_did_not_finish_is_recorded_as_a_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            record = self._record(Path(directory), suite_output="....", timed_out=True)
        self.assertFalse(record["success"])
        self.assertIn("timed out", record["detail"])
        self.assertEqual(record["gh_calls"], [])

    def test_a_malformed_repository_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                baseline.record_baseline("not a repo", root=Path(directory))


if __name__ == "__main__":
    unittest.main()
