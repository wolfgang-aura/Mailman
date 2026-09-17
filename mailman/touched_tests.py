"""Run every test file that exercises a module the diff touches.

edgartools#1329 failed CI on `tests/xbrl/test_statement_drilldown.py`, a file
the primary never ran. It imports `edgar.xbrl.xbrl`, the module the diff
changed, and would have failed locally in under a second. The primary's
focused check and Mailman's recorded verification command both covered only
the tests the primary chose. This stage chooses by the diff instead: every
test file whose text imports or names a touched module runs in the run's own
environment before `handoff-check` can pass.
See https://github.com/wolfgang-aura/Mailman/issues/115.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mailman.environment import ENVIRONMENT_DIRECTORY
from mailman.executor import CommandResult, execute
from mailman.toolchain import toolchain_executable

TOUCHED_TESTS_FILENAME = "touched-tests.json"
TOUCHED_TESTS_SCHEMA_VERSION = 1
#: More than this and the stage is no longer a focused check; the record says
#: which files were left out so the reader can widen it by hand.
TOUCHED_TESTS_CAP = 25
TOUCHED_TESTS_TIMEOUT_SECONDS = 20 * 60
PROBE_TIMEOUT_SECONDS = 120

#: Layout prefixes that are not part of the import path.
_LAYOUT_PREFIXES = ("src", "lib", "python")
_SKIPPED_DIRECTORIES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".tox",
        ".nox",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        "build",
        "dist",
        ".eggs",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        ENVIRONMENT_DIRECTORY,
    }
)
#: Nothing-to-run outcomes. The stage ran and has nothing to report on, which
#: is not the same as never having run.
_NOTHING_TO_RUN = frozenset({"no-source-change", "no-matching-tests"})


def diff_sha256(diff: str) -> str:
    return hashlib.sha256(diff.encode("utf-8")).hexdigest()


def _is_test_path(path: str) -> bool:
    from mailman.submission import _is_test_path as is_test_path

    return is_test_path(path)


def module_names(path: str) -> list[str]:
    """The names a test could import or mention to reach this file.

    `edgar/xbrl/xbrl.py` gives `edgar.xbrl.xbrl`, its package `edgar.xbrl`,
    and the bare stem `xbrl`. The top-level package alone is left out: every
    test in the repository imports it, which is a full run, not a focused one.
    """
    normalized = path.replace("\\", "/")
    if not normalized.endswith(".py"):
        return []
    segments = normalized[: -len(".py")].split("/")
    while len(segments) > 1 and segments[0] in _LAYOUT_PREFIXES:
        segments = segments[1:]
    if segments[-1] == "__init__":
        segments = segments[:-1]
    if not segments or not all(re.fullmatch(r"\w+", segment) for segment in segments):
        return []
    names: list[str] = []
    dotted = ".".join(segments)
    names.append(dotted)
    # The module's own package, never its grandparents: for
    # `nicegui/elements/leaflet/leaflet.py`, `nicegui.elements` is imported by
    # most of the test suite, and matching it ran eight unrelated files and
    # not the one named after the module.
    if len(segments) > 2:
        names.append(".".join(segments[:-1]))
    stem = segments[-1]
    if stem not in names:
        names.append(stem)
    return names


_OPTIONAL_LAYOUT = "(?:(?:" + "|".join(_LAYOUT_PREFIXES) + r")\.)?"


def _reference_pattern(name: str) -> re.Pattern[str]:
    escaped = _OPTIONAL_LAYOUT + re.escape(name)
    if "." in name:
        return re.compile(
            rf"(?m)^\s*(?:from\s+{escaped}\b|import\s+{escaped}\b)"
            rf"|[\"']{escaped}(?:\.[\w.]*)?[\"']"
        )
    # A bare stem: `import x`, `from x import`, or `from pkg import ..., x`.
    return re.compile(
        rf"(?m)^\s*(?:from\s+{escaped}\b|import\s+{escaped}\b"
        rf"|from\s+[\w.]+\s+import\b[^\n]*\b{re.escape(name)}\b)"
    )


def _test_files(workspace: Path) -> list[str]:
    """Every test file in the workspace, as a `/`-separated relative path."""
    found: list[str] = []
    for root, directories, files in os.walk(workspace):
        # A `testing` directory inside a package is library code, not a test
        # tree: `nicegui/testing/user_interaction.py` is the User fixture,
        # and collecting it as a test file made the stage exit 2.
        directories[:] = sorted(
            name
            for name in directories
            if name not in _SKIPPED_DIRECTORIES
            and not (name == "testing" and (Path(root) / "__init__.py").is_file())
        )
        relative_root = Path(root).relative_to(workspace)
        for name in sorted(files):
            if not name.endswith(".py"):
                continue
            relative = (relative_root / name).as_posix()
            if relative.startswith("./"):
                relative = relative[2:]
            if _is_test_path(relative):
                found.append(relative)
    return found


def select_test_files(
    workspace: Path, changed_paths: list[str], *, cap: int = TOUCHED_TESTS_CAP
) -> dict[str, Any]:
    """Pick the test files whose text imports or names a touched module."""
    source_files = [
        path
        for path in changed_paths
        if not _is_test_path(path) and path.replace("\\", "/").endswith(".py")
    ]
    modules = {path: module_names(path) for path in source_files}
    patterns = {
        name: _reference_pattern(name)
        for names in modules.values()
        for name in names
    }
    selected: list[dict[str, Any]] = []
    if patterns:
        for relative in _test_files(workspace):
            try:
                text = (workspace / relative).read_text(
                    encoding="utf-8", errors="replace"
                )
            except OSError:
                continue
            matched = [name for name, pattern in patterns.items() if pattern.search(text)]
            if matched:
                selected.append(
                    {
                        "path": relative,
                        "matched": matched,
                        "reason": "imports or names " + ", ".join(matched),
                    }
                )
    capped = len(selected) > cap
    omitted = [entry["path"] for entry in selected[cap:]] if capped else []
    return {
        "source_files": source_files,
        "modules": modules,
        "candidates": len(selected),
        "selected": selected[:cap],
        "capped": capped,
        "cap": cap,
        "omitted": omitted,
    }


def environment_python(run_directory: Path) -> str | None:
    """The interpreter `prepare-environment` registered, or the venv's own."""
    try:
        pinned = toolchain_executable(run_directory, "python")
    except (OSError, ValueError):
        pinned = None
    if pinned:
        return pinned
    environment = run_directory / ENVIRONMENT_DIRECTORY
    for candidate in (
        environment / "Scripts" / "python.exe",
        environment / "bin" / "python",
    ):
        if candidate.is_file():
            return str(candidate)
    return None


