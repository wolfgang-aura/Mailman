"""draft-environment picks the newest interpreter that installs from wheels.

Run 20261001T154853Z-f96a27 (spikeinterface) drafted its plan on the host's
Python 3.14, where `numcodecs<0.16.0` has no wheel; the sdist build failed for
want of a compiler and the operator switched to uv's 3.12 by hand. Mailman #365.
"""
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from mailman.artifacts import create_run
from mailman.cli import main
from mailman.environment import load_plan
from mailman.environment_plan import (
    _source_builds,
    choose_interpreter,
    draft_plan,
    probe_interpreter,
)
from mailman.executor import CommandResult

SPIKEINTERFACE = '''
[project]
name = "spikeinterface"
requires-python = ">=3.10"
dependencies = [
  "numcodecs<0.16.0",
  "neo @ git+https://github.com/NeuralEnsemble/python-neo.git",
]
[project.optional-dependencies]
test = ["pytest", "spikeinterface[full]"]
full = ["scipy"]
[build-system]
requires = ["setuptools>=78"]
'''

PY314 = r"C:\Python\3.14\python.exe"
PY312 = r"C:\uv\cpython-3.12.14\python.exe"
PY310 = r"C:\Python\3.10\python.exe"


class _Probe:
    def __init__(self, answers: dict[str, dict]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, list[str], str]] = []

    def __call__(self, executable: str, requirements: list[str], version: str) -> dict:
        self.calls.append((executable, requirements, version))
        return {"seconds": 1, **self.answers[version]}


def _clean() -> dict:
    return {"ok": True, "source_builds": []}


def _needs(*names: str) -> dict:
    return {"ok": True, "source_builds": list(names)}


class DraftChoosesInterpreterTests(unittest.TestCase):
    def _draft(self, pyproject: str, candidates, probe):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        (root / "pyproject.toml").write_text(pyproject, encoding="utf-8")
        path = root / "run" / "environment-plan.json"
        draft_plan(root, path, python=PY314, candidates=candidates, probe=probe)
        return load_plan(path)

    def test_the_newest_interpreter_that_needs_a_source_build_is_passed_over(self) -> None:
        probe = _Probe({"3.14": _needs("numcodecs"), "3.12": _clean()})

        plan = self._draft(SPIKEINTERFACE, [("3.14", PY314), ("3.12", PY312)], probe)

        self.assertEqual(plan["steps"][0]["command"][0], PY312)
        self.assertEqual(plan["draft"]["interpreter"]["chosen"]["python"], "3.12")
        self.assertIn("Interpreter: Python 3.12", plan["draft"]["review"])
        self.assertIn("entirely from wheels", plan["draft"]["review"])
        self.assertEqual([call[2] for call in probe.calls], ["3.14", "3.12"])

    def test_probing_stops_at_the_first_interpreter_that_needs_no_source_build(self) -> None:
        probe = _Probe({"3.14": _clean()})

        plan = self._draft(SPIKEINTERFACE, [("3.14", PY314), ("3.12", PY312)], probe)

        self.assertEqual(plan["steps"][0]["command"][0], PY314)
        self.assertEqual(len(probe.calls), 1)

    def test_when_every_interpreter_needs_a_source_build_the_fewest_wins(self) -> None:
        probe = _Probe({
            "3.14": _needs("langdetect", "numcodecs"),
            "3.12": _needs("langdetect"),
            "3.10": _needs("langdetect"),
        })

        plan = self._draft(
            SPIKEINTERFACE, [("3.14", PY314), ("3.12", PY312), ("3.10", PY310)], probe
        )

        # 3.12 and 3.10 tie on one pure-Python sdist; the newer one wins.
        self.assertEqual(plan["steps"][0]["command"][0], PY312)
        self.assertIn("fewest source builds (langdetect)", plan["draft"]["review"])

    def test_when_no_interpreter_resolves_the_default_is_kept_and_said(self) -> None:
        failed = {"ok": False, "detail": "exit 1: no network"}
        probe = _Probe({"3.14": failed, "3.12": failed})

        plan = self._draft(SPIKEINTERFACE, [("3.14", PY314), ("3.12", PY312)], probe)

        self.assertEqual(plan["steps"][0]["command"][0], PY314)
        self.assertIsNone(plan["draft"]["interpreter"]["chosen"])
        self.assertIn("no installed interpreter", plan["draft"]["review"])
        self.assertEqual(len(plan["draft"]["interpreter"]["probes"]), 2)

    def test_an_interpreter_requires_python_excludes_is_not_probed(self) -> None:
        pyproject = SPIKEINTERFACE.replace('">=3.10"', '">=3.10,<3.14"')
        probe = _Probe({"3.12": _clean()})

        plan = self._draft(pyproject, [("3.14", PY314), ("3.12", PY312)], probe)

        self.assertEqual([call[2] for call in probe.calls], ["3.12"])
        self.assertEqual(plan["steps"][0]["command"][0], PY312)

    def test_the_probe_resolves_runtime_and_test_requirements_but_not_source_only_ones(self) -> None:
        probe = _Probe({"3.14": _clean()})

        self._draft(SPIKEINTERFACE, [("3.14", PY314)], probe)

        requirements = probe.calls[0][1]
        self.assertIn("numcodecs<0.16.0", requirements)
        self.assertIn("pytest", requirements)
        self.assertIn("setuptools>=78", requirements)
        # A git reference builds from source everywhere, and the target's own
        # extras are not on PyPI as this checkout.
        self.assertFalse(any("neo" in entry for entry in requirements))
        self.assertFalse(any(entry.startswith("spikeinterface") for entry in requirements))

    def test_without_candidates_the_plan_keeps_the_interpreter_it_was_given(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / "pyproject.toml").write_text(SPIKEINTERFACE, encoding="utf-8")
            plan = draft_plan(root, root / "plan.json", python=PY314)

        self.assertEqual(plan["steps"][0]["command"][0], PY314)
        self.assertNotIn("interpreter", plan["draft"])


