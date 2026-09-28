"""Checks the target's own CI runs on a pull request, run before filing.

`prepare-submission` ran pytest only, so the first CI result on a filed pull
request could be a failure Mailman never looked for:

- pypdf#4105 failed "Check code style issues" 22 seconds after filing on a
  ruff finding (#120). The lint stage finds ruff, flake8, black, isort, mypy
  and ty in the target's configuration and CI, runs each over the changed
  Python files in the run environment, and blocks on a finding. A tool CI runs
  that cannot run here blocks too: securo#1039's CI ran ty, which this host
  cannot start, and the pull request's first CI run failed.
- edgartools#1365 failed `scripts/check_offline_audit.py` on a changed test
  file (#137). When the target ships that script, it runs on the changed test
  files and a non-zero exit blocks.

Each function returns `(record, findings)`; a finding is a dict with `code`,
`blocking` and `detail`, which `prepare-submission` turns into a `Finding`.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mailman.executor import execute
from mailman.touched_tests import environment_python

OFFLINE_AUDIT_SCRIPT = "scripts/check_offline_audit.py"
TARGET_CHECK_TIMEOUT_SECONDS = 10 * 60
INSTALL_TIMEOUT_SECONDS = 5 * 60


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


@dataclass(frozen=True)
class LintTool:
    """One linter or type checker a target's CI can run on a pull request.

    `invocation` is searched in workflows, `.pre-commit-config.yaml` and
    `tox.ini`, where the tool is run. `sections` are configuration headers in
    `pyproject.toml`, `setup.cfg` and `tox.ini`; `files` are the tool's own
    configuration files. `commands` are the arguments after
    `python -m <module>`; the changed files are appended to each.
    """

    name: str
    invocation: re.Pattern[str]
    pre_commit_repo: str
    sections: tuple[str, ...] = ()
    files: tuple[str, ...] = ()
    commands: tuple[tuple[str, ...], ...] = ((),)


# `--force-exclude` keeps ruff's own excludes for files named on the command
# line, which is what CI's `ruff check .` would honour.
_RUFF_OPTIONS = ("--no-cache", "--force-exclude")

LINT_TOOLS: tuple[LintTool, ...] = (
    LintTool(
        name="ruff",
        invocation=re.compile(r"\bruff\b"),
        pre_commit_repo=r"astral-sh/ruff-pre-commit",
        sections=("[tool.ruff",),
        files=("ruff.toml", ".ruff.toml"),
        commands=(("check", *_RUFF_OPTIONS),),
    ),
    LintTool(
        name="flake8",
        # `flake8-lazy` or `flake8-bugbear` is a plugin hook, not flake8 (#160).
        invocation=re.compile(r"(?<![\w-])flake8(?![\w-])"),
        pre_commit_repo=r"pycqa/flake8",
        sections=("[flake8]", "[tool.flake8"),
        files=(".flake8",),
    ),
    LintTool(
        name="black",
        invocation=re.compile(r"\bblack\b"),
        pre_commit_repo=r"psf/black(?:-pre-commit-mirror)?",
        sections=("[tool.black",),
        commands=(("--check", "--diff"),),
    ),
    LintTool(
        name="isort",
        invocation=re.compile(r"\bisort\b"),
        pre_commit_repo=r"pycqa/isort",
        sections=("[tool.isort", "[isort]"),
        files=(".isort.cfg",),
        commands=(("--check-only", "--diff"),),
    ),
    LintTool(
        name="mypy",
        invocation=re.compile(r"\bmypy\b"),
        pre_commit_repo=r"pre-commit/mirrors-mypy",
        sections=("[tool.mypy", "[mypy"),
        files=("mypy.ini", ".mypy.ini"),
    ),
    LintTool(
        name="ty",
        # A bare `\bty\b` matches too much prose; CI runs `ty check`.
        invocation=re.compile(r"\bty\s+check\b|astral-sh/ty-pre-commit|id:\s*ty\b"),
        pre_commit_repo=r"astral-sh/ty-pre-commit",
        sections=("[tool.ty",),
        files=("ty.toml",),
        commands=(("check",),),
    ),
)
LINT_TOOL_NAMES = tuple(tool.name for tool in LINT_TOOLS)
LINT_ACKNOWLEDGEMENT_FILENAME = "lint-acknowledgement.json"

_RUFF_FORMAT = re.compile(r"ruff[ -]format\b|id:\s*ruff-format\b")
_INVOKING_FILES = (".pre-commit-config.yaml", "tox.ini")
_CONFIGURING_FILES = ("pyproject.toml", "setup.cfg", "tox.ini")


def _lint_sources(workspace: Path) -> dict[str, str]:
    """The files where a target configures or runs its linters, by name."""
    names = ["pyproject.toml", "setup.cfg", *_INVOKING_FILES]
    names.extend(name for tool in LINT_TOOLS for name in tool.files)
    sources: dict[str, str] = {}
    for name in names:
        if (workspace / name).is_file():
            sources[name] = _read(workspace / name)
    workflows = workspace / ".github" / "workflows"
    if workflows.is_dir():
        for path in sorted(workflows.iterdir()):
            if path.suffix in (".yml", ".yaml"):
                sources[f".github/workflows/{path.name}"] = _read(path)
    return sources


_INLINE_DEPENDENCIES = re.compile(r"additional_dependencies:\s*\[[^\]]*\]")


def _without_hook_dependencies(text: str) -> str:
    """A pre-commit config minus every hook's `additional_dependencies`.

    A package a hook installs is not a tool CI runs: nilearn's blacken-docs
    hook lists `black` there while nilearn formats with ruff, and matching it
    ran black over the diff and blocked the run (#144).
    """
    text = _INLINE_DEPENDENCIES.sub("", text)
    kept: list[str] = []
    block_indent: int | None = None
    for line in text.splitlines():
        stripped = line.lstrip()
        indent = len(line) - len(stripped)
        if block_indent is not None:
            if not stripped or stripped.startswith("#") or indent > block_indent:
                continue
            if stripped.startswith("-") and indent == block_indent:
                continue
            block_indent = None
        if stripped.startswith("additional_dependencies:"):
            block_indent = indent
            continue
        kept.append(line)
    return "\n".join(kept)


def _mentions(tool: LintTool, name: str, text: str) -> bool:
    if name == ".pre-commit-config.yaml":
        text = _without_hook_dependencies(text)
    if name in tool.files:
        return True
    if name in _CONFIGURING_FILES:
        headers = [line.strip() for line in text.splitlines() if line.lstrip().startswith("[")]
        if any(header.startswith(section) for header in headers for section in tool.sections):
            return True
    if name.startswith(".github/workflows/") or name in _INVOKING_FILES:
        return tool.invocation.search(text) is not None
    return False


def _version(tool: LintTool, sources: dict[str, str]) -> str | None:
    """A pinned version from a pre-commit rev or a `tool==x` requirement."""
    pre_commit = re.compile(
        rf"{tool.pre_commit_repo}[^\n]*\n\s*rev:\s*['\"]?v?([0-9][\w.]*)", re.IGNORECASE
    )
    pinned = re.compile(rf"(?<![\w-]){re.escape(tool.name)}\s*==\s*([0-9][\w.]*)")
    for text in sources.values():
        # A pin can sit in a dependency group of a pyproject with no [tool.x].
        found = pre_commit.search(text) or pinned.search(text)
        if found:
            return found.group(1)
    return None


def lint_configurations(workspace: Path) -> list[dict[str, Any]]:
    """Every linter or type checker the target configures or runs in CI."""
    sources = _lint_sources(workspace)
    configurations: list[dict[str, Any]] = []
    for tool in LINT_TOOLS:
        found = [name for name, text in sources.items() if _mentions(tool, name, text)]
        if not found:
            continue
        configuration: dict[str, Any] = {
            "tool": tool.name,
            "sources": found,
            "version": _version(tool, sources),
        }
        if tool.name == "ruff":
            configuration["format"] = any(
                name != "pyproject.toml" and _RUFF_FORMAT.search(sources[name]) is not None
                for name in found
            )
        configurations.append(configuration)
    return configurations


def ruff_configuration(workspace: Path) -> dict[str, Any] | None:
    """Where the target configures or runs ruff, and the pinned version if any."""
    for configuration in lint_configurations(workspace):
        if configuration["tool"] == "ruff":
            return {key: configuration[key] for key in ("sources", "version", "format")}
    return None


def load_lint_acknowledgement(run_directory: Path, diff_sha256: str) -> dict[str, str]:
    """`{tool: note}` a human recorded for this exact diff, or nothing."""
    path = run_directory / LINT_ACKNOWLEDGEMENT_FILENAME
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict) or payload.get("diff_sha256") != diff_sha256:
        return {}
    note = str(payload.get("note") or "")
    return {str(tool): note for tool in payload.get("tools") or [] if note}


def record_lint_acknowledgement(
    run_directory: Path, *, tools: list[str], note: str, diff: str
) -> dict[str, Any]:
    """Record why a linter the target's CI runs cannot run here, for one diff.

    Same shape as the no-test acknowledgement: a note in writing, pinned to the
    exact diff, so a later export is not covered and this cannot become a
    standing waiver. It turns `lint-not-run` non-blocking, never `lint-failed`.
    """
    if not note.strip():
        raise ValueError("an acknowledgement needs a note saying why the tool cannot run")
    unknown = sorted(set(tools) - set(LINT_TOOL_NAMES))
    if not tools or unknown:
        raise ValueError(
            f"name one or more of {', '.join(LINT_TOOL_NAMES)}"
            + (f"; not a known tool: {', '.join(unknown)}" if unknown else "")
        )
    if not diff.strip():
        raise ValueError("the diff is empty, so there is nothing to acknowledge")
    record = {
        "schema_version": 1,
        "acknowledged_at": datetime.now(UTC).isoformat(),
        "note": note.strip(),
        "tools": sorted(set(tools)),
        "diff_sha256": hashlib.sha256(diff.encode("utf-8")).hexdigest(),
    }
    (run_directory / LINT_ACKNOWLEDGEMENT_FILENAME).write_text(
        json.dumps(record, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    return record


def _run_tool(
    tool: LintTool,
    configuration: dict[str, Any],
    *,
    python: str | None,
    workspace: Path,
    files: list[str],
    timeout_seconds: float,
) -> dict[str, Any]:
    """Probe, install if needed, and run one tool over the changed files."""
    entry: dict[str, Any] = {
        **configuration,
        "ran": False,
        "reason": None,
        "install": None,
        "commands": [],
        "results": [],
    }
    if python is None:
        entry["reason"] = "not-run: the run has no environment interpreter"
        return entry
    base = [python, "-m", tool.name]

    def probe():
        return execute(
            [*base, "--version"], working_directory=workspace, timeout_seconds=120
        )

    probed = probe()
    if probed.timed_out or probed.exit_code != 0:
        version = configuration.get("version")
        requirement = f"{tool.name}=={version}" if version else tool.name
        install = execute(
            [python, "-m", "pip", "install", "--disable-pip-version-check", requirement],
            working_directory=workspace,
            timeout_seconds=INSTALL_TIMEOUT_SECONDS,
        )
        entry["install"] = {
            "requirement": requirement,
            "exit_code": install.exit_code,
            "timed_out": install.timed_out,
            "output_tail": _tail(install.stdout + "\n" + install.stderr, 20),
        }
        if install.timed_out or install.exit_code != 0:
            entry["reason"] = f"not-run: `pip install {requirement}` exited {install.exit_code}"
            return entry
        # An installed tool can still refuse to start: Application Control
        # blocks ty.exe on this host.
        probed = probe()
        if probed.timed_out or probed.exit_code != 0:
            entry["reason"] = (
                f"not-run: `{tool.name} --version` exited {probed.exit_code} after "
                "install: " + _tail(probed.stdout + "\n" + probed.stderr, 3).strip()
            )
            return entry
    for arguments in tool.commands:
        commands = [list(arguments)]
        if tool.name == "ruff" and configuration.get("format") and arguments[0] == "check":
            commands.append(["format", "--check", *_RUFF_OPTIONS])
        for command_arguments in commands:
            command = [*base, *command_arguments, *files]
            result = execute(
                command, working_directory=workspace, timeout_seconds=timeout_seconds
            )
            entry["commands"].append(command)
            entry["results"].append(
                {
                    "command": command,
                    "label": " ".join([tool.name, *command_arguments[:1]]),
                    "exit_code": result.exit_code,
                    "timed_out": result.timed_out,
                    "output_tail": _tail(result.stdout + "\n" + result.stderr),
                }
            )
    entry["ran"] = True
    failed = any(r["timed_out"] or r["exit_code"] != 0 for r in entry["results"])
    entry["reason"] = "failed" if failed else "passed"
    return entry


def run_lint(
    run_directory: Path,
    *,
    workspace: Path | None,
    changed_paths: list[str],
    acknowledged: dict[str, str] | None = None,
    timeout_seconds: float = TARGET_CHECK_TIMEOUT_SECONDS,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Run the target's linters and type checkers over the changed files (#120).

    A tool the target configures or runs in CI that cannot run here is
    `lint-not-run` and blocks, unless `acknowledged` (read from
    `lint-acknowledgement.json`) holds a note for it. A target with no linter
    configured has no finding.
    """
    record: dict[str, Any] = {"ran": False, "reason": None, "files": [], "tools": []}
    if workspace is None or not workspace.is_dir():
        record["reason"] = "no-workspace"
        return record, []
    configurations = lint_configurations(workspace)
    if not configurations:
        record["reason"] = "no-linter-configured"
        return record, []
    record["tools"] = configurations
    files = [path for path in _existing(workspace, changed_paths) if path.endswith(".py")]
    record["files"] = files
    if not files:
        record["reason"] = "no-changed-python-files"
        return record, []

    acknowledged = acknowledged or {}
    python = environment_python(run_directory)
    tools = {tool.name: tool for tool in LINT_TOOLS}
    findings: list[dict[str, Any]] = []
    entries: list[dict[str, Any]] = []
    for configuration in configurations:
        tool = tools[configuration["tool"]]
        entry = _run_tool(
            tool,
            configuration,
            python=python,
            workspace=workspace,
            files=files,
            timeout_seconds=timeout_seconds,
        )
        entries.append(entry)
        if not entry["ran"]:
            note = acknowledged.get(tool.name)
            entry["acknowledged"] = bool(note)
            why = entry["reason"].removeprefix("not-run: ")
            findings.append(
                {
                    "code": "lint-not-run",
                    "blocking": not note,
                    "detail": (
                        f"the target runs {tool.name} "
                        f"({', '.join(configuration['sources'])}) and it did not "
                        f"run here: {why}. "
                        + (
                            f"Acknowledged in {LINT_ACKNOWLEDGEMENT_FILENAME}: {note}"
                            if note
                            else "CI would be the first to run it. Run it another "
                            "way, or record why with `mailman acknowledge-lint "
                            f"RUN_ID --tool {tool.name} --note ...`."
                        )
                    ),
                }
            )
            continue
        for result in entry["results"]:
            if result["timed_out"] or result["exit_code"] != 0:
                findings.append(
                    {
                        "code": "lint-failed",
                        "blocking": True,
                        "detail": (
                            f"`{result['label']}` exited {result['exit_code']} on "
                            f"the changed files; the target's CI runs {tool.name} "
                            "and will fail. " + _tail(result["output_tail"], 15)
                        ),
                    }
                )
    record["tools"] = entries
    record["ran"] = any(entry["ran"] for entry in entries)
    if any(finding["code"] == "lint-failed" for finding in findings):
        record["reason"] = "failed"
    elif not all(entry["ran"] for entry in entries):
        record["reason"] = "not-run"
    else:
        record["reason"] = "passed"
    return record, findings

__all__ = [
    "LINT_ACKNOWLEDGEMENT_FILENAME",
    "LINT_TOOLS",
    "LINT_TOOL_NAMES",
    "OFFLINE_AUDIT_SCRIPT",
    "lint_configurations",
    "load_lint_acknowledgement",
    "record_lint_acknowledgement",
    "ruff_configuration",
    "run_lint",
    "run_offline_audit",
]
