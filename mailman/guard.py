"""Check that the tests a candidate adds fail without its source change.

A test that passes at the base commit guards nothing. django-oauth-toolkit
#1918's candidate added four parametrized `Application.clean()` tests that
passed at base: the fixture failed `clean()` on `algorithm`, not on the URI the
test was about. The reviewer approved them, and final verification only runs
tests with the fix in place. This stage puts the base bytes of every changed
source file back, runs only the test functions the diff adds, restores the
candidate, and records which of them passed. Mailman #410.
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mailman.executor import execute
from mailman.touched_tests import _is_test_path, diff_sha256, environment_python

GUARD_FILENAME = "guard.json"
# Bump when the outcome rules change, so a cached record is rerun (#461).
GUARD_VERSION = 2
GUARD_TIMEOUT_SECONDS = 10 * 60
_SOURCE_SUFFIXES = (".py", ".pyi")
_FILE_HEADER = re.compile(r"^\+\+\+ b/(.+)$")
_ADDED_TEST = re.compile(r"^\+\s*(?:async\s+)?def\s+(test\w*)\s*\(")
#: A pytest-mypy-plugins case added to a yml test file (#438).
_ADDED_YAML_CASE = re.compile(r"^\+\s*-\s+case:\s*['\"]?(\w+)")
#: pytest options whose value is the next argument, which may be a real path.
_VALUE_OPTIONS = frozenset({
    "-c", "-p", "-o", "-W", "-n", "-r", "--rootdir", "--confcutdir", "--basetemp",
    "--ds", "--deselect", "--ignore", "--ignore-glob", "--override-ini", "--config-file",
})
_OUTCOME = re.compile(r"^(PASSED|FAILED|ERROR)\s+(\S+)", re.M)


def added_test_names(diff: str) -> dict[str, list[str]]:
    """Test functions the diff adds, by test file, in diff order."""
    names: dict[str, list[str]] = {}
    current: str | None = None
    for line in diff.splitlines():
        header = _FILE_HEADER.match(line)
        if header:
            current = header.group(1).strip()
            continue
        if line.startswith("diff --git"):
            current = None
            continue
        if current is None or not _is_test_path(current):
            continue
        pattern = _ADDED_YAML_CASE if current.endswith((".yml", ".yaml")) else _ADDED_TEST
        match = pattern.match(line)
        if match and match.group(1) not in names.get(current, []):
            names.setdefault(current, []).append(match.group(1))
    return names


def _pytest_command(
    verification_command: list[str] | None, python: str | None, workspace: Path
) -> list[str] | None:
    """The recorded verification argv without its own targets, or a bare pytest."""
    runner = next(
        (index for index, part in enumerate((verification_command or [])[:3]) if "pytest" in part),
        None,
    )
    if verification_command and runner is not None:
        # The interpreter and `-m pytest` stay; the recorded targets and any
        # `-k` selection give way to the added tests. A `-m` marker filter
        # after pytest stays, so the same lane runs.
        kept: list[str] = verification_command[: runner + 1]
        skip_next = False
        for part in verification_command[runner + 1:]:
            if skip_next:
                skip_next = False
                continue
            if part == "-k":
                skip_next = True
                continue
            if part.startswith("-") or kept[-1] in _VALUE_OPTIONS:
                kept.append(part)
                continue
            target = part.split("::", 1)[0]
            if "::" in part or (workspace / target).exists():
                continue
            kept.append(part)
        return kept
    if python:
        return [python, "-m", "pytest", "-q"]
    return None


def _base_bytes(workspace: Path, base_commit: str, path: str) -> bytes | None:
    result = subprocess.run(
        ["git", "-C", str(workspace), "show", f"{base_commit}:{path}"],
        capture_output=True,
    )
    return result.stdout if result.returncode == 0 else None


def _write(run_directory: Path, record: dict[str, Any]) -> dict[str, Any]:
    (run_directory / GUARD_FILENAME).write_text(
        json.dumps(record, indent=2) + "\n", encoding="utf-8"
    )
    return record


def load_guard(run_directory: Path) -> dict[str, Any] | None:
    path = run_directory / GUARD_FILENAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def guard_is_current(record: dict[str, Any] | None, diff_digest: str) -> bool:
    """A record is reused only for the same diff under the same outcome rules."""
    return (record is not None and record.get("diff_sha256") == diff_digest
            and record.get("version") == GUARD_VERSION)


def run_guard(
    run_directory: Path,
    *,
    diff: str,
    changed_paths: list[str],
    workspace: Path | None,
    base_commit: str | None,
    verification_command: list[str] | None,
    timeout_seconds: float = GUARD_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Run the added tests against base source; every outcome is written."""
    record: dict[str, Any] = {
        "version": GUARD_VERSION,
        "diff_sha256": diff_sha256(diff),
        "checked_at": datetime.now(UTC).isoformat(),
        "ran": False,
        "reason": None,
        "added": added_test_names(diff),
        "source_files": [],
        "command": None,
        "exit_code": None,
        "passed_at_base": [],
        "failed_at_base": [],
        "not_reported": [],
        "output_tail": "",
    }
    sources = [
        path for path in changed_paths
        if path.endswith(_SOURCE_SUFFIXES) and not _is_test_path(path)
    ]
    record["source_files"] = sources
    if not record["added"]:
        record["reason"] = "no-added-tests"
        return _write(run_directory, record)
    if not sources:
        record["reason"] = "no-source-change"
        return _write(run_directory, record)
    if workspace is None or not workspace.is_dir() or not base_commit:
        record["reason"] = "no-workspace"
        return _write(run_directory, record)
    command = _pytest_command(verification_command, environment_python(run_directory), workspace)
    if command is None:
        record["reason"] = "no-pytest"
        return _write(run_directory, record)
    wanted = {name for names in record["added"].values() for name in names}
    command = command + list(record["added"]) + ["-rA", "-k", " or ".join(sorted(wanted))]
    record["command"] = command

    saved: dict[str, bytes | None] = {}
    try:
        for path in sources:
            target = workspace / path
            saved[path] = target.read_bytes() if target.is_file() else None
            base = _base_bytes(workspace, base_commit, path)
            if base is None:
                target.unlink(missing_ok=True)
            else:
                target.write_bytes(base)
        result = execute(command, working_directory=workspace, timeout_seconds=timeout_seconds)
    finally:
        for path, content in saved.items():
            target = workspace / path
            if content is None:
                target.unlink(missing_ok=True)
            else:
                target.write_bytes(content)

    output = (result.stdout or "") + "\n" + (result.stderr or "")
    record["exit_code"] = result.exit_code
    record["output_tail"] = "\n".join(output.splitlines()[-40:])
    outcomes: dict[str, set[str]] = {}
    for outcome, node in _OUTCOME.findall(output):
        name = node.split("::")[-1].split("[", 1)[0]
        if name in wanted:
            outcomes.setdefault(name, set()).add(outcome)
    ordered = [name for names in record["added"].values() for name in names]
    # A parametrized test guards the fix when any case fails at base; a case
    # that passes there (a control) still lands in passed_at_base (#461).
    record["passed_at_base"] = [name for name in ordered if "PASSED" in outcomes.get(name, set())]
    record["failed_at_base"] = [
        name for name in ordered if outcomes.get(name, set()) & {"FAILED", "ERROR"}
    ]
    record["not_reported"] = [name for name in ordered if name not in outcomes]
    record["ran"] = bool(outcomes)
    if not outcomes:
        record["reason"] = "no-outcomes"
    return _write(run_directory, record)


def guard_findings(record: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Submission findings for a guard record; none when there was nothing to check."""
    if not record or record.get("reason") in ("no-added-tests", "no-source-change"):
        return []
    if not record.get("ran"):
        return [{
            "code": "guard-not-run",
            "blocking": False,
            "detail": (
                f"The added tests could not be run against base source ({record.get('reason')}). "
                "Check by hand that they fail without the fix."
            ),
        }]
    passed = record.get("passed_at_base") or []
    if not passed:
        return []
    if not record.get("failed_at_base"):
        return [{
            "code": "new-tests-pass-at-base",
            "blocking": True,
            "detail": (
                "Every test the diff adds passes without the source change, so none of them "
                f"guards the fix: {', '.join(passed)}. Make at least one fail at base."
            ),
        }]
    return [{
        "code": "unguarded-tests",
        "blocking": False,
        "detail": (
            f"These added tests pass without the source change: {', '.join(passed)}. "
            "Tighten them, or say in the body that they cover behaviour the fix keeps."
        ),
    }]