def _report_entry(name: str, url: str, **extra) -> dict:
    return {"metadata": {"name": name}, "download_info": {"url": url, **extra}}


class ProbeTests(unittest.TestCase):
    def test_an_sdist_is_a_source_build_and_a_wheel_or_git_reference_is_not(self) -> None:
        report = {"install": [
            _report_entry("numcodecs", "https://files/numcodecs-0.15.1.tar.gz",
                          archive_info={}),
            _report_entry("numpy", "https://files/numpy-2.5.3-cp314-cp314-win_amd64.whl",
                          archive_info={}),
            _report_entry("neo", "https://github.com/NeuralEnsemble/python-neo.git",
                          vcs_info={"vcs": "git"}),
        ]}

        self.assertEqual(_source_builds(report), ["numcodecs"])

    def test_the_host_pip_resolves_inside_the_candidate_without_installing(self) -> None:
        seen: list[list[str]] = []

        def run(command, *, working_directory, timeout_seconds):
            seen.append(list(command))
            report = Path(command[command.index("--report") + 1])
            report.write_text(json.dumps({"install": [
                _report_entry("numcodecs", "https://files/numcodecs-0.15.1.tar.gz"),
            ]}), encoding="utf-8")
            return CommandResult(list(command), str(working_directory), "", 3.2, 0, "", "",
                                 False, timeout_seconds, {})

        with tempfile.TemporaryDirectory() as name:
            result = probe_interpreter(
                PY312, ["numcodecs<0.16.0"], constraints=["-c", "host.txt"],
                report=Path(name) / "probes" / "python-3.12.json", run=run,
            )

        self.assertEqual(result, {"ok": True, "seconds": 3, "source_builds": ["numcodecs"]})
        command = seen[0]
        self.assertEqual(command[command.index("--python") + 1], PY312)
        self.assertIn("--dry-run", command)
        self.assertIn("--ignore-installed", command)
        self.assertEqual(command[-3:], ["--report", command[-2], "numcodecs<0.16.0"])
        self.assertIn("host.txt", command)

    def test_a_failed_resolution_says_why(self) -> None:
        def run(command, *, working_directory, timeout_seconds):
            return CommandResult(list(command), str(working_directory), "", 9.0, 1, "",
                                 "ERROR: No matching distribution found for numcodecs<0.16.0\n",
                                 False, timeout_seconds, {})

        with tempfile.TemporaryDirectory() as name:
            result = probe_interpreter(
                PY312, ["numcodecs<0.16.0"], constraints=[],
                report=Path(name) / "python-3.12.json", run=run,
            )

        self.assertFalse(result["ok"])
        self.assertIn("exit 1", result["detail"])
        self.assertIn("No matching distribution", result["detail"])

    def test_every_probe_is_announced(self) -> None:
        said: list[str] = []
        probe = _Probe({"3.14": _needs("numcodecs"), "3.12": _clean()})

        choose_interpreter([("3.14", PY314), ("3.12", PY312)], ["numcodecs"],
                           probe=probe, announce=said.append)

        self.assertEqual(said, ["python 3.14: 1 source build(s) (numcodecs) in 1s",
                                "python 3.12: 0 source build(s) in 1s"])


class DraftEnvironmentCommandTests(unittest.TestCase):
    def _run(self, *extra: str, installed: dict) -> tuple[int, mock.MagicMock, str]:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        data_root = Path(directory.name) / "runs"
        run, run_directory = create_run(
            repository="https://github.com/example/project.git",
            issue="https://github.com/example/project/issues/7",
            base_commit="a" * 40,
            primary="codex",
            reviewer="claude",
            data_root=data_root,
        )
        workspace = run_directory / "workspace"
        workspace.mkdir()
        (workspace / "pyproject.toml").write_text(SPIKEINTERFACE, encoding="utf-8")
        stderr = StringIO()
        with mock.patch("mailman.baseline.host_interpreters", return_value=installed), \
                mock.patch("mailman.environment_plan.draft_plan") as drafted, \
                redirect_stdout(StringIO()), redirect_stderr(stderr):
            code = main(["draft-environment", run.run_id, "--data-root", str(data_root), *extra])
        return code, drafted, stderr.getvalue()

    def test_installed_interpreters_in_the_requires_python_range_are_offered_newest_first(self) -> None:
        code, drafted, _ = self._run(installed={(3, 14): PY314, (3, 12): PY312, (3, 9): "old"})

        self.assertEqual(code, 0)
        self.assertEqual(drafted.call_args.kwargs["candidates"],
                         [("3.14", PY314), ("3.12", PY312)])

    def test_an_explicit_python_skips_the_probes(self) -> None:
        code, drafted, _ = self._run("--python", PY310, installed={(3, 14): PY314})

        self.assertEqual(code, 0)
        self.assertIsNone(drafted.call_args.kwargs["candidates"])
        self.assertEqual(drafted.call_args.kwargs["python"], PY310)

    def test_no_installed_candidate_keeps_the_default_without_probing(self) -> None:
        code, drafted, _ = self._run(installed={})

        self.assertEqual(code, 0)
        self.assertIsNone(drafted.call_args.kwargs["candidates"])


if __name__ == "__main__":
    unittest.main()
