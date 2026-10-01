"""A target's own test suite at its default-branch head, before any change.

`mailman baseline OWNER/REPO` clones the head of the default branch, builds the
environment the target's CI builds (stepping down from the CI interpreter until
the install works), runs the whole suite under a timeout, and writes
`baseline.json`. Each failure is matched against the target's open issues, and
one an open issue already names is `known`, not a finding.

On 2026-09-06 `uv sync --all-extras` failed on Python 3.14 for want of an
aiohttp cp314 wheel and 3.13 worked; the suite then ran 8398 passed and 17
failed, and all 17 were one already-reported Windows symlink issue. That was
three hand steps an agent had to notice. Mailman #54.
"""
from __future__ import annotations

import json
import re
import shutil
import sys
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from mailman.environment import load_environment_record, load_plan, prepare_environment
from mailman.environment_plan import draft_plan
from mailman.executor import CommandResult, execute
from mailman.target_intel import _Gh, repository_slug
from mailman.touched_tests import (
    NO_CACHE,
    _UNKNOWN_CACHE_DIR,
    _scratch_cache,
    environment_python,
    parse_counts,
)
from mailman.workspace import WORKSPACE_DIRECTORY, prepare_workspace

BASELINE_FILENAME = "baseline.json"
BASELINE_SCHEMA_VERSION = 1
SUITE_TIMEOUT_SECONDS = 60 * 60
INSTALL_TIMEOUT_SECONDS = 30 * 60
CLONE_TIMEOUT_SECONDS = 10 * 60
#: Distinct test names searched one by one before the rest fall back to the
#: open-issue listing. GitHub's search budget is 30 calls a minute.
SEARCH_LIMIT = 20
#: Open-issue listing pages read when search is unavailable, 100 rows each.
LISTING_PAGES = 10

KNOWN = "known"
UNEXPLAINED = "unexplained"
UNCHECKED = "unchecked"

Version = tuple[int, int]


def baseline_directory(root: Path, slug: str, commit: str) -> Path:
    """Beside the run records, as hunts are: `.mailman/baselines/...`."""
    return root.parent / "baselines" / f"{slug.replace('/', '-')}-{commit[:12]}"


def _version_text(version: Version) -> str:
    return f"{version[0]}.{version[1]}"


# --- The base commit ---------------------------------------------------------


def resolve_head(
    repository_url: str,
    *,
    working_directory: Path,
    timeout_seconds: float = 120,
    run: Callable[..., CommandResult] = execute,
) -> str:
    """The commit the remote's default branch points at now."""
    result = run(
        ["git", "ls-remote", repository_url, "HEAD"],
        working_directory=working_directory,
        timeout_seconds=timeout_seconds,
    )
    found = re.match(r"([0-9a-f]{40}|[0-9a-f]{64})\s+HEAD\b", result.stdout or "")
    if result.timed_out or result.exit_code != 0 or not found:
        raise ValueError(
            f"could not read the default-branch head of {repository_url}: "
            f"exit {result.exit_code}, {(result.stderr or '').strip()[-300:]}"
        )
    return found.group(1)


# --- The interpreter ---------------------------------------------------------

_PYTHON_VERSION_KEY = re.compile(r"^(\s*)-?\s*python[-_]?versions?\s*:(.*)$", re.IGNORECASE)
_VERSION = re.compile(r"(?<![\d.])3\.(\d{1,2})(?![\d])")


def ci_python_versions(workspace: Path) -> dict[str, Any]:
    """The Python versions the target's GitHub workflows name.

    Reads `python-version:` values, inline lists and the `- "3.12"` items
    below the key. A `pypy` entry is not CPython and is skipped.
    """
    versions: set[Version] = set()
    sources: list[str] = []
    folder = workspace / ".github" / "workflows"
    for path in sorted(folder.glob("*.y*ml")) if folder.is_dir() else []:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        found: set[Version] = set()
        for index, line in enumerate(lines):
            key = _PYTHON_VERSION_KEY.match(line)
            if not key:
                continue
            values = [key.group(2)]
            indent = len(key.group(1))
            for following in lines[index + 1 :]:
                stripped = following.strip()
                if not stripped.startswith("- ") or len(following) - len(following.lstrip()) < indent:
                    break
                values.append(stripped)
            for value in values:
                if "pypy" in value.lower():
                    continue
                found.update((3, int(minor)) for minor in _VERSION.findall(value))
        if found:
            sources.append(f".github/workflows/{path.name}")
            versions |= found
    return {
        "versions": [_version_text(version) for version in sorted(versions)],
        "sources": sources,
    }


