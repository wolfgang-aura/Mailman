"""Target CI checks run before filing: the offline audit (#137) and lint (#120)."""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from mailman.executor import CommandResult
from mailman.target_checks import (
    lint_configurations,
    load_lint_acknowledgement,
    record_lint_acknowledgement,
    ruff_configuration,
    run_lint,
    run_offline_audit,
)


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
    """Answers by the first matching fragment of the joined command.

    A list of exit codes is consumed one call at a time; the last one repeats.
    """

    def __init__(self, answers: dict[str, int | list[int]] | None = None) -> None:
        self.answers = answers or {}
        self.calls: list[list[str]] = []

    def __call__(self, command, *, working_directory, timeout_seconds, **_):
        self.calls.append(list(command))
        joined = " ".join(command)
        for fragment, exit_code in self.answers.items():
            if fragment in joined:
                if isinstance(exit_code, list):
                    exit_code = exit_code.pop(0) if len(exit_code) > 1 else exit_code[0]
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
        self.assertEqual([tool["tool"] for tool in record["tools"]], ["ruff"])
        self.assertIsNone(record["tools"][0]["install"])
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
        executor = Executor({"ruff --version": [1, 0]})
        record, findings = self._run(executor)
        self.assertEqual(findings, [])
        self.assertEqual(record["tools"][0]["install"]["requirement"], "ruff==0.16.0")
        self.assertIn("ruff==0.16.0", executor.calls[1])
        self.assertTrue(record["ran"])

    def test_a_ruff_that_cannot_be_installed_blocks_as_not_run(self) -> None:
        executor = Executor({"ruff --version": 1, "pip install": 1})
        record, findings = self._run(executor)
        self.assertFalse(record["ran"])
        self.assertEqual(record["reason"], "not-run")
        self.assertEqual([f["code"] for f in findings], ["lint-not-run"])
        self.assertTrue(findings[0]["blocking"])
        self.assertIn("ruff", findings[0]["detail"])
        self.assertFalse(any("ruff check" in " ".join(c) for c in executor.calls))

    def test_a_missing_environment_blocks_as_not_run(self) -> None:
        self.python.unlink()
        executor = Executor()
        _, findings = self._run(executor)
        self.assertEqual([f["code"] for f in findings], ["lint-not-run"])
        self.assertTrue(findings[0]["blocking"])
        self.assertEqual(executor.calls, [])

    def test_a_diff_without_python_files_is_not_linted(self) -> None:
        executor = Executor()
        record, findings = self._run(executor, changed=("README.md",))
        self.assertEqual(record["reason"], "no-changed-python-files")
        self.assertEqual(executor.calls, [])


class LintConfigurationTests(_Fixture):
    def _tools(self) -> list[str]:
        return [entry["tool"] for entry in lint_configurations(self.workspace)]

    def test_a_target_with_no_linter_has_no_configuration_and_no_finding(self) -> None:
        self.write("pyproject.toml", "[project]\nname = 'x'\ndependencies = ['mypy']\n")
        self.write(".github/workflows/ci.yml", "steps:\n  - run: pytest\n")
        self.assertEqual(self._tools(), [])
        executor = Executor()
        with patch("mailman.target_checks.execute", executor):
            record, findings = run_lint(
                self.run_directory, workspace=self.workspace, changed_paths=["pkg/mod.py"]
            )
        self.assertEqual(record["reason"], "no-linter-configured")
        self.assertEqual(findings, [])
        self.assertEqual(executor.calls, [])

    def test_each_tool_is_found_where_ci_or_configuration_names_it(self) -> None:
        self.write("setup.cfg", "[flake8]\nmax-line-length = 100\n")
        self.write("pyproject.toml", "[tool.black]\nline-length = 100\n[tool.mypy]\n")
        self.write(
            ".pre-commit-config.yaml",
            "repos:\n  - repo: https://github.com/PyCQA/isort\n    rev: 5.13.2\n"
            "    hooks:\n      - id: isort\n",
        )
        self.write(".github/workflows/lint.yml", "steps:\n  - run: uv run ty check src\n")
        self.assertEqual(self._tools(), ["flake8", "black", "isort", "mypy", "ty"])
        isort = lint_configurations(self.workspace)[2]
        self.assertEqual(isort["version"], "5.13.2")
        self.assertEqual(isort["sources"], [".pre-commit-config.yaml"])

    def test_a_hook_dependency_is_not_an_invocation(self) -> None:
        # nilearn (#144): blacken-docs lists black under
        # additional_dependencies; nilearn formats with ruff, not black.
        self.write(
            ".pre-commit-config.yaml",
            "repos:\n-   repo: https://github.com/adamchainz/blacken-docs\n"
            "    rev: 1.20.0\n    hooks:\n    -   id: blacken-docs\n"
            "        additional_dependencies:\n        -   black\n"
            "        # a comment\n        exclude: doc/\n"
            "-   repo: https://github.com/pre-commit/mirrors-mypy\n"
            "    rev: v1.0.0\n    hooks:\n    -   id: mypy\n"
            "        additional_dependencies: [black==24.1.0, types-requests]\n",
        )
        self.assertEqual(self._tools(), ["mypy"])

    def test_the_black_hook_itself_is_still_found(self) -> None:
        self.write(
            ".pre-commit-config.yaml",
            "repos:\n  - repo: https://github.com/psf/black-pre-commit-mirror\n"
            "    rev: 24.1.0\n    hooks:\n      - id: black\n",
        )
        self.assertEqual(self._tools(), ["black"])

    def test_ty_is_not_found_in_unrelated_prose(self) -> None:
        self.write(".github/workflows/ci.yml", "name: pretty ty docs\nsteps: []\n")
        self.assertEqual(self._tools(), [])

    def test_a_ruff_isort_section_is_not_isort(self) -> None:
        self.write("pyproject.toml", "[tool.ruff.lint.isort]\nknown-first-party = ['x']\n")
        self.assertEqual(self._tools(), ["ruff"])


