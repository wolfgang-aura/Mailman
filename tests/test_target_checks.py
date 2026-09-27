"""Target CI checks run before filing: the offline audit (#137) and lint (#120)."""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from mailman.executor import CommandResult
from mailman.target_checks import run_offline_audit


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


if __name__ == "__main__":
    unittest.main()