_PYTEST_COUNTS = {
    "passed": re.compile(r"(\d+) passed"),
    "failed": re.compile(r"(\d+) failed"),
    "errors": re.compile(r"(\d+) errors?\b"),
    "skipped": re.compile(r"(\d+) skipped"),
}
_UNITTEST_RAN = re.compile(r"^Ran (\d+) tests? in", re.M)
_UNITTEST_FAILURES = re.compile(r"failures=(\d+)")
_UNITTEST_ERRORS = re.compile(r"errors=(\d+)")
_UNITTEST_SKIPPED = re.compile(r"skipped=(\d+)")


def parse_counts(runner: str, stdout: str, stderr: str) -> dict[str, int | None]:
    """Read passed/failed/errors/skipped out of a runner's summary line."""
    counts: dict[str, int | None] = {
        "passed": None,
        "failed": None,
        "errors": None,
        "skipped": None,
    }
    if runner == "pytest":
        summary = None
        for line in reversed((stdout + "\n" + stderr).splitlines()):
            if re.search(r"\d+ (passed|failed|error)", line):
                summary = line
                break
        if summary is None:
            return counts
        for key, pattern in _PYTEST_COUNTS.items():
            found = pattern.search(summary)
            counts[key] = int(found.group(1)) if found else 0
        return counts
    text = stdout + "\n" + stderr
    ran = _UNITTEST_RAN.search(text)
    if ran is None:
        return counts
    failures = _UNITTEST_FAILURES.search(text)
    errors = _UNITTEST_ERRORS.search(text)
    skipped = _UNITTEST_SKIPPED.search(text)
    counts["failed"] = int(failures.group(1)) if failures else 0
    counts["errors"] = int(errors.group(1)) if errors else 0
    counts["skipped"] = int(skipped.group(1)) if skipped else 0
    counts["passed"] = (
        int(ran.group(1)) - counts["failed"] - counts["errors"] - counts["skipped"]
    )
    return counts


def _tail(text: str, lines: int = 60) -> str:
    return "\n".join(text.splitlines()[-lines:])


