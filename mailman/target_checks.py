"""Checks the target's own CI runs on a pull request, run before filing.

`prepare-submission` ran pytest only, so the first CI result on a filed pull
request could be a failure Mailman never looked for:

- edgartools#1365 failed `scripts/check_offline_audit.py` on a changed test
  file (#137). When the target ships that script, it runs on the changed test
  files and a non-zero exit blocks.

Each function returns `(record, findings)`; a finding is a dict with `code`,
`blocking` and `detail`, which `prepare-submission` turns into a `Finding`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mailman.executor import execute
from mailman.touched_tests import environment_python

OFFLINE_AUDIT_SCRIPT = "scripts/check_offline_audit.py"
TARGET_CHECK_TIMEOUT_SECONDS = 10 * 60


def _tail(text: str, lines: int = 60) -> str:
    return "\n".join(text.splitlines()[-lines:])


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


__all__ = [
    "OFFLINE_AUDIT_SCRIPT",
    "run_offline_audit",
]