class OtherLinterTests(_Fixture):
    def _run(self, executor: Executor, acknowledged=None):
        with patch("mailman.target_checks.execute", executor):
            return run_lint(
                self.run_directory,
                workspace=self.workspace,
                changed_paths=["pkg/mod.py"],
                acknowledged=acknowledged,
            )

    def test_black_and_isort_run_in_check_mode_over_the_changed_files(self) -> None:
        self.write("pyproject.toml", "[tool.black]\n[tool.isort]\n")
        executor = Executor()
        record, findings = self._run(executor)
        self.assertEqual(findings, [])
        self.assertEqual(record["reason"], "passed")
        python = str(self.python)
        self.assertIn([python, "-m", "black", "--check", "--diff", "pkg/mod.py"],
                      executor.calls)
        self.assertIn([python, "-m", "isort", "--check-only", "--diff", "pkg/mod.py"],
                      executor.calls)

    def test_a_mypy_error_blocks_as_lint_failed(self) -> None:
        self.write("mypy.ini", "[mypy]\nstrict = True\n")
        record, findings = self._run(Executor({"mypy pkg/mod.py": 1}))
        self.assertEqual(record["reason"], "failed")
        self.assertEqual([f["code"] for f in findings], ["lint-failed"])
        self.assertIn("`mypy`", findings[0]["detail"])

    def test_a_ty_that_installs_but_cannot_start_blocks_as_not_run(self) -> None:
        # securo#1039: CI ran ty, which Application Control blocks on this
        # host; the first CI run on the pull request failed.
        self.write(".github/workflows/ci.yml", "- run: uvx ty check\n")
        executor = Executor({"ty --version": 1})
        record, findings = self._run(executor)
        self.assertEqual(record["reason"], "not-run")
        self.assertEqual([f["code"] for f in findings], ["lint-not-run"])
        self.assertTrue(findings[0]["blocking"])
        self.assertIn("ty", findings[0]["detail"])
        self.assertIn("after install", findings[0]["detail"])
        self.assertFalse(any("ty check" in " ".join(c) for c in executor.calls))

    def test_an_acknowledged_tool_that_cannot_run_does_not_block(self) -> None:
        self.write(".github/workflows/ci.yml", "- run: uvx ty check\n")
        _, findings = self._run(
            Executor({"ty --version": 1}),
            acknowledged={"ty": "ty.exe is blocked; ran ty in CI on the fork"},
        )
        self.assertEqual([f["code"] for f in findings], ["lint-not-run"])
        self.assertFalse(findings[0]["blocking"])
        self.assertIn("ran ty in CI on the fork", findings[0]["detail"])

    def test_an_acknowledgement_never_clears_a_failure(self) -> None:
        self.write("mypy.ini", "[mypy]\n")
        _, findings = self._run(Executor({"mypy pkg/mod.py": 1}),
                                acknowledged={"mypy": "note"})
        self.assertTrue(findings[0]["blocking"])


class LintAcknowledgementTests(unittest.TestCase):
    def test_the_record_is_pinned_to_the_diff(self) -> None:
        with TemporaryDirectory() as temporary:
            run_directory = Path(temporary)
            record_lint_acknowledgement(
                run_directory, tools=["ty"], note="blocked by policy", diff="diff A\n"
            )
            from hashlib import sha256

            same = sha256(b"diff A\n").hexdigest()
            other = sha256(b"diff B\n").hexdigest()
            self.assertEqual(
                load_lint_acknowledgement(run_directory, same), {"ty": "blocked by policy"}
            )
            self.assertEqual(load_lint_acknowledgement(run_directory, other), {})

    def test_an_unknown_tool_or_empty_note_is_refused(self) -> None:
        with TemporaryDirectory() as temporary:
            with self.assertRaises(ValueError):
                record_lint_acknowledgement(
                    Path(temporary), tools=["pylint"], note="x", diff="d\n"
                )
            with self.assertRaises(ValueError):
                record_lint_acknowledgement(
                    Path(temporary), tools=["ty"], note="  ", diff="d\n"
                )


if __name__ == "__main__":
    unittest.main()
