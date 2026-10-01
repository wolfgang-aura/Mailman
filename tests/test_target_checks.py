"""Target CI checks run before filing: the offline audit (#137) and lint (#120)."""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from mailman.executor import CommandResult
from mailman.target_checks import (
    _signatures,
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

    def test_a_pre_commit_rev_outranks_a_dev_dependency_pin(self) -> None:
        # prefect's CI runs pre-commit at v0.15.19; its dev group pins
        # ruff==0.16.2, which the gate took and failed a clean patch. #247.
        self.write(
            "pyproject.toml",
            '[dependency-groups]\ndev = ["ruff==0.16.2"]\n[tool.ruff]\n',
        )
        self.write(
            ".pre-commit-config.yaml",
            "repos:\n  - repo: https://github.com/astral-sh/ruff-pre-commit\n"
            "    rev: v0.15.19\n    hooks:\n      - id: ruff-check\n",
        )
        self.assertEqual(ruff_configuration(self.workspace)["version"], "0.15.19")

    def test_a_wildcard_dependency_pin_keeps_its_wildcard(self) -> None:
        # schwifty pins `ruff==0.15.*`; the parser kept `0.15.` and pip
        # refused the requirement. Mailman #361.
        self.write(
            "pyproject.toml",
            '[dependency-groups]\ndev = ["ruff==0.15.*"]\n[tool.ruff]\n',
        )
        self.assertEqual(ruff_configuration(self.workspace)["version"], "0.15.*")

    def _black_version(self, rev: str) -> str | None:
        self.write("pyproject.toml", "[tool.black]\n")
        self.write(
            ".pre-commit-config.yaml",
            "repos:\n  - repo: https://github.com/psf/black-pre-commit-mirror\n"
            f"    rev: {rev}\n    hooks:\n      - id: black\n",
        )
        return next(c for c in lint_configurations(self.workspace)
                    if c["tool"] == "black")["version"]

    def test_a_frozen_sha_rev_pins_the_version_in_its_comment(self) -> None:
        # python/typeshed (#310): `rev: <sha> # frozen: 26.5.1` was read as
        # black==<sha>, which pip refuses.
        self.assertEqual(
            self._black_version("4160603246a6b365d4a2af661c6d71b0a0f50478 # frozen: 26.5.1"),
            "26.5.1",
        )

    def test_a_bare_sha_rev_is_no_pin(self) -> None:
        self.assertIsNone(self._black_version("4160603246a6b365d4a2af661c6d71b0a0f50478"))


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

    def test_a_ruff_other_than_the_pin_runs_the_pin_from_a_tools_folder(self) -> None:
        # prefect pins ruff 0.15.19; the lockfile's 0.16.2 added UP007 and
        # the gate blocked a clean patch. `python -m ruff` still found the
        # venv's ruff.exe, so the pinned binary is called directly. Mailman #247.
        tools = self.run_directory / "lint-tools" / "ruff-0.16.0"
        binary = tools / "bin" / "ruff.exe"

        class Newer(Executor):
            def __call__(self, command, *, working_directory, timeout_seconds, **options):
                result = super().__call__(
                    command,
                    working_directory=working_directory,
                    timeout_seconds=timeout_seconds,
                )
                self.environments = getattr(self, "environments", [])
                self.environments.append(options.get("environment"))
                if "--target" in command:
                    binary.parent.mkdir(parents=True)
                    binary.write_bytes(b"")
                if command[-1] == "--version":
                    version = "0.16.0" if command[0] == str(binary) else "0.16.2"
                    return _result(list(command), stdout=f"ruff {version}\n")
                return result

        executor = Newer()
        record, findings = self._run(executor)
        self.assertEqual(findings, [])
        install = record["tools"][0]["install"]
        self.assertEqual(install["requirement"], "ruff==0.16.0")
        self.assertIn("--target", executor.calls[1])
        self.assertIn(str(tools), executor.calls[1])
        self.assertEqual(executor.calls[-1][:2], [str(binary), "check"])
        self.assertEqual(executor.environments[-1], {"PYTHONPATH": str(tools)})

    def test_a_pin_that_still_reports_another_version_blocks_as_not_run(self) -> None:
        class Stuck(Executor):
            def __call__(self, command, **options):
                result = super().__call__(command, **options)
                if command[-1] == "--version":
                    return _result(list(command), stdout="ruff 0.16.2\n")
                return result

        executor = Stuck()
        record, findings = self._run(executor)
        self.assertEqual([f["code"] for f in findings], ["lint-not-run"])
        self.assertIn("0.16.2", record["tools"][0]["reason"])
        self.assertFalse(any("check" in call for call in executor.calls))

    def test_a_ruff_inside_a_wildcard_pin_is_not_reinstalled(self) -> None:
        # Mailman #361: schwifty pins pyrefly==1.1.* and the venv has 1.1.1.
        self.write(".github/workflows/ci.yml", "- run: pip install ruff==0.16.*\n")

        class Inside(Executor):
            def __call__(self, command, **options):
                if command[-1] == "--version":
                    self.calls.append(list(command))
                    return _result(list(command), stdout="ruff 0.16.3\n")
                return super().__call__(command, **options)

        executor = Inside()
        record, findings = self._run(executor)
        self.assertEqual(findings, [])
        self.assertIsNone(record["tools"][0]["install"])
        self.assertFalse(any("pip" in call for call in executor.calls))

    def test_a_ruff_at_the_pin_is_not_reinstalled(self) -> None:
        class Pinned(Executor):
            def __call__(self, command, **options):
                if command[-1] == "--version":
                    self.calls.append(list(command))
                    return _result(list(command), stdout="ruff 0.16.0\n")
                return super().__call__(command, **options)

        executor = Pinned()
        record, _ = self._run(executor)
        self.assertIsNone(record["tools"][0]["install"])
        self.assertFalse(any("pip" in call for call in executor.calls))

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

    def test_a_flake8_plugin_hook_is_not_flake8(self) -> None:
        # nox (#160): its only flake8-named hook is flake8-lazy, which checks
        # lazy imports under nox/; plain flake8 reported E501 CI never sees.
        self.write(
            ".pre-commit-config.yaml",
            "repos:\n  - repo: https://github.com/henryiii/flake8-lazy\n"
            "    rev: v0.9.0\n    hooks:\n      - id: flake8-lazy\n"
            "        args: ['--apply=set']\n        files: '^nox/'\n",
        )
        self.assertEqual(self._tools(), [])

    def test_the_flake8_hook_itself_is_still_found(self) -> None:
        self.write(
            ".pre-commit-config.yaml",
            "repos:\n  - repo: https://github.com/pycqa/flake8\n"
            "    rev: 7.1.0\n    hooks:\n      - id: flake8\n",
        )
        self.assertEqual(self._tools(), ["flake8"])

    def test_ty_is_not_found_in_unrelated_prose(self) -> None:
        self.write(".github/workflows/ci.yml", "name: pretty ty docs\nsteps: []\n")
        self.assertEqual(self._tools(), [])

    def test_checkers_run_as_poe_tasks_are_found(self) -> None:
        # pandas-stubs (#305): CI runs `poetry run poe ty|pyrefly|pyright|mypy`
        # and pyproject defines each task; no `[tool.ty]` and no `ty check`.
        self.write(
            "pyproject.toml",
            "[tool.poe.tasks.ty]\nscript = 'x'\n[tool.poe.tasks.ty_dist]\n"
            "[tool.poe.tasks.type_completeness]\n[tool.poe.tasks.pyrefly]\n",
        )
        self.write(
            ".github/workflows/test.yml",
            "steps:\n  - run: poetry run poe pyright\n  - run: poetry run poe mypy\n",
        )
        self.assertEqual(self._tools(), ["mypy", "ty", "pyright", "pyrefly"])

    def test_a_poe_task_with_a_longer_name_is_not_the_checker(self) -> None:
        self.write("pyproject.toml", "[tool.poe.tasks.ty_dist]\n[tool.poe.tasks.mypy_dist]\n")
        self.write(".github/workflows/ci.yml", "- run: poe type_completeness\n")
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

    def test_a_changed_stub_is_type_checked(self) -> None:
        # pandas-stubs (#305): every source change is a .pyi; a `.py`-only
        # filter linted nothing and the misplaced ty ignore went unseen.
        self.write("pyproject.toml", "[tool.ty]\n[tool.pyright]\n[tool.pyrefly]\n")
        self.write("pkg/mod.pyi", "x: int\n")
        executor = Executor()
        with patch("mailman.target_checks.execute", executor):
            record, _ = run_lint(
                self.run_directory, workspace=self.workspace,
                changed_paths=["pkg/mod.pyi", "tests/test_mod.py"],
            )
        python = str(self.python)
        self.assertEqual(record["files"], ["pkg/mod.pyi", "tests/test_mod.py"])
        for command in (
            [python, "-m", "ty", "check", "pkg/mod.pyi", "tests/test_mod.py"],
            [python, "-m", "pyright", "pkg/mod.pyi", "tests/test_mod.py"],
            [python, "-m", "pyrefly", "check", "pkg/mod.pyi", "tests/test_mod.py"],
        ):
            self.assertIn(command, executor.calls)

    def test_a_hook_s_args_reach_the_tool(self) -> None:
        # agentscope's black hook sets `args: [--line-length=79]`; black ran
        # at its default 88 and passed lines CI rejects. Mailman #288.
        self.write(
            ".pre-commit-config.yaml",
            "repos:\n"
            "  - repo: https://github.com/psf/black\n"
            "    hooks:\n"
            "      - id: black\n"
            "        args: [--line-length=79, '--skip-string-normalization']\n"
            "  - repo: https://github.com/pycqa/flake8\n"
            "    hooks:\n"
            "      - id: flake8\n"
            "        args:\n"
            "          - --max-line-length=79\n"
            "        exclude: ^docs\n",
        )
        executor = Executor()
        self._run(executor)
        python = str(self.python)
        self.assertIn(
            [python, "-m", "black", "--check", "--diff", "--line-length=79",
             "--skip-string-normalization", "pkg/mod.py"],
            executor.calls,
        )
        self.assertIn([python, "-m", "flake8", "--max-line-length=79", "pkg/mod.py"],
                      executor.calls)

    def test_a_hook_option_keeps_its_separate_value(self) -> None:
        # isort's documented hook is `args: ["--profile", "black"]`. Keeping
        # only dashed tokens ran `isort --profile pkg/mod.py`, which read the
        # file as the profile name and failed a clean candidate.
        self.write(
            ".pre-commit-config.yaml",
            "repos:\n"
            "  - repo: https://github.com/pycqa/isort\n"
            "    hooks:\n"
            "      - id: isort\n"
            "        args: [\"--profile\", \"black\", \"--filter-files\"]\n"
            "  - repo: https://github.com/pycqa/flake8\n"
            "    hooks:\n"
            "      - id: flake8\n"
            "        args: [\n"
            "          --max-line-length, \"100\",\n"
            "          --extend-ignore=E203,\n"
            "          src,\n"
            "        ]\n",
        )
        executor = Executor()
        self._run(executor)
        python = str(self.python)
        self.assertIn(
            [python, "-m", "isort", "--check-only", "--diff", "--profile", "black",
             "--filter-files", "pkg/mod.py"],
            executor.calls,
        )
        self.assertIn(
            [python, "-m", "flake8", "--max-line-length", "100",
             "--extend-ignore=E203", "pkg/mod.py"],
            executor.calls,
        )

    def test_a_ruff_hook_s_config_path_reaches_check_and_format(self) -> None:
        # capa's local ruff hooks pass `--config .github/ruff.toml`; ruff ran
        # with its defaults and flagged capa's length-sorted imports. The
        # hook's `--fix` still stays out. Mailman #299.
        self.write(
            ".pre-commit-config.yaml",
            "repos:\n"
            "-   repo: local\n"
            "    hooks:\n"
            "    -   id: ruff-format\n"
            "        entry: ruff\n"
            "        args:\n"
            "        -   \"format\"\n"
            "        -   \"--config\"\n"
            "        -   \".github/ruff.toml\"\n"
            "        -   \"capa/\"\n"
            "-   repo: local\n"
            "    hooks:\n"
            "    -   id: ruff\n"
            "        entry: ruff\n"
            "        args:\n"
            "        -   \"check\"\n"
            "        -   \"--fix\"\n"
            "        -   \"--config\"\n"
            "        -   \".github/ruff.toml\"\n"
            "        -   \"capa/\"\n",
        )
        executor = Executor()
        self._run(executor)
        python = str(self.python)
        self.assertIn(
            [python, "-m", "ruff", "check", "--no-cache", "--force-exclude",
             "--config", ".github/ruff.toml", "pkg/mod.py"],
            executor.calls,
        )
        self.assertIn(
            [python, "-m", "ruff", "format", "--check", "--no-cache", "--force-exclude",
             "--config", ".github/ruff.toml", "pkg/mod.py"],
            executor.calls,
        )
        self.assertFalse(any("--fix" in call for call in executor.calls))

    def test_a_mypy_error_blocks_as_lint_failed(self) -> None:
        self.write("mypy.ini", "[mypy]\nstrict = True\n")
        record, findings = self._run(Executor({"mypy pkg/mod.py": 1}))
        self.assertEqual(record["reason"], "failed")
        self.assertEqual([f["code"] for f in findings], ["lint-failed"])
        self.assertIn("`mypy`", findings[0]["detail"])

    def test_mypy_skips_files_the_target_config_excludes(self) -> None:
        # ipython excludes `tests` from mypy; naming tests/test_history.py on
        # the command line bypassed that and raised errors CI never sees.
        self.write("pyproject.toml", "[tool.mypy]\nexclude = ['tests', 'test_.+\\\\.py']\n")
        executor = Executor({"mypy pkg": 1})
        with patch("mailman.target_checks.execute", executor):
            record, findings = run_lint(
                self.run_directory,
                workspace=self.workspace,
                changed_paths=["pkg/mod.py", "tests/test_mod.py"],
            )
        mypy_calls = [c for c in executor.calls if "mypy" in c and "--version" not in c
                      and "pip" not in c]
        self.assertEqual(mypy_calls, [[str(self.python), "-m", "mypy", "pkg/mod.py"]])
        self.assertEqual([f["code"] for f in findings], ["lint-failed"])

    def test_mypy_with_every_changed_file_excluded_does_not_run(self) -> None:
        self.write("mypy.ini", "[mypy]\nexclude = tests\n")
        executor = Executor({"mypy": 1})
        with patch("mailman.target_checks.execute", executor):
            record, findings = run_lint(
                self.run_directory,
                workspace=self.workspace,
                changed_paths=["tests/test_mod.py"],
            )
        self.assertEqual(findings, [])
        self.assertEqual(record["reason"], "passed")
        self.assertEqual(executor.calls, [])

    # pylint's config (#260): black's exclude is a YAML anchor, a second black
    # hook takes only doc/, and mypy's exclude is inline.
    _PYLINT_PRE_COMMIT = (
        "exclude: '^vendor/'\n"
        "repos:\n"
        "  - repo: https://github.com/psf/black-pre-commit-mirror\n"
        "    rev: 26.5.1\n"
        "    hooks:\n"
        "      - id: black\n"
        "        args: [--safe, --quiet]\n"
        "        exclude: &fixtures tests(/\\w*)*/functional/|tests/input|doc/data/messages\n"
        "      - id: black\n"
        "        name: black-doc\n"
        "        files: doc/data/messages/\n"
        "        exclude: |\n"
        "          (?x)^(\n"
        "            doc/data/messages/r/raw.py\n"
        "          )$\n"
        "  - repo: https://github.com/pre-commit/mirrors-mypy\n"
        "    rev: v2.3.1\n"
        "    hooks:\n"
        "      - id: mypy\n"
        "        additional_dependencies:\n"
        "          [\"isort>=5\"]\n"
        "        exclude: *fixtures\n"
    )

    def _lint_pylint_shape(self, changed: list[str]) -> Executor:
        self.write(".pre-commit-config.yaml", self._PYLINT_PRE_COMMIT)
        for path in changed:
            self.write(path, "x = 1\n")
        executor = Executor({"--check --diff": 1, "mypy pkg": 1})
        with patch("mailman.target_checks.execute", executor):
            self.record, self.findings = run_lint(
                self.run_directory, workspace=self.workspace, changed_paths=changed
            )
        return executor

    def _linted(self, executor: Executor, tool: str) -> list[str]:
        return [
            path for call in executor.calls
            if tool in call and "--version" not in call and "pip" not in call
            for path in call if path.endswith(".py")
        ]

    def test_a_pre_commit_hook_exclude_skips_the_file_in_ci(self) -> None:
        executor = self._lint_pylint_shape(
            ["pkg/checker.py", "tests/functional/u/unreachable.py"]
        )
        self.assertEqual(self._linted(executor, "black"), ["pkg/checker.py"])
        self.assertEqual(self._linted(executor, "mypy"), ["pkg/checker.py"])

    def test_every_changed_file_outside_the_hooks_does_not_run(self) -> None:
        executor = self._lint_pylint_shape(
            ["tests/functional/u/unreachable.py", "vendor/lib.py"]
        )
        self.assertEqual(self._linted(executor, "black"), [])
        self.assertEqual(self._linted(executor, "mypy"), [])
        self.assertEqual(self.findings, [])
        self.assertEqual(self.record["reason"], "passed")

    def test_a_second_hook_of_the_tool_covers_its_own_files(self) -> None:
        executor = self._lint_pylint_shape(
            ["doc/data/messages/a/good.py", "doc/data/messages/r/raw.py"]
        )
        self.assertEqual(self._linted(executor, "black"), ["doc/data/messages/a/good.py"])

    def test_a_plain_multi_line_exclude_is_read_as_its_pattern(self) -> None:
        # agentscope: `exclude:` with the pattern on the lines below read as
        # '', which matches every path, so mypy never ran. Mailman #288.
        self.write(
            ".pre-commit-config.yaml",
            "repos:\n"
            "  - repo: https://github.com/pre-commit/mirrors-mypy\n"
            "    rev: v1.10.0\n"
            "    hooks:\n"
            "      - id: mypy\n"
            "        exclude:\n"
            "            (?x)(\n"
            "                pb2\\.py$\n"
            "                | ^docs\n"
            "            )\n"
            "  - repo: https://github.com/pre-commit/pre-commit-hooks\n"
            "    hooks:\n"
            "      - id: check-yaml\n",
        )
        changed = ["pkg/mod.py", "docs/conf.py"]
        for path in changed:
            self.write(path, "x = 1\n")
        executor = Executor({"mypy pkg": 1})
        with patch("mailman.target_checks.execute", executor):
            run_lint(self.run_directory, workspace=self.workspace, changed_paths=changed)
        self.assertEqual(self._linted(executor, "mypy"), ["pkg/mod.py"])

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


class HookArgumentTests(_Fixture):
    """How a pre-commit hook's `args` are read. Mailman #358."""

    def _args(self, hooks: str) -> list:
        from mailman.target_checks import _pre_commit_scopes

        self.write(".pre-commit-config.yaml", "repos:\n  - repo: local\n    hooks:\n" + hooks)
        return [hook["args"] for hook in _pre_commit_scopes(self.workspace)[1]]

    def test_a_comma_inside_quotes_stays_in_its_argument(self) -> None:
        self.assertEqual(
            self._args(
                "      - id: flake8\n"
                "        args: [\"--extend-ignore=E203,W503\", '--select=E,W', --max-line-length=100]\n"
            ),
            [["--extend-ignore=E203,W503", "--select=E,W", "--max-line-length=100"]],
        )

    def test_a_multi_line_list_does_not_end_at_a_quoted_bracket(self) -> None:
        self.assertEqual(
            self._args(
                "      - id: codespell\n"
                "        args: [\n"
                "          \"--ignore-words-list=[a]\",  # a comment, with a comma\n"
                "          --quiet,\n"
                "        ]\n"
                "      - id: black\n"
                "        args: [--safe]\n"
            ),
            [["--ignore-words-list=[a]", "--quiet"], ["--safe"]],
        )

    def test_an_alias_reads_its_anchor_and_an_unknown_one_is_not_empty(self) -> None:
        self.assertEqual(
            self._args(
                "      - id: black\n"
                "        args: &style [--line-length=79, --safe]\n"
                "      - id: blacken-docs\n"
                "        args: *style\n"
                "      - id: flake8\n"
                "        args: *elsewhere\n"
            ),
            [["--line-length=79", "--safe"], ["--line-length=79", "--safe"], None],
        )

    def test_a_positional_after_a_flag_stays_out(self) -> None:
        from mailman.target_checks import _options

        # `--filter-files` takes no value, so `src` is a path for isort.
        self.assertEqual(_options(["--filter-files", "src"], "isort"), ["--filter-files"])
        self.assertEqual(_options(["--check", "src"], "black"), ["--check"])
        self.assertEqual(_options(["--count", "src"], "flake8"), ["--count"])
        # An option that takes a value keeps it (#320), plugin options too.
        self.assertEqual(
            _options(["--profile", "black", "--filter-files"], "isort"),
            ["--profile", "black", "--filter-files"],
        )
        self.assertEqual(
            _options(["--max-line-length", "100", "--docstring-convention", "google", "src"], "flake8"),
            ["--max-line-length", "100", "--docstring-convention", "google"],
        )
        self.assertEqual(_options(["--config", "setup.cfg"], "black"), ["--config", "setup.cfg"])


class BaselineExecutor:
    """Makes the base worktree on `git worktree add`; answers by directory."""

    def __init__(self, workspace_output: str, base_output: str | None,
                 base_files=("pkg/mod.py",)) -> None:
        self.workspace_output = workspace_output
        self.base_output = base_output
        self.base_files = base_files
        self.calls: list[tuple[list[str], str]] = []

    def __call__(self, command, *, working_directory, timeout_seconds, **_):
        self.calls.append((list(command), str(working_directory)))
        if command[:3] == ["git", "worktree", "add"]:
            base = Path(command[4])
            for relative in self.base_files:
                (base / relative).parent.mkdir(parents=True, exist_ok=True)
                (base / relative).write_text("x = 1\n", encoding="utf-8")
            return _result(list(command))
        if command[0] == "git" or "--version" in command:
            return _result(list(command))
        if "lint-base" in str(working_directory):
            if self.base_output is None:
                return _result(list(command))
            return _result(list(command), exit_code=1, stdout=self.base_output)
        return _result(list(command), exit_code=1, stdout=self.workspace_output)


class LintBaselineTests(_Fixture):
    # ipython#9891: flake8, black and mypy each failed on lines the patch never
    # touched, and the run was held on findings the base commit already had.
    def setUp(self) -> None:
        super().setUp()
        self.write(".flake8", "[flake8]\n")

    def _run(self, executor, base_commit="a" * 40):
        with patch("mailman.target_checks.execute", executor):
            return run_lint(
                self.run_directory,
                workspace=self.workspace,
                changed_paths=["pkg/mod.py"],
                base_commit=base_commit,
            )

    def test_a_finding_the_base_already_has_does_not_block(self) -> None:
        executor = BaselineExecutor(
            "pkg/mod.py:40:80: E501 line too long (91 > 79 characters)\n",
            "pkg/mod.py:31:80: E501 line too long (91 > 79 characters)\n",
        )
        record, findings = self._run(executor)
        self.assertEqual([f["code"] for f in findings], ["lint-preexisting"])
        self.assertFalse(findings[0]["blocking"])
        self.assertEqual(record["reason"], "preexisting")
        self.assertIn(["git", "worktree", "remove", "--force",
                       str(self.run_directory / "scratch" / "lint-base")],
                      [call for call, _ in executor.calls])

    def test_a_finding_the_patch_added_blocks_and_is_named(self) -> None:
        executor = BaselineExecutor(
            "pkg/mod.py:31:80: E501 line too long (91 > 79 characters)\n"
            "pkg/mod.py:44:1: F401 'os' imported but unused\n",
            "pkg/mod.py:31:80: E501 line too long (91 > 79 characters)\n",
        )
        record, findings = self._run(executor)
        self.assertEqual([f["code"] for f in findings], ["lint-failed"])
        self.assertTrue(findings[0]["blocking"])
        self.assertIn("F401", findings[0]["detail"])
        self.assertNotIn("E501", findings[0]["detail"])
        self.assertEqual(record["reason"], "failed")

    def test_a_base_that_passes_leaves_the_failure_blocking(self) -> None:
        executor = BaselineExecutor("pkg/mod.py:1:1: F401 'os' imported but unused\n", None)
        _, findings = self._run(executor)
        self.assertEqual([f["code"] for f in findings], ["lint-failed"])
        self.assertIn("the base commit passes", findings[0]["detail"])

    def test_a_file_new_in_the_patch_is_not_compared(self) -> None:
        executor = BaselineExecutor("pkg/mod.py:1:1: F401\n", "pkg/mod.py:1:1: F401\n",
                                    base_files=())
        _, findings = self._run(executor)
        self.assertEqual([f["code"] for f in findings], ["lint-failed"])
        self.assertTrue(findings[0]["blocking"])

    def test_diff_context_holding_the_new_code_is_not_a_new_finding(self) -> None:
        # black --diff printed the patch's own code as context next to an
        # old reformat; only +/- lines tell the two runs apart.
        base = "would reformat pkg/mod.py\n@@ -1,3 +1,3 @@\n x = 1\n-y=2\n+y = 2\n"
        patched = ("would reformat pkg/mod.py\n@@ -1,4 +1,4 @@\n x = 1\n"
                   " def added(pager): pass\n-y=2\n+y = 2\n@@ -9,2 +9,2 @@\n z = 3\n")
        _, findings = self._run(BaselineExecutor(patched, base))
        self.assertEqual([f["code"] for f in findings], ["lint-preexisting"])

    def test_a_note_naming_the_bare_root_is_not_a_new_finding(self) -> None:
        # pydata/xarray#10639 (#177): ty prints the searched root with no
        # trailing separator, and the base worktree's root differs.
        base_root = self.run_directory / "scratch" / "lint-base"
        note = "info:   1. {} (first-party code)\n"
        error = "error[unresolved-import]: Cannot resolve imported module `pydap`\n"
        executor = BaselineExecutor(error + note.format(self.workspace),
                                    error + note.format(base_root))
        _, findings = self._run(executor)
        self.assertEqual([f["code"] for f in findings], ["lint-preexisting"])

    def test_a_hint_that_moved_between_imports_is_not_a_new_finding(self) -> None:
        # pydata/xarray#10639 (#179): mypy prints the stubs hint once, beside
        # whichever import of scipy it checks first.
        errors = (
            'pkg/a.py:3: error: Library stubs not installed for "scipy"  [import-untyped]\n'
            'pkg/b.py:5: error: Library stubs not installed for "scipy"  [import-untyped]\n'
        )
        executor = BaselineExecutor(
            errors + 'pkg/a.py:3: note: Hint: "python3 -m pip install scipy-stubs"\n',
            errors + 'pkg/b.py:5: note: Hint: "python3 -m pip install scipy-stubs"\n',
        )
        _, findings = self._run(executor)
        self.assertEqual([f["code"] for f in findings], ["lint-preexisting"])

    def test_a_new_error_still_blocks_beside_its_note(self) -> None:
        base = 'pkg/a.py:3: error: old  [misc]\n'
        executor = BaselineExecutor(
            base + 'pkg/a.py:9: error: new  [arg-type]\npkg/a.py:9: note: see here\n',
            base,
        )
        _, findings = self._run(executor)
        self.assertEqual([f["code"] for f in findings], ["lint-failed"])
        self.assertIn("error: new", findings[0]["detail"])
        self.assertNotIn("see here", findings[0]["detail"])

    def _pyrefly_missing(self, module: str, line: int) -> str:
        return (f"ERROR Cannot find module `{module}` [missing-import]\n"
                f" --> pkg/mod.py:{line}:1\n  |\n{line} | import {module}\n  |\n"
                f"  Looked in these locations (from config in `{self.workspace}`):\n")

    def test_an_added_import_of_a_module_the_base_cannot_find_is_not_new(self) -> None:
        # python/typeshed#15495 (#312): pyrefly on bare files never resolves
        # `grpc.aio`; the patch's extra import of it is one more such block.
        base = self._pyrefly_missing("grpc.aio", 8) + " INFO 1 errors\n"
        patched = (self._pyrefly_missing("grpc.aio", 8)
                   + self._pyrefly_missing("grpc.aio", 9) + " INFO 2 errors\n")
        _, findings = self._run(BaselineExecutor(patched, base))
        self.assertEqual([f["code"] for f in findings], ["lint-preexisting"])

    def test_an_import_of_a_module_the_base_finds_still_blocks(self) -> None:
        base = self._pyrefly_missing("grpc.aio", 8)
        patched = base + self._pyrefly_missing("grcp", 9)
        _, findings = self._run(BaselineExecutor(patched, base))
        self.assertEqual([f["code"] for f in findings], ["lint-failed"])
        self.assertIn("grcp", findings[0]["detail"])

    def test_without_a_base_commit_no_worktree_is_made(self) -> None:
        executor = BaselineExecutor("pkg/mod.py:1:1: F401\n", "pkg/mod.py:1:1: F401\n")
        _, findings = self._run(executor, base_commit=None)
        self.assertEqual([f["code"] for f in findings], ["lint-failed"])
        self.assertFalse(any(call[0] == "git" for call, _ in executor.calls))


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


class SignatureTests(unittest.TestCase):
    """pyright output against the base comparison (#305)."""

    ROOT = Path("C:/work/run/workspace")

    def _pyright(self, drive: str, line: int) -> str:
        path = rf"{drive}:\work\run\workspace\pkg\mod.pyi"
        return (
            f"{path}\n"
            f"  {path}:{line}:6 - error: Import \"x\" could not be resolved (reportMissingImports)\n"
            "1 error, 0 warnings, 0 informations\n"
        )

    def test_a_lowercase_drive_is_still_the_workspace_root(self) -> None:
        signatures = _signatures(self._pyright("c", 3), (self.ROOT,))
        self.assertIn("pkg/mod.pyi", signatures)
        self.assertFalse(any("work/run" in line for line in signatures))

    def test_indented_pyright_errors_are_findings_not_diff_context(self) -> None:
        signatures = _signatures(self._pyright("C", 3), (self.ROOT,))
        self.assertTrue(any("reportMissingImports" in line for line in signatures))

    def test_a_diff_s_context_lines_are_still_skipped(self) -> None:
        output = "--- a/pkg/mod.py\n+++ b/pkg/mod.py\n@@ -1,2 +1,2 @@\n x = 1\n-y=2\n+y = 2\n"
        self.assertEqual(sorted(_signatures(output, (self.ROOT,))), ["+y = #", "-y=#"])

    def test_a_code_frame_is_not_a_finding(self) -> None:
        output = "error[x]: bad\n  --> pkg/mod.py:3:1\n   |\n 3 | x = 1\n   | ^\n"
        self.assertEqual(
            sorted(_signatures(output, (self.ROOT,))),
            ["--> pkg/mod.py:#:#", "error[x]: bad"],
        )

    def test_a_json_escaped_root_is_still_the_workspace_root(self) -> None:
        output = r'  Import root: "C:\\work\\run\\workspace"' + "\n"
        self.assertEqual(list(_signatures(output, (self.ROOT,))), ['Import root: "<root>"'])