def requires_python_floor(workspace: Path) -> Version | None:
    """The lowest Python `requires-python` admits, when it says `>=3.X`."""
    source = workspace / "pyproject.toml"
    if not source.is_file():
        return None
    try:
        declared = tomllib.loads(source.read_text(encoding="utf-8")).get("project", {}).get("requires-python")
    except tomllib.TOMLDecodeError:
        return None
    found = re.search(r">=\s*3\.(\d+)", declared or "")
    return (3, int(found.group(1))) if found else None


_LAUNCHER_LINE = re.compile(r"^\s*-(?:V:)?(\S+)\s+(?:\*\s+)?(.+?)\s*$")


def parse_launcher_listing(text: str) -> dict[Version, str]:
    """Interpreters `py -0p` lists, first per version, free-threaded skipped."""
    found: dict[Version, str] = {}
    for line in text.splitlines():
        entry = _LAUNCHER_LINE.match(line)
        if not entry:
            continue
        version = re.search(r"(\d+)\.(\d+)(t?)", entry.group(1))
        if not version or version.group(3) or not entry.group(2).lower().endswith(".exe"):
            continue
        found.setdefault((int(version.group(1)), int(version.group(2))), entry.group(2))
    return found


def host_interpreters(
    *, working_directory: Path, run: Callable[..., CommandResult] = execute
) -> dict[Version, str]:
    """The CPython interpreters installed here, by minor version."""
    found: dict[Version, str] = {}
    if sys.platform == "win32" and shutil.which("py"):
        listing = run(["py", "-0p"], working_directory=working_directory, timeout_seconds=60)
        if listing.exit_code == 0:
            found.update(parse_launcher_listing(listing.stdout))
    else:
        for minor in range(8, 21):
            path = shutil.which(f"python3.{minor}")
            if path:
                found[(3, minor)] = path
    found.setdefault(sys.version_info[:2], sys.executable)
    return found


def candidate_versions(
    ci_versions: list[str], floor: Version | None, available: dict[Version, str]
) -> list[Version]:
    """The CI interpreter first, then each installed one below it, newest first.

    The CI interpreter is tried even when it is not installed, so the record
    says that is why the baseline used another. The step-down stops at the
    oldest version CI tests, or at `requires-python` when CI names none.
    """
    named = [tuple(int(part) for part in text.split(".")) for text in ci_versions]
    if named:
        start, low = max(named), min(named)
    elif available:
        start, low = max(available), floor or min(available)
    else:
        return []
    below = sorted((v for v in available if low <= v < start), reverse=True)
    return [start, *below]


def _failure_detail(record: dict[str, Any]) -> str:
    failed = next((step for step in record.get("steps", []) if not step.get("ok")), None)
    if failed is None:
        return str(record.get("detail") or "the environment was not built")
    command = failed.get("command") or {}
    output = (command.get("stderr") or "") + "\n" + (command.get("stdout") or "")
    tail = [line.strip() for line in output.splitlines() if line.strip()][-2:]
    reason = "timed out" if command.get("timed_out") else f"exit {command.get('exit_code')}"
    return f"step {failed['name']} failed ({reason}): {' / '.join(tail)}"[:600]


