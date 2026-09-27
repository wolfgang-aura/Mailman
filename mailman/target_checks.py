"""Checks the target's own CI runs on a pull request, run before filing.

`prepare-submission` ran pytest only, so the first CI result on a filed pull
request could be a failure Mailman never looked for:

- pypdf#4105 failed "Check code style issues" 22 seconds after filing on a
  ruff finding (#120). The lint stage finds ruff in the target's configuration,
  runs it over the changed Python files in the run environment, and blocks on a
  finding.
- edgartools#1365 failed `scripts/check_offline_audit.py` on a changed test
  file (#137). When the target ships that script, it runs on the changed test
  files and a non-zero exit blocks.

Each function returns `(record, findings)`; a finding is a dict with `code`,
`blocking` and `detail`, which `prepare-submission` turns into a `Finding`.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from mailman.executor import execute
from mailman.touched_tests import environment_python

OFFLINE_AUDIT_SCRIPT = "scripts/check_offline_audit.py"
TARGET_CHECK_TIMEOUT_SECONDS = 10 * 60
INSTALL_TIMEOUT_SECONDS = 5 * 60

_RUFF_PRE_COMMIT_REV = re.compile(
    r"astral-sh/ruff-pre-commit[^\n]*\n\s*rev:\s*['\"]?v?([0-9][\w.]*)"
)
_RUFF_PIN = re.compile(r"\bruff\s*==\s*([0-9][\w.]*)")
_RUFF_FORMAT = re.compile(r"ruff[ -]format\b|id:\s*ruff-format\b")


def _tail(text: str, lines: int = 60) -> str:
    return "\n".join(text.splitlines()[-lines:])


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _existing(workspace: Path, paths: list[str]) -> list[str]:
    return [
        path.replace("\\", "/")
        for path in paths
        if (workspace / path).is_file()
    ]


def _changed_test_files(workspace: Path, changed_paths: list[str]) -> list[str]:
    from mailman.touched_tests import _is_test_module, _is_test_path

    return [
        path
        for path in _existing(workspace, changed_paths)
        if path.endswith(".py") and _is_test_path(path) and _is_test_module(path)
    ]


def run_offline_audit(
    run_directory: Path,
    *,
    workspace: Path | None,
    changed_paths: list[str],
    timeout_seconds: float = TARGET_CHECK_TIMEOUT_SECONDS,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Run the target's offline audit over the changed test files (#137)."""
    record: dict[str, Any] = {
        "script": OFFLINE_AUDIT_SCRIPT,
        "ran": False,
        "reason": None,
        "files": [],
        "command": None,
        "exit_code": None,
        "timed_out": False,
        "output_tail": "",
    }
    if workspace is None or not (workspace / OFFLINE_AUDIT_SCRIPT).is_file():
        record["reason"] = "not-shipped"
        return record, []
    files = _changed_test_files(workspace, changed_paths)
    record["files"] = files
    if not files:
        record["reason"] = "no-changed-test-files"
        return record, []
    python = environment_python(run_directory)
    if python is None:
        record["reason"] = "no-environment-python"
        return record, [
            {
                "code": "offline-audit-not-run",
                "blocking": True,
                "detail": (
                    f"the target ships {OFFLINE_AUDIT_SCRIPT} and CI runs it on "
                    "changed test files, but the run has no environment "
                    "interpreter to run it with. Run `mailman prepare-environment`."
                ),
            }
        ]
    command = [python, OFFLINE_AUDIT_SCRIPT, *files]
    result = execute(command, working_directory=workspace, timeout_seconds=timeout_seconds)
    record.update(
        {
            "ran": True,
            "command": command,
            "exit_code": result.exit_code,
            "timed_out": result.timed_out,
            "output_tail": _tail(result.stdout + "\n" + result.stderr),
        }
    )
    if result.timed_out or result.exit_code != 0:
        return record, [
            {
                "code": "offline-audit-failed",
                "blocking": True,
                "detail": (
                    f"{OFFLINE_AUDIT_SCRIPT} exited {result.exit_code} on "
                    f"{', '.join(files)}; CI runs the same audit and will fail. "
                    "A new offline test in a network-classed file usually needs "
                    "the target's fast marker."
                ),
            }
        ]
    return record, []


