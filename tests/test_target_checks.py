"""Target CI checks run before filing: the offline audit (#137) and lint (#120)."""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from mailman.executor import CommandResult
from mailman.target_checks import ruff_configuration, run_lint, run_offline_audit


def _result(command: list[str], *, exit_code: int = 0, stdout: str = "",
            timed_out: bool = False) -> CommandResult:
    return CommandResult(
        command=command,
        working_directory=".",
        started_at="2026-09-28T00:00:00+00:00",
        duration_seconds=0.2,
        exit_code=exit_code,
        stdout=stdout,
        stderr="",
        timed_out=timed_out,
        timeout_seconds=600,
        environment={},
    )


class Executor:
    """Answers by the first matching fragment of the joined command."""

    def __init__(self, answers: dict[str, int] | None = None) -> None:
        self.answers = answers or {}
        self.calls: list[list[str]] = []

    def __call__(self, command, *, working_directory, timeout_seconds, **_):
        self.calls.append(list(command))
        joined = " ".join(command)
        for fragment, exit_code in self.answers.items():
            if fragment in joined:
                return _result(list(command), exit_code=exit_code, stdout="output\n")
        return _result(list(command))


class _Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = TemporaryDirectory()
        root = Path(self._temporary.name)
        self.run_directory = root / "run"
        self.workspace = root / "workspace"
        (self.run_directory / "environment" / "Scripts").mkdir(parents=True)
        self.python = self.run_directory / "environment" / "Scripts" / "python.exe"
        self.python.write_bytes(b"")
        (self.workspace / "pkg").mkdir(parents=True)
        (self.workspace / "tests").mkdir()
        (self.workspace / "pkg" / "mod.py").write_text("x = 1\n", encoding="utf-8")
        (self.workspace / "tests" / "test_mod.py").write_text(
            "from pkg.mod import x\n", encoding="utf-8"
        )

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def write(self, relative: str, text: str) -> None:
        path = self.workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


class OfflineAuditTests(_Fixture):
    def _run(self, executor: Executor, changed=("pkg/mod.py", "tests/test_mod.py")):
        with patch("mailman.target_checks.execute", executor):
            return run_offline_audit(
                self.run_directory, workspace=self.workspace, changed_paths=list(changed)
            )

    def test_a_target_without_the_script_is_not_audited(self) -> None:
        executor = Executor()
        record, findings = self._run(executor)
        self.assertEqual(record["reason"], "not-shipped")
        self.assertEqual(findings, [])
        self.assertEqual(executor.calls, [])

    def test_the_script_runs_on_the_changed_test_files_only(self) -> None:
        self.write("scripts/check_offline_audit.py", "print('OK')\n")
        executor = Executor()
        record, findings = self._run(executor)
        self.assertEqual(findings, [])
        self.assertTrue(record["ran"])
        self.assertEqual(
            executor.calls,
            [[str(self.python), "scripts/check_offline_audit.py", "tests/test_mod.py"]],
        )

    def test_a_non_zero_audit_blocks(self) -> None:
        # edgartools#1365: the audit exited 2 on a new offline test in a
        # network-classed file, and CI's test-fast failed on it.
        self.write("scripts/check_offline_audit.py", "raise SystemExit(2)\n")
        record, findings = self._run(Executor({"check_offline_audit": 2}))
        self.assertEqual(record["exit_code"], 2)
        self.assertEqual([f["code"] for f in findings], ["offline-audit-failed"])
        self.assertTrue(findings[0]["blocking"])
        self.assertIn("tests/test_mod.py", findings[0]["detail"])

    def test_no_changed_test_file_means_nothing_to_audit(self) -> None:
        self.write("scripts/check_offline_audit.py", "print('OK')\n")
        executor = Executor()
        record, findings = self._run(executor, changed=("pkg/mod.py",))
        self.assertEqual(record["reason"], "no-changed-test-files")
        self.assertEqual(findings, [])
        self.assertEqual(executor.calls, [])

    def test_a_missing_environment_blocks_rather_than_passing(self) -> None:
        self.write("scripts/check_offline_audit.py", "print('OK')\n")
        self.python.unlink()
        _, findings = self._run(Executor())
        self.assertEqual([f["code"] for f in findings], ["offline-audit-not-run"])
        self.assertTrue(findings[0]["blocking"])