def build_environment(
    directory: Path,
    workspace: Path,
    *,
    ci: dict[str, Any],
    floor: Version | None,
    interpreters: dict[Version, str],
    timeout_seconds: float = INSTALL_TIMEOUT_SECONDS,
    prepare: Callable[..., dict[str, Any]] = prepare_environment,
    draft: Callable[..., dict[str, Any]] = draft_plan,
    announce: Callable[[str], None] = lambda message: None,
) -> dict[str, Any]:
    """Build an environment, stepping down from the CI interpreter until one installs.

    Each attempt lives in `python-X.Y/` with its own drafted plan. A plan
    already there is reused, so an operator can edit one and rerun.
    """
    attempts: list[dict[str, Any]] = []
    used: dict[str, Any] | None = None
    for version in candidate_versions(ci["versions"], floor, interpreters):
        text = _version_text(version)
        executable = interpreters.get(version)
        if executable is None:
            attempts.append(
                {"python": text, "installed": False, "success": False,
                 "detail": "not installed on this host"}
            )
            announce(f"skip python {text}: not installed on this host")
            continue
        attempt = directory / f"python-{text}"
        attempt.mkdir(parents=True, exist_ok=True)
        previous = load_environment_record(attempt)
        if previous and previous.get("success"):
            record, reused = previous, True
        else:
            plan_path = attempt / "environment-plan.json"
            if not plan_path.is_file():
                draft(workspace, plan_path, python=executable)
            announce(f"build python {text}: {executable}")
            record = prepare(
                attempt, workspace=workspace, plan=load_plan(plan_path),
                timeout_seconds=timeout_seconds, announce=announce,
            )
            reused = False
        row = {
            "python": text,
            "installed": True,
            "executable": executable,
            "directory": str(attempt),
            "reused": reused,
            "success": bool(record.get("success")),
            "detail": "installed" if record.get("success") else _failure_detail(record),
        }
        attempts.append(row)
        if row["success"]:
            used = row
            break
    ci_interpreter = ci["versions"][-1] if ci["versions"] else None
    result: dict[str, Any] = {
        "ci_versions": ci["versions"],
        "ci_sources": ci["sources"],
        "ci_interpreter": ci_interpreter,
        "requires_python_floor": _version_text(floor) if floor else None,
        "attempts": attempts,
        "used": used["python"] if used else None,
        "executable": environment_python(Path(used["directory"])) if used else None,
        "directory": used["directory"] if used else None,
        "differs_from_ci": bool(used) and used["python"] != ci_interpreter,
        "reason": None,
    }
    tried = "; ".join(f"Python {row['python']}: {row['detail']}" for row in attempts if not row["success"])
    if used is None:
        result["reason"] = f"no interpreter built the environment: {tried or 'none to try'}"
    elif ci_interpreter is None:
        result["reason"] = f"the workflows name no Python version; used the newest installed, {used['python']}"
    elif result["differs_from_ci"]:
        result["reason"] = tried
    return result


# --- The suite ---------------------------------------------------------------

_SUMMARY_NODE = re.compile(r"^(FAILED|ERROR) (\S+?)(?: - (.*))?$", re.MULTILINE)


def suite_failures(output: str) -> list[dict[str, Any]]:
    """Each failed or errored node from pytest's `-rfE` short summary."""
    found: dict[str, dict[str, Any]] = {}
    for kind, node, message in _SUMMARY_NODE.findall(output):
        found.setdefault(
            node,
            {"nodeid": node, "kind": "failed" if kind == "FAILED" else "error",
             "message": (message or "").strip()[:300]},
        )
    return list(found.values())