def _write(run_directory: Path, record: dict[str, Any]) -> dict[str, Any]:
    (run_directory / TOUCHED_TESTS_FILENAME).write_text(
        json.dumps(record, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    return record


def load_touched_tests(run_directory: Path) -> dict[str, Any] | None:
    path = run_directory / TOUCHED_TESTS_FILENAME
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def run_touched_tests(
    run_directory: Path,
    *,
    diff: str,
    changed_paths: list[str],
    workspace: Path | None,
    timeout_seconds: float = TOUCHED_TESTS_TIMEOUT_SECONDS,
    cap: int = TOUCHED_TESTS_CAP,
) -> dict[str, Any]:
    """Select, run and record. Every outcome is written, including not running."""
    started = datetime.now(UTC)
    record: dict[str, Any] = {
        "schema_version": TOUCHED_TESTS_SCHEMA_VERSION,
        "diff_sha256": diff_sha256(diff),
        "started_at": started.isoformat(),
        "ran": False,
        "reason": None,
        "workspace": str(workspace) if workspace else None,
        "python": None,
        "runner": None,
        "source_files": [],
        "modules": {},
        "candidates": 0,
        "selected": [],
        "capped": False,
        "cap": cap,
        "omitted": [],
        "command": None,
        "exit_code": None,
        "timed_out": False,
        "duration_seconds": 0.0,
        "passed": None,
        "failed": None,
        "errors": None,
        "skipped": None,
        "output_tail": "",
    }
    if workspace is None or not workspace.is_dir():
        record["reason"] = "no-workspace"
        return _write(run_directory, record)
    selection = select_test_files(workspace, changed_paths, cap=cap)
    record.update(selection)
    if not selection["source_files"]:
        record["ran"] = True
        record["reason"] = "no-source-change"
        return _write(run_directory, record)
    if not selection["selected"]:
        record["ran"] = True
        record["reason"] = "no-matching-tests"
        return _write(run_directory, record)
    python = environment_python(run_directory)
    if python is None:
        record["reason"] = "no-environment-python"
        return _write(run_directory, record)
    record["python"] = python
    probe: CommandResult = execute(
        [python, "-c", "import pytest"],
        working_directory=workspace,
        timeout_seconds=PROBE_TIMEOUT_SECONDS,
    )
    runner = "pytest" if probe.exit_code == 0 and not probe.timed_out else "unittest"
    files = [entry["path"] for entry in selection["selected"]]
    if runner == "pytest":
        command = [python, "-m", "pytest", *files, "-q", "-p", "no:cacheprovider"]
    else:
        command = [python, "-m", "unittest", *files]
    record["runner"] = runner
    record["command"] = command
    result: CommandResult = execute(
        command, working_directory=workspace, timeout_seconds=timeout_seconds
    )
    record["ran"] = True
    record["exit_code"] = result.exit_code
    record["timed_out"] = result.timed_out
    record["duration_seconds"] = result.duration_seconds
    record.update(parse_counts(runner, result.stdout, result.stderr))
    record["output_tail"] = _tail(result.stdout + "\n" + result.stderr)
    return _write(run_directory, record)


def touched_tests_verdict(
    record: dict[str, Any] | None, *, expected_diff_sha256: str | None = None
) -> tuple[str | None, str]:
    """`(code, detail)` for a record; the code is None when the stage passed.

    `touched-tests-not-run` means nobody has the evidence yet; a record for
    another diff is as good as none. `touched-tests-failed` means the evidence
    exists and says no.
    """
    if record is None:
        return (
            "touched-tests-not-run",
            "the tests that exercise the changed modules have not been run for "
            "this export. Run `mailman prepare-submission` with the run's "
            "workspace and environment in place.",
        )
    if expected_diff_sha256 and record.get("diff_sha256") != expected_diff_sha256:
        return (
            "touched-tests-not-run",
            "the touched-tests record belongs to an earlier export; run "
            "`mailman prepare-submission` again for the current diff.",
        )
    if not record.get("ran"):
        reason = record.get("reason") or "unknown"
        return (
            "touched-tests-not-run",
            f"the touched-tests stage did not run ({reason}). It needs the run's "
            "workspace and the interpreter `prepare-environment` registered.",
        )
    if record.get("reason") in _NOTHING_TO_RUN:
        return None, f"nothing to run: {record['reason']}"
    if record.get("timed_out"):
        return (
            "touched-tests-failed",
            f"the touched tests timed out after {record.get('duration_seconds')} s: "
            f"{' '.join(record.get('command') or [])}",
        )
    if record.get("exit_code") != 0:
        return (
            "touched-tests-failed",
            "the tests that exercise the changed modules failed "
            f"(exit {record.get('exit_code')}, passed {record.get('passed')}, "
            f"failed {record.get('failed')}, errors {record.get('errors')}): "
            f"{' '.join(record.get('command') or [])}",
        )
    return (
        None,
        f"{len(record.get('selected') or [])} test file(s) ran, "
        f"passed {record.get('passed')}, failed {record.get('failed')}",
    )


def resolve_workspace(run_directory: Path, given: Path | None) -> Path | None:
    """The workspace the export came from, or the one `prepare-workspace` made."""
    if given is not None:
        return given
    export = run_directory / "export" / "export.json"
    if export.is_file():
        try:
            payload = json.loads(export.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        recorded = payload.get("workspace") if isinstance(payload, dict) else None
        if isinstance(recorded, str) and Path(recorded).is_dir():
            return Path(recorded)
    prepared = run_directory / "workspace"
    return prepared if prepared.is_dir() else None


__all__ = [
    "TOUCHED_TESTS_CAP",
    "TOUCHED_TESTS_FILENAME",
    "TOUCHED_TESTS_TIMEOUT_SECONDS",
    "diff_sha256",
    "environment_python",
    "load_touched_tests",
    "module_names",
    "parse_counts",
    "resolve_workspace",
    "run_touched_tests",
    "select_test_files",
    "touched_tests_verdict",
]