class RuffConfigurationTests(_Fixture):
    def test_no_ruff_anywhere_is_no_configuration(self) -> None:
        self.write("pyproject.toml", "[project]\nname = 'x'\n")
        self.assertIsNone(ruff_configuration(self.workspace))

    def test_pyproject_tool_ruff_and_a_workflow_pin_are_found(self) -> None:
        # pypdf: pyproject configures ruff and CI runs a pinned `ruff check .`.
        self.write("pyproject.toml", "[tool.ruff]\nline-length = 120\n")
        self.write(
            ".github/workflows/ci.yml",
            "steps:\n  - run: pip install ruff==0.16.0\n  - run: ruff check .\n",
        )
        configuration = ruff_configuration(self.workspace)
        self.assertEqual(
            configuration["sources"], ["pyproject.toml", ".github/workflows/ci.yml"]
        )
        self.assertEqual(configuration["version"], "0.16.0")
        self.assertFalse(configuration["format"])

    def test_a_pre_commit_rev_pins_the_version_and_ruff_format_is_seen(self) -> None:
        self.write(
            ".pre-commit-config.yaml",
            "repos:\n  - repo: https://github.com/astral-sh/ruff-pre-commit\n"
            "    rev: v0.6.9\n    hooks:\n      - id: ruff\n      - id: ruff-format\n",
        )
        configuration = ruff_configuration(self.workspace)
        self.assertEqual(configuration["version"], "0.6.9")
        self.assertTrue(configuration["format"])


class LintTests(_Fixture):
    def setUp(self) -> None:
        super().setUp()
        self.write("pyproject.toml", "[tool.ruff]\n")
        self.write(".github/workflows/ci.yml", "- run: pip install ruff==0.16.0\n")

    def _run(self, executor: Executor, changed=("pkg/mod.py", "README.md")):
        with patch("mailman.target_checks.execute", executor):
            return run_lint(
                self.run_directory, workspace=self.workspace, changed_paths=list(changed)
            )

    def test_ruff_runs_over_the_changed_python_files_and_passes(self) -> None:
        executor = Executor()
        record, findings = self._run(executor)
        self.assertEqual(findings, [])
        self.assertTrue(record["ran"])
        self.assertEqual(record["reason"], "passed")
        self.assertIsNone(record["install"])
        self.assertEqual(
            executor.calls[-1],
            [str(self.python), "-m", "ruff", "check", "--no-cache", "--force-exclude",
             "pkg/mod.py"],
        )

    def test_a_ruff_finding_blocks(self) -> None:
        # pypdf#4105: B008 in a test helper, caught by CI 22 seconds after filing.
        record, findings = self._run(Executor({"ruff check": 1}))
        self.assertEqual(record["reason"], "failed")
        self.assertEqual([f["code"] for f in findings], ["lint-failed"])
        self.assertTrue(findings[0]["blocking"])

    def test_a_missing_ruff_is_installed_at_the_pinned_version(self) -> None:
        executor = Executor({"ruff --version": 1})
        record, findings = self._run(executor)
        self.assertEqual(findings, [])
        self.assertEqual(record["install"]["requirement"], "ruff==0.16.0")
        self.assertIn("ruff==0.16.0", executor.calls[1])
        self.assertTrue(record["ran"])

    def test_a_ruff_that_cannot_be_installed_is_recorded_as_skipped(self) -> None:
        executor = Executor({"ruff --version": 1, "pip install": 1})
        record, findings = self._run(executor)
        self.assertFalse(record["ran"])
        self.assertTrue(record["reason"].startswith("skipped:"))
        self.assertEqual([f["code"] for f in findings], ["lint-skipped"])
        self.assertFalse(findings[0]["blocking"])
        self.assertFalse(any("ruff check" in " ".join(c) for c in executor.calls))

    def test_a_diff_without_python_files_is_not_linted(self) -> None:
        executor = Executor()
        record, findings = self._run(executor, changed=("README.md",))
        self.assertEqual(record["reason"], "no-changed-python-files")
        self.assertEqual(executor.calls, [])


if __name__ == "__main__":
    unittest.main()