def run_suite(
    directory: Path,
    workspace: Path,
    python: str,
    *,
    timeout_seconds: float = SUITE_TIMEOUT_SECONDS,
    run: Callable[..., CommandResult] = execute,
    on_line: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """The whole suite once, as the target's CI would, under a timeout."""
    cache: tuple[str, ...] = NO_CACHE
    while True:
        command = [python, "-m", "pytest", "-q", "-rfE", *cache]
        result = run(
            command, working_directory=workspace, timeout_seconds=timeout_seconds,
            **({"on_stdout_line": on_line} if on_line else {}),
        )
        output = (result.stdout or "") + "\n" + (result.stderr or "")
        # A project that sets cache_dir under --strict-config exits 4 once
        # the cache plugin is off. Mailman #297.
        if cache == NO_CACHE and result.exit_code == 4 and _UNKNOWN_CACHE_DIR in output:
            cache = _scratch_cache(directory)
            continue
        break
    output_path = directory / "suite-output.txt"
    output_path.write_text(output, encoding="utf-8")
    counts = parse_counts("pytest", result.stdout or "", result.stderr or "")
    return {
        "command": list(command),
        "exit_code": result.exit_code,
        "timed_out": result.timed_out,
        "timeout_seconds": timeout_seconds,
        "duration_seconds": result.duration_seconds,
        "output": str(output_path),
        "counts": counts,
        "failures": suite_failures(output),
        # Exit 0 and 1 are a finished run; anything else is a collection,
        # usage or internal error, and counts from it are not a baseline.
        "completed": not result.timed_out and result.exit_code in (0, 1)
        and counts["passed"] is not None,
    }


# --- Matching failures to open issues ----------------------------------------


def test_name(nodeid: str) -> str:
    """The name an issue would quote: the test function, or a module file."""
    if "::" in nodeid:
        return nodeid.rsplit("::", 1)[1].split("[", 1)[0]
    return nodeid.replace("\\", "/").rsplit("/", 1)[-1]


def _mentions(row: dict[str, Any], name: str) -> bool:
    text = f"{row.get('title') or ''}\n{row.get('body') or ''}"
    return re.search(rf"(?<![\w.]){re.escape(name)}(?![\w])", text) is not None


def _issue(row: dict[str, Any], name: str, nodeid: str, method: str) -> dict[str, Any]:
    return {
        "number": row.get("number"),
        "title": row.get("title"),
        "url": row.get("html_url"),
        "matched_on": name,
        # The whole node id in the text, not only a test name that another
        # module could share.
        "names_nodeid": _mentions(row, nodeid),
        "found_by": method,
    }


def match_failures(
    failures: list[dict[str, Any]], slug: str, gh: _Gh
) -> dict[str, Any]:
    """Mark each failure `known`, `unexplained` or `unchecked`, in place.

    A read-only search per distinct test name first. Names past SEARCH_LIMIT,
    or whose search failed, are matched against a listing of open issues
    instead. Only when both readings fail is a failure `unchecked`: the lookup
    did not happen, which is not the same as finding no issue. An issue
    quoting the node id also quotes its test name, so the name is the match.
    """
    names = list(dict.fromkeys(test_name(failure["nodeid"]) for failure in failures))
    found: dict[str, list[dict[str, Any]]] = {}
    searched: list[str] = []
    unresolved: list[str] = []
    for index, name in enumerate(names):
        if index < SEARCH_LIMIT:
            query = quote(f'repo:{slug} is:issue is:open "{name}"')
            result = gh.json(f"search/issues?q={query}&per_page=20")
            items = result.get("items") if isinstance(result, dict) else None
            if isinstance(items, list):
                found[name] = [row for row in items if isinstance(row, dict) and _mentions(row, name)]
                searched.append(name)
                continue
        unresolved.append(name)
    listed: int | None = None
    if unresolved:
        listing = gh.every_page(f"repos/{slug}/issues?state=open", pages=LISTING_PAGES)
        if listing is not None:
            rows = [row for row in listing if isinstance(row, dict) and "pull_request" not in row]
            listed = len(rows)
            for name in unresolved:
                found[name] = [row for row in rows if _mentions(row, name)]
    for failure in failures:
        name = test_name(failure["nodeid"])
        method = "search" if name in searched else "listing"
        rows = found.get(name)
        failure["issues"] = [
            _issue(row, name, failure["nodeid"], method) for row in rows or []
        ]
        if rows is None:
            failure["status"] = UNCHECKED
        else:
            failure["status"] = KNOWN if rows else UNEXPLAINED
    return {
        "searched": searched,
        "listed_after_search": unresolved,
        "listed_issues": listed,
        "unread": list(gh.failures),
        "rate_limited": gh.rate_limited,
    }


# --- The record --------------------------------------------------------------


def record_baseline(
    repository: str,
    *,
    root: Path,
    commit: str | None = None,
    gh_executable: str = "gh",
    clone_timeout_seconds: float = CLONE_TIMEOUT_SECONDS,
    install_timeout_seconds: float = INSTALL_TIMEOUT_SECONDS,
    suite_timeout_seconds: float = SUITE_TIMEOUT_SECONDS,
    run: Callable[..., CommandResult] = execute,
    clone: Callable[..., dict[str, Any]] = prepare_workspace,
    interpreters: dict[Version, str] | None = None,
    build: Callable[..., dict[str, Any]] = build_environment,
    suite: Callable[..., dict[str, Any]] = run_suite,
    announce: Callable[[str], None] = lambda message: None,
) -> dict[str, Any]:
    """Clone, build, run and match; write and return `baseline.json`."""
    slug = repository_slug(repository)
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", slug):
        raise ValueError("repository must be OWNER/REPO or a GitHub URL")
    url = f"https://github.com/{slug}.git"
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    source = "given"
    if commit is None:
        announce(f"read the default-branch head of {slug}")
        commit = resolve_head(url, working_directory=root, run=run)
        source = "default-branch head (git ls-remote HEAD)"
    commit = commit.lower()
    directory = baseline_directory(root, slug, commit)
    directory.mkdir(parents=True, exist_ok=True)
    record: dict[str, Any] = {
        "schema_version": BASELINE_SCHEMA_VERSION,
        "repository": slug,
        "repository_url": url,
        "base_commit": commit,
        "base_commit_source": source,
        "started_at": datetime.now(UTC).isoformat(),
        "directory": str(directory),
        "success": False,
    }

    def finish(detail: str | None = None) -> dict[str, Any]:
        if detail:
            record["detail"] = detail
        record["finished_at"] = datetime.now(UTC).isoformat()
        destination = directory / BASELINE_FILENAME
        temporary = destination.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        temporary.replace(destination)
        record["path"] = str(destination)
        return record

    announce(f"clone {slug} at {commit}")
    workspace_record = clone(
        repository=url, base_commit=commit, run_directory=directory,
        timeout_seconds=clone_timeout_seconds,
    )
    workspace = directory / WORKSPACE_DIRECTORY
    record["workspace"] = str(workspace)
    if not workspace_record.get("success"):
        return finish(f"the clone failed: {workspace_record.get('detail') or 'see workspace.json'}")

    found = interpreters if interpreters is not None else host_interpreters(
        working_directory=directory, run=run
    )
    record["interpreter"] = build(
        directory, workspace,
        ci=ci_python_versions(workspace),
        floor=requires_python_floor(workspace),
        interpreters=found,
        timeout_seconds=install_timeout_seconds,
        announce=announce,
    )
    python = record["interpreter"].get("executable")
    if not python:
        return finish(record["interpreter"].get("reason") or "no environment was built")

    announce(f"run the suite with {python}, timeout {suite_timeout_seconds:.0f}s")
    ran = suite(directory, workspace, python, timeout_seconds=suite_timeout_seconds)
    failures = ran.pop("failures")
    record["suite"] = ran
    record.update({key: ran["counts"].get(key) for key in ("passed", "failed", "errors", "skipped")})
    record["failures"] = failures
    if not ran["completed"]:
        reason = "timed out" if ran["timed_out"] else f"exited {ran['exit_code']}"
        return finish(f"the suite did not finish: it {reason}; see {ran['output']}")

    if failures:
        announce(f"match {len(failures)} failure(s) against open issues of {slug}")
        gh = _Gh(gh_executable, directory, 60, run=run)
        record["issue_lookup"] = match_failures(failures, slug, gh)
    statuses = [failure.get("status") for failure in failures]
    record["known"] = statuses.count(KNOWN)
    record["unexplained"] = statuses.count(UNEXPLAINED)
    record["unchecked"] = statuses.count(UNCHECKED)
    record["success"] = True
    return finish(
        f"{record['passed']} passed, {record['failed']} failed, {record['errors']} errors; "
        f"{record['known']} known, {record['unexplained']} unexplained, "
        f"{record['unchecked']} unchecked"
    )


def load_baseline(directory: Path) -> dict[str, Any] | None:
    path = directory / BASELINE_FILENAME
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else None