def ruff_configuration(workspace: Path) -> dict[str, Any] | None:
    """Where the target configures or runs ruff, and the pinned version if any."""
    sources: list[tuple[str, str]] = []
    for name in ("pyproject.toml", "ruff.toml", ".ruff.toml", ".pre-commit-config.yaml"):
        text = _read(workspace / name)
        if text:
            sources.append((name, text))
    workflows = workspace / ".github" / "workflows"
    if workflows.is_dir():
        for path in sorted(workflows.iterdir()):
            if path.suffix in (".yml", ".yaml"):
                sources.append((f".github/workflows/{path.name}", _read(path)))
    found: list[str] = []
    version: str | None = None
    formats = False
    for name, text in sources:
        # A pin can sit in a dependency group of a pyproject with no [tool.ruff].
        if version is None:
            pinned = _RUFF_PRE_COMMIT_REV.search(text) or _RUFF_PIN.search(text)
            if pinned:
                version = pinned.group(1)
        mentions = (
            "[tool.ruff" in text
            if name == "pyproject.toml"
            else name.endswith("ruff.toml") or re.search(r"\bruff\b", text) is not None
        )
        if not mentions:
            continue
        found.append(name)
        if name != "pyproject.toml" and _RUFF_FORMAT.search(text):
            formats = True
    if not found:
        return None
    return {"sources": found, "version": version, "format": formats}


def run_lint(
    run_directory: Path,
    *,
    workspace: Path | None,
    changed_paths: list[str],
    timeout_seconds: float = TARGET_CHECK_TIMEOUT_SECONDS,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Run the target's ruff over the changed Python files (#120)."""
    record: dict[str, Any] = {
        "tool": None,
        "ran": False,
        "reason": None,
        "sources": [],
        "version": None,
        "files": [],
        "commands": [],
        "install": None,
        "results": [],
    }
    if workspace is None or not workspace.is_dir():
        record["reason"] = "no-workspace"
        return record, []
    configuration = ruff_configuration(workspace)
    if configuration is None:
        record["reason"] = "no-linter-configured"
        return record, []
    record.update(
        tool="ruff", sources=configuration["sources"], version=configuration["version"]
    )
    files = [path for path in _existing(workspace, changed_paths) if path.endswith(".py")]
    record["files"] = files
    if not files:
        record["reason"] = "no-changed-python-files"
        return record, []

    def skipped(reason: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        record["reason"] = f"skipped: {reason}"
        return record, [
            {
                "code": "lint-skipped",
                "blocking": False,
                "detail": (
                    f"the target runs ruff ({', '.join(record['sources'])}) but it "
                    f"did not run here: {reason}. CI will be the first to lint "
                    "this change."
                ),
            }
        ]

    python = environment_python(run_directory)
    if python is None:
        return skipped("the run has no environment interpreter")
    probe = execute(
        [python, "-m", "ruff", "--version"],
        working_directory=workspace,
        timeout_seconds=120,
    )
    if probe.timed_out or probe.exit_code != 0:
        requirement = f"ruff=={record['version']}" if record["version"] else "ruff"
        install = execute(
            [python, "-m", "pip", "install", "--disable-pip-version-check", requirement],
            working_directory=workspace,
            timeout_seconds=INSTALL_TIMEOUT_SECONDS,
        )
        record["install"] = {
            "requirement": requirement,
            "exit_code": install.exit_code,
            "timed_out": install.timed_out,
            "output_tail": _tail(install.stdout + "\n" + install.stderr, 20),
        }
        if install.timed_out or install.exit_code != 0:
            return skipped(f"`pip install {requirement}` exited {install.exit_code}")

    # `--force-exclude` keeps the target's own excludes for files named on the
    # command line, which is what CI's `ruff check .` would honour.
    ruff = [python, "-m", "ruff"]
    options = ["--no-cache", "--force-exclude", *files]
    commands = [[*ruff, "check", *options]]
    if configuration["format"]:
        commands.append([*ruff, "format", "--check", *options])
    findings: list[dict[str, Any]] = []
    for command in commands:
        result = execute(
            command, working_directory=workspace, timeout_seconds=timeout_seconds
        )
        record["commands"].append(command)
        record["results"].append(
            {
                "command": command,
                "exit_code": result.exit_code,
                "timed_out": result.timed_out,
                "output_tail": _tail(result.stdout + "\n" + result.stderr),
            }
        )
        if result.timed_out or result.exit_code != 0:
            findings.append(
                {
                    "code": "lint-failed",
                    "blocking": True,
                    "detail": (
                        f"`ruff {command[3]}` exited {result.exit_code} on the "
                        f"changed files; the target's CI runs ruff and will fail. "
                        + _tail(result.stdout + "\n" + result.stderr, 15)
                    ),
                }
            )
    record["ran"] = True
    record["reason"] = "failed" if findings else "passed"
    return record, findings


__all__ = [
    "OFFLINE_AUDIT_SCRIPT",
    "ruff_configuration",
    "run_lint",
    "run_offline_audit",
]
