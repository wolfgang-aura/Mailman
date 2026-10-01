"""Checks the target's own CI runs on a pull request, run before filing.

`prepare-submission` ran pytest only, so the first CI result on a filed pull
request could be a failure Mailman never looked for:

- pypdf#4105 failed "Check code style issues" 22 seconds after filing on a
  ruff finding (#120). The lint stage finds ruff, flake8, black, isort, mypy,
  pylint and ty in the target's configuration and CI, runs each over the changed
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

import configparser
import hashlib
import json
import re
import textwrap
import tomllib
from collections import Counter
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
        name="pylint",
        # Found only where it runs. A leftover `[tool.pylint]` or `.pylintrc`
        # in a project whose CI never runs pylint would block a clean patch on
        # a missing docstring. `pylint-django` is a plugin, not pylint.
        invocation=re.compile(r"(?<![\w-])pylint(?![\w-])"),
        pre_commit_repo=r"(?:pylint-dev|pycqa)/pylint",
        # `--score=n` drops the rating line, whose "previous run" suffix
        # differs between the patched and the base run; `--persistent=n`
        # keeps pylint's stats out of the user's profile.
        commands=(("--persistent=n", "--score=n"),),
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
    LintTool(
        name="pyright",
        invocation=re.compile(r"(?<![\w-])pyright(?![\w-])"),
        pre_commit_repo=r"RobertCraigie/pyright-python",
        sections=("[tool.pyright",),
        files=("pyrightconfig.json",),
    ),
    LintTool(
        name="pyrefly",
        invocation=re.compile(r"(?<![\w-])pyrefly\s+check|facebook/pyrefly-pre-commit|id:\s*pyrefly"),
        pre_commit_repo=r"facebook/pyrefly-pre-commit",
        sections=("[tool.pyrefly",),
        files=("pyrefly.toml",),
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
        # pandas-stubs runs every checker as a poe task of the same name (#305).
        if f"[tool.poe.tasks.{tool.name}]" in headers:
            return True
    if name.startswith(".github/workflows/") or name in _INVOKING_FILES:
        if re.search(rf"poe\s+{re.escape(tool.name)}(?![\w-])", text):
            return True
        return tool.invocation.search(text) is not None
    return False


def _version(tool: LintTool, sources: dict[str, str]) -> str | None:
    """A pinned version from a pre-commit rev or a `tool==x` requirement."""
    pre_commit = re.compile(
        rf"{tool.pre_commit_repo}[^\n]*\n\s*rev:\s*['\"]?([^\s'\"#]+)['\"]?"
        r"(?:[ \t]*#[ \t]*frozen:[ \t]*v?([0-9][\w.]*))?",
        re.IGNORECASE,
    )
    # `==1.1.*` keeps its wildcard; cut to `1.1.` it is no requirement pip
    # accepts. Mailman #361.
    pinned = re.compile(rf"(?<![\w-]){re.escape(tool.name)}\s*==\s*([0-9][\w.]*\*?)")
    # CI's pre-commit rev outranks a dev-group pin in any file: prefect's CI
    # runs ruff v0.15.19 while its dev group pins 0.16.2. Mailman #247.
    for text in sources.values():
        found = pre_commit.search(text)
        if not found:
            continue
        rev = found.group(1)
        # typeshed pins `rev: <sha> # frozen: 26.5.1`; the SHA is no
        # version pip can install (#310).
        if re.fullmatch(r"[0-9a-f]{40}", rev, re.IGNORECASE):
            if found.group(2):
                return found.group(2)
            continue
        version = re.match(r"v?([0-9][\w.]*)", rev)
        if version:
            return version.group(1)
    for text in sources.values():
        # A pin can sit in a dependency group of a pyproject with no [tool.x].
        found = pinned.search(text)
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
    if (probed.timed_out or probed.exit_code != 0) and _environment_hook(workspace, tool):
        entry["reason"] = (
            f"not-run: the target's pre-commit runs {tool.name} as a `repo: local` "
            f"hook from its own environment, and `{tool.name} --version` exited "
            f"{probed.exit_code} in the run environment. Install the target's lint "
            f"dependencies into the run environment; another {tool.name} could "
            "disagree with CI"
        )
        return entry
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
    environment: dict[str, str] | None = None
    pinned = configuration.get("version")
    found = re.search(r"\d+(?:\.\d+)+", probed.stdout)
    wildcard = bool(pinned) and pinned.endswith(".*")
    inside = bool(pinned and found) and (
        found.group(0) == pinned
        or (wildcard and found.group(0).startswith(pinned[:-1]))
    )
    if pinned and found and not inside:
        # A newer tool enables rules the target's CI never runs: the lockfile's
        # ruff 0.16.2 flagged UP007 where prefect's pinned 0.15.19 passed.
        # The pin goes beside the environment, not into it. Mailman #247.
        folder = pinned.replace("*", "x")
        tools = Path(python).parent.parent.parent / "lint-tools" / f"{tool.name}-{folder}"
        requirement = f"{tool.name}=={pinned}"
        install = execute(
            [python, "-m", "pip", "install", "--disable-pip-version-check",
             "--target", str(tools), requirement],
            working_directory=workspace,
            timeout_seconds=INSTALL_TIMEOUT_SECONDS,
        )
        entry["install"] = {
            "requirement": requirement,
            "replaces": found.group(0),
            "exit_code": install.exit_code,
            "timed_out": install.timed_out,
            "output_tail": _tail(install.stdout + "\n" + install.stderr, 20),
        }
        if install.timed_out or install.exit_code != 0:
            entry["reason"] = (
                f"not-run: the target pins {requirement}, the environment has "
                f"{found.group(0)}, and `pip install --target` exited {install.exit_code}"
            )
            return entry
        environment = {"PYTHONPATH": str(tools)}
        # `python -m ruff` looks for the venv's ruff.exe before the --target
        # folder's bin/, so PYTHONPATH alone still ran 0.16.2. A tool that
        # ships a binary is called by that binary.
        binaries = [tools / "bin" / f"{tool.name}{suffix}" for suffix in (".exe", "")]
        binary = next((path for path in binaries if path.is_file()), None)
        if binary is not None:
            base = [str(binary)]
        probed = execute(
            [*base, "--version"], working_directory=workspace,
            timeout_seconds=120, environment=environment,
        )
        if (pinned[:-1] if wildcard else pinned) not in probed.stdout:
            entry["reason"] = (
                f"not-run: installed {requirement} beside the environment, but "
                f"`{tool.name} --version` reports: "
                + _tail(probed.stdout + "\n" + probed.stderr, 3).strip()
            )
            return entry
    hook_arguments = _pre_commit_arguments(workspace, tool)
    for arguments in tool.commands:
        commands = [[*arguments, *hook_arguments]]
        if tool.name == "ruff" and configuration.get("format") and arguments[0] == "check":
            commands.append(["format", "--check", *_RUFF_OPTIONS, *hook_arguments])
        for command_arguments in commands:
            command = [*base, *command_arguments, *files]
            result = execute(
                command,
                working_directory=workspace,
                timeout_seconds=timeout_seconds,
                environment=environment,
            )
            entry["commands"].append(command)
            entry["results"].append(
                {
                    "command": command,
                    "environment": environment,
                    "label": " ".join([tool.name, *command_arguments[:1]]),
                    "exit_code": result.exit_code,
                    "timed_out": result.timed_out,
                    "output_tail": _tail(result.stdout + "\n" + result.stderr),
                    "output": result.stdout + "\n" + result.stderr,
                }
            )
    entry["ran"] = True
    failed = any(r["timed_out"] or r["exit_code"] != 0 for r in entry["results"])
    entry["reason"] = "failed" if failed else "passed"
    return entry


def _mypy_exclude_patterns(workspace: Path) -> list[str]:
    """The target's mypy `exclude` regexes, from pyproject, mypy.ini or setup.cfg."""
    patterns: list[str] = []
    pyproject = workspace / "pyproject.toml"
    if pyproject.is_file():
        try:
            data = tomllib.loads(_read(pyproject))
        except tomllib.TOMLDecodeError:
            data = {}
        exclude = data.get("tool", {}).get("mypy", {}).get("exclude", [])
        patterns.extend([exclude] if isinstance(exclude, str) else list(exclude))
    for name in ("mypy.ini", ".mypy.ini", "setup.cfg"):
        path = workspace / name
        if not path.is_file():
            continue
        parser = configparser.ConfigParser()
        try:
            parser.read_string(_read(path))
        except configparser.Error:
            continue
        value = parser.get("mypy", "exclude", fallback="")
        patterns.extend(line.strip() for line in value.splitlines() if line.strip())
    return patterns


def _mypy_excluded(workspace: Path, path: str) -> bool:
    """Whether CI's recursive `mypy` would skip `path` (#163).

    mypy applies `exclude` only to files it discovers; a file named on the
    command line is checked anyway. ipython excludes `tests`, and naming
    tests/test_history.py raised errors CI never sees.
    """
    posix = path.replace(chr(92), "/")
    for pattern in _mypy_exclude_patterns(workspace):
        try:
            if re.search(pattern, posix):
                return True
        except re.error:
            continue
    return False


_YAML_KEY = re.compile(r"^(\s*)(-\s+)?([\w-]+):(?:\s+(.*))?$")


def _yaml_scalar(
    value: str, lines: list[str], index: int, indent: int, anchors: dict[str, str | list[str]]
) -> tuple[str | None, int]:
    """One scalar of a pre-commit config and the index of the line after it.

    Handles what hook scopes use: an `&anchor`, a `*alias`, a `|` block and
    single or double quotes.
    """
    anchor = None
    if value.startswith("&"):
        anchor, _, value = value.partition(" ")
        anchor, value = anchor[1:], value.strip()
    if value.startswith("*"):
        found = anchors.get(value[1:].strip())
        return (found if isinstance(found, str) else None), index
    if value[:1] in ("|", ">"):
        body: list[str] = []
        while index < len(lines) and (
            not lines[index].strip()
            or len(lines[index]) - len(lines[index].lstrip()) > indent
        ):
            body.append(lines[index])
            index += 1
        text = textwrap.dedent("\n".join(body)).strip("\n")
    elif value[:1] in ("'", '"'):
        quote = value[0]
        end = value.rfind(quote)
        text = value[1:end] if end > 0 else value[1:]
        text = text.replace("''", "'") if quote == "'" else text.replace(chr(92) * 2, chr(92))
    elif not value:
        # A plain scalar that starts on the next line and folds its lines
        # into one. agentscope's `exclude:` read as '', which matched every
        # path and skipped mypy. An empty value is no pattern. Mailman #288.
        body = []
        while index < len(lines) and (
            not lines[index].strip()
            or len(lines[index]) - len(lines[index].lstrip()) > indent
        ):
            if lines[index].lstrip().startswith("- "):
                break
            body.append(lines[index].strip())
            index += 1
        text = " ".join(part for part in body if part) or None
    else:
        text = re.sub(r"\s+#.*$", "", value)
    if anchor:
        anchors[anchor] = text
    return text, index


def _flow_list(text: str, lines: list[str], index: int) -> tuple[list[str], int]:
    """A `[a, "b,c"]` flow list from just past its `[`, over as many lines as it runs.

    A comma or `]` inside quotes belongs to the item: `"--extend-ignore=E203,W503"`
    and `"--ignore-words-list=[a]"` are one argument each. Mailman #358.
    """
    items: list[str] = []
    token = ""
    quote = ""
    quoted = closed = False
    while True:
        position = 0
        while position < len(text):
            char = text[position]
            position += 1
            if quote:
                if char == quote and quote == "'" and text[position : position + 1] == "'":
                    token += "'"
                    position += 1
                elif char == quote:
                    quote, closed = "", True
                elif char == chr(92) and quote == '"' and text[position : position + 1] in ('"', chr(92)):
                    token += text[position]
                    position += 1
                else:
                    token += char
            elif char in ",]":
                if quoted or token.strip():
                    items.append(token if quoted else token.strip())
                token, quoted, closed = "", False, False
                if char == "]":
                    return items, index
            elif char == "#" and (position == 1 or text[position - 2].isspace()):
                break
            elif char in ("'", '"') and not token.strip() and not quoted:
                quote, quoted, token = char, True, ""
            elif not closed:
                token += char
        if index >= len(lines):
            break
        # A line break inside an item folds to a space.
        token += " " if (quote or token.strip()) and not closed else ""
        text = lines[index]
        index += 1
    if quoted or token.strip():
        items.append(token if quoted else token.strip())
    return items, index


def _yaml_list(
    value: str, lines: list[str], index: int, indent: int, anchors: dict[str, str | list[str]]
) -> tuple[list[str] | None, int]:
    """A hook's `args`: a flow list on its line, or `- item` lines below it.

    `&name` records the list for a later `*name`. An alias whose anchor is not
    in this file is None, not an empty list: the hook has args nobody read.
    """
    def unquote(item: str) -> str:
        item = item.strip()
        if len(item) > 1 and item[0] == item[-1] and item[0] in ("'", '"'):
            return item[1:-1]
        return item

    value = value.strip()
    anchor = None
    if value.startswith("&"):
        anchor, _, value = value.partition(" ")
        anchor, value = anchor[1:], value.strip()
    if value.startswith("*"):
        found = anchors.get(re.sub(r"\s+#.*$", "", value)[1:].strip())
        if isinstance(found, str):
            found = _flow_list(found[1:], [], 0)[0] if found.startswith("[") else [found]
        return found, index
    if value.startswith("["):
        items, index = _flow_list(value[1:], lines, index)
        if anchor:
            anchors[anchor] = items
        return items, index
    items: list[str] = []
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if stripped and len(line) - len(line.lstrip()) < indent:
            break
        if stripped.startswith("- "):
            items.append(unquote(re.sub(r"\s+#.*$", "", stripped[2:])))
        elif stripped and not stripped.startswith("#"):
            break
        index += 1
    if anchor:
        anchors[anchor] = items
    return items, index


def _pre_commit_scopes(workspace: Path) -> tuple[dict, list[dict]] | None:
    """The top-level `files`/`exclude`, and each hook's repo, id, files and exclude."""
    path = workspace / ".pre-commit-config.yaml"
    if not path.is_file():
        return None
    lines = _read(path).splitlines()
    anchors: dict[str, str | list[str]] = {}
    top: dict[str, str | None] = {"files": None, "exclude": None}
    hooks: list[dict[str, str | None]] = []
    repo = ""
    hook: dict[str, str | None] | None = None
    key_indent = -1
    index = 0
    while index < len(lines):
        line = lines[index]
        index += 1
        found = _YAML_KEY.match(line)
        if not found:
            continue
        spaces, dash, key, value = found.groups()
        indent = len(spaces) + len(dash or "")
        value = (value or "").strip()
        if dash and key == "repo":
            repo, hook = value, None
            continue
        if dash and repo:
            hook = {"repo": repo, "id": None, "files": None, "exclude": None, "args": []}
            hooks.append(hook)
            key_indent = indent
        if key == "args" and hook is not None and indent == key_indent:
            hook["args"], index = _yaml_list(value, lines, index, indent, anchors)
            continue
        if key not in ("files", "exclude", "id") and not value.startswith("&"):
            continue
        text, index = _yaml_scalar(value, lines, index, indent, anchors)
        if indent == 0:
            if key in top:
                top[key] = text
        elif hook is not None and indent == key_indent and key in hook:
            hook[key] = text
    return top, hooks


# Hook args that only set how a tool judges code. A `--fix` or `--write`
# would rewrite the candidate, so ruff's and mypy's hook args are not taken.
# agentscope's pylint hook disables 25 messages and raises the size limits.
_HOOK_ARGUMENT_TOOLS = ("black", "isort", "flake8", "pylint")

# pylint imports the code it checks, so a `repo: local` pylint hook runs the
# project's own pylint. When the run environment lacks it, installing
# another one could pass or fail where CI does not; the run says so instead.
_ENVIRONMENT_HOOK_TOOLS = ("pylint",)


def _environment_hook(workspace: Path, tool: LintTool) -> bool:
    """Whether every pre-commit hook of `tool` is a `repo: local` hook."""
    if tool.name not in _ENVIRONMENT_HOOK_TOOLS:
        return False
    scopes = _pre_commit_scopes(workspace)
    if scopes is None:
        return False
    own = _own_hooks(scopes[1], tool)
    return bool(own) and all(str(hook["repo"]).strip("'\"") == "local" for hook in own)

# Options of those tools that take no value, from each tool's --help. A bare
# token after one of these is a path, not a value. Any other option keeps the
# token after it: a flake8 plugin's `--docstring-convention google` would
# otherwise read the changed file as its value. Mailman #358.
_COMMON_FLAGS = frozenset({"-h", "--help", "--version", "-v", "--verbose", "-q", "--quiet"})
_FLAGS: dict[str, frozenset[str]] = {
    "black": _COMMON_FLAGS | {
        "--check", "--diff", "--color", "--no-color", "--fast", "--safe", "--preview",
        "--unstable", "--pyi", "--ipynb", "-S", "--skip-string-normalization", "-C",
        "--skip-magic-trailing-comma", "-x", "--skip-source-first-line",
        "--experimental-string-processing",
    },
    "isort": _COMMON_FLAGS | {
        "-c", "--check", "--check-only", "-d", "--diff", "--filter-files", "--atomic",
        "--stdout", "--show-config", "--show-files", "--overwrite-in-place",
        "--dont-follow-links", "--gitignore", "--skip-gitignore", "--float-to-top",
        "--dont-float-to-top", "--ca", "--combine-as", "--combine-star",
        "--fss", "--force-sort-within-sections", "--fas", "--force-alphabetical-sort",
        "--fass", "--force-alphabetical-sort-within-sections", "--sl",
        "--force-single-line-imports", "--force-adds", "--ot", "--order-by-type",
        "--dt", "--dont-order-by-type", "--case-sensitive",
        "--honor-case-in-force-sorted-sections", "--up", "--use-parentheses",
        "--tc", "--trailing-comma", "--reverse-relative", "--reverse-sort",
        "--sort-reexports", "--star-first", "-e", "--balanced",
        "--remove-redundant-aliases", "--os", "--only-sections", "--om",
        "--only-modified", "--csi", "--combine-straight-imports", "--ls",
        "--length-sort", "--lss", "--length-sort-straight",
        "--no-sections", "--no-inline-sort", "--ensure-newline-before-comments",
        "--group-by-package", "--treat-all-comment-as-code", "--honor-noqa",
        "--ignore-whitespace", "--append-only", "--resolve-all-configs",
    },
    "flake8": _COMMON_FLAGS | {
        "--count", "--show-source", "--no-show-source", "--statistics", "--exit-zero",
        "--tee", "--benchmark", "--bug-report", "--isolated", "--hang-closing",
        "--disable-noqa", "--doctests",
    },
    "pylint": _COMMON_FLAGS | {
        "-E", "--errors-only", "--exit-zero", "--enable-all-extensions",
        "--long-help", "--list-msgs", "--list-msgs-enabled", "--list-groups",
        "--list-conf-levels", "--list-extensions", "--full-documentation",
        "--generate-rcfile", "--generate-toml-config",
    },
}


def _own_hooks(hooks: list[dict], tool: LintTool) -> list[dict]:
    return [
        hook for hook in hooks
        if hook["id"] == tool.name
        or (re.search(tool.pre_commit_repo, str(hook["repo"]), re.IGNORECASE)
            and not str(hook["id"]).endswith("-format"))
    ]


def _pre_commit_arguments(workspace: Path, tool: LintTool) -> list[str]:
    """The options the target's own pre-commit hook passes `tool` (#288).

    agentscope's black hook sets `--line-length=79`; black ran at its
    default 88 and passed lines CI rejects.
    """
    scopes = _pre_commit_scopes(workspace)
    if scopes is None:
        return []
    if tool.name == "ruff":
        return _ruff_config(scopes[1])
    if tool.name not in _HOOK_ARGUMENT_TOOLS:
        return []
    for hook in _own_hooks(scopes[1], tool):
        if hook.get("args"):
            return _options(hook["args"], tool.name)
    return []


def _options(arguments: list[str], tool: str) -> list[str]:
    """A hook's options, each with its value; positional paths stay out.

    isort's documented hook is `args: ["--profile", "black"]`. Keeping only
    dashed tokens ran `isort --profile pkg/mod.py`, which read the file as
    the profile. A bare token right after an option without `=` is that
    option's value, unless the option is one of the tool's flags:
    `--filter-files src` names a path, and isort ran on all of src (#358).
    """
    options: list[str] = []
    expects_value = False
    for argument in arguments:
        if argument.startswith("-"):
            options.append(argument)
            expects_value = "=" not in argument and argument not in _FLAGS.get(tool, ())
        elif expects_value:
            options.append(argument)
            expects_value = False
    return options


def _ruff_config(hooks: list[dict]) -> list[str]:
    """Only the `--config` path of a ruff hook; its `--fix` would rewrite.

    capa's hooks run `ruff check --fix --config .github/ruff.toml`; ruff ran
    at its defaults and flagged imports capa sorts by length. Mailman #299.
    """
    for hook in hooks:
        if not str(hook.get("id") or "").startswith("ruff"):
            continue
        arguments = [str(argument) for argument in hook.get("args") or []]
        for index, argument in enumerate(arguments):
            if argument.startswith("--config="):
                return [argument]
            if argument == "--config" and index + 1 < len(arguments):
                return ["--config", arguments[index + 1]]
    return []


def _pre_commit_skips(workspace: Path, tool: LintTool, path: str) -> bool:
    """Whether CI's pre-commit leaves `path` out of every hook of `tool` (#260).

    pylint excludes tests/functional/ from black and mypy; linting a new
    functional test there blocked a run on findings CI never reports. A
    pattern that does not compile counts as covering the file.
    """
    scopes = _pre_commit_scopes(workspace)
    if scopes is None:
        return False
    top, hooks = scopes
    own = _own_hooks(hooks, tool)
    if not own:
        return False
    posix = path.replace(chr(92), "/")

    def matches(pattern: str | None, default: bool) -> bool:
        if pattern is None:
            return default
        try:
            return re.search(pattern, posix) is not None
        except re.error:
            return default

    if not matches(top["files"], True) or matches(top["exclude"], False):
        return True
    return not any(
        matches(hook["files"], True) and not matches(hook["exclude"], False)
        for hook in own
    )


BASELINE_DIRECTORY = "lint-base"
_DIGITS = re.compile(r"\d+")
_NOTE = re.compile(r":\d+(?::\d+)?: note: ")
_CODE_FRAME = re.compile(r"\s*\d*\s*\|")
#: pyrefly's `Cannot find module `x`` and mypy's missing-module error.
_MISSING_MODULE = re.compile(
    r"Cannot find module `([^`]+)`"
    r'|Cannot find implementation or library stub for module named "([^"]+)"'
)
#: The first line of a pyrefly diagnostic; the lines under it belong to it.
_BLOCK_HEADER = re.compile(r"\s*(?:ERROR|WARN|INFO)\b")


def _without_missing_imports(output: str, modules: set[str]) -> str:
    """`output` without the missing-import diagnostics for `modules`.

    A pyrefly diagnostic runs from its ERROR line to the next header, so its
    code frame and search-path notes go with it.
    """
    if not modules:
        return output
    kept: list[str] = []
    skipping = False
    for line in output.splitlines():
        found = _MISSING_MODULE.search(line)
        if found and (found.group(1) or found.group(2)) in modules:
            skipping = _BLOCK_HEADER.match(line) is not None
            continue
        if skipping and not _BLOCK_HEADER.match(line):
            continue
        skipping = False
        kept.append(line)
    return "\n".join(kept)


def _signatures(output: str, roots: tuple[Path, ...]) -> Counter[str]:
    """Output lines with paths made relative and every number blanked.

    Line numbers shift when a patch adds lines above a finding, and summary
    counts change with it, so neither may tell an old finding from a new one.
    """
    lines: Counter[str] = Counter()
    # pyright indents every finding, so only a diff's context lines are
    # skipped (#305).
    is_diff = any(line.startswith("@@") for line in output.splitlines())
    for line in output.splitlines():
        # A diff's context lines can hold the patch's own well-formatted code
        # next to an old reformat (ipython#9891, black); only +/- lines count.
        # Hunk and file headers carry no finding, and their count moves with
        # how the patch splits the hunks.
        if is_diff and line.startswith((" ", "@@", "--- ", "+++ ")):
            continue
        # ruff's and ty's code frames quote source, which the patch can shift.
        if _CODE_FRAME.match(line):
            continue
        line = line.replace("\\", "/").strip()
        # pyrefly quotes the import root JSON-escaped, `C:\\Users` (#305).
        line = re.sub(r"(?<!\w\w:)/{2,}", "/", line)
        # pyright prints `c:\...` where the root says `C:\...` (#305).
        for root in roots:
            prefix = re.escape(str(root).replace("\\", "/"))
            line = re.sub(prefix + "/", "", line, flags=re.IGNORECASE)
        # ty names the bare root in its module-resolution notes (#177).
        for root in roots:
            prefix = re.escape(str(root).replace("\\", "/"))
            line = re.sub(prefix, "<root>", line, flags=re.IGNORECASE)
        line = _DIGITS.sub("#", line).rstrip()
        if line:
            lines[line] += 1
    return lines


class _Baseline:
    """A detached worktree at the base commit, made on the first failure (#163).

    ipython#9891: flake8, black and mypy each failed on lines the patch never
    touched. A finding only blocks when the base commit does not already
    produce it.
    """

    def __init__(
        self,
        run_directory: Path,
        workspace: Path,
        base_commit: str,
        *,
        directory: str = BASELINE_DIRECTORY,
    ) -> None:
        self.workspace = workspace
        self.base_commit = base_commit
        self.path = run_directory / "scratch" / directory
        self.ready: bool | None = None
        self.detail = ""

    def prepare(self, timeout_seconds: float) -> bool:
        if self.ready is None:
            self._remove()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            added = execute(
                ["git", "worktree", "add", "--detach", str(self.path), self.base_commit],
                working_directory=self.workspace,
                timeout_seconds=timeout_seconds,
            )
            self.ready = (
                added.exit_code == 0 and not added.timed_out and self.path.is_dir()
            )
            if not self.ready:
                self.detail = _tail(added.stdout + "\n" + added.stderr, 3).strip()
        return self.ready

    def _remove(self) -> None:
        execute(
            ["git", "worktree", "remove", "--force", str(self.path)],
            working_directory=self.workspace,
            timeout_seconds=120,
        )
        execute(
            ["git", "worktree", "prune"], working_directory=self.workspace, timeout_seconds=120
        )

    def close(self) -> None:
        if self.ready is not None:
            self._remove()


def _new_findings(
    result: dict[str, Any],
    baseline: _Baseline,
    *,
    workspace: Path,
    files: list[str],
    timeout_seconds: float,
) -> tuple[list[str] | None, str]:
    """Lines of `result` the base commit does not produce, or None if unknown."""
    if not baseline.prepare(timeout_seconds):
        return None, f"the base worktree could not be made: {baseline.detail}"
    base_files = [path for path in files if (baseline.path / path).is_file()]
    if not base_files:
        return None, "every linted file is new in this patch"
    command = [*result["command"][: len(result["command"]) - len(files)], *base_files]
    ran = execute(
        command,
        working_directory=baseline.path,
        timeout_seconds=timeout_seconds,
        environment=result.get("environment"),
    )
    if ran.timed_out:
        return None, "the base run timed out"
    roots = (workspace.resolve(), baseline.path.resolve(), workspace, baseline.path)
    # A module the base run cannot resolve either is this host's environment,
    # not the patch: python/typeshed#15495 added one more import of an
    # unresolved `grpc.aio` and read as a new finding (#312).
    base_output = ran.stdout + "\n" + ran.stderr
    unresolved = {
        found.group(1) or found.group(2) for found in _MISSING_MODULE.finditer(base_output)
    }
    output = _without_missing_imports(result["output"], unresolved)
    base_output = _without_missing_imports(base_output, unresolved)
    patched = _signatures(output, roots)
    if ran.exit_code == 0:
        return list(patched.elements()) or ["(output unchanged)"], "the base commit passes"
    new = patched - _signatures(base_output, roots)
    lines = []
    for line in output.splitlines():
        signature = next(iter(_signatures(line, roots)), None)
        if signature is not None and new[signature] > 0:
            new[signature] -= 1
            lines.append(line.rstrip())
    # mypy prints each missing-stubs hint once, beside whichever import it
    # checks first, so the hint moves between the two runs. A note only
    # elaborates on an error; a new error is caught on its own line (#179).
    lines = [line for line in lines if not _NOTE.search(line)]
    return lines, f"the base commit also exits {ran.exit_code}"


def run_lint(
    run_directory: Path,
    *,
    workspace: Path | None,
    changed_paths: list[str],
    acknowledged: dict[str, str] | None = None,
    base_commit: str | None = None,
    timeout_seconds: float = TARGET_CHECK_TIMEOUT_SECONDS,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Run the target's linters and type checkers over the changed files (#120).

    A tool the target configures or runs in CI that cannot run here is
    `lint-not-run` and blocks, unless `acknowledged` (read from
    `lint-acknowledgement.json`) holds a note for it. A target with no linter
    configured has no finding. With `base_commit`, a failure whose output the
    base commit already produces is `lint-preexisting` and does not block
    (#163).
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
    # A stubs package changes only .pyi files; skipping them linted nothing (#305).
    files = [
        path for path in _existing(workspace, changed_paths) if path.endswith((".py", ".pyi"))
    ]
    record["files"] = files
    if not files:
        record["reason"] = "no-changed-python-files"
        return record, []

    acknowledged = acknowledged or {}
    python = environment_python(run_directory)
    tools = {tool.name: tool for tool in LINT_TOOLS}
    findings: list[dict[str, Any]] = []
    entries: list[dict[str, Any]] = []
    baseline = _Baseline(run_directory, workspace, base_commit) if base_commit else None
    try:
        _lint_each(
            configurations,
            tools,
            acknowledged,
            findings,
            entries,
            baseline,
            python=python,
            workspace=workspace,
            files=files,
            timeout_seconds=timeout_seconds,
        )
    finally:
        if baseline is not None:
            baseline.close()
    for entry in entries:
        for result in entry["results"]:
            result.pop("output", None)
    record["tools"] = entries
    record["ran"] = any(entry["ran"] for entry in entries)
    if any(finding["code"] == "lint-failed" for finding in findings):
        record["reason"] = "failed"
    elif not all(entry["ran"] for entry in entries):
        record["reason"] = "not-run"
    elif any(finding["code"] == "lint-preexisting" for finding in findings):
        record["reason"] = "preexisting"
    else:
        record["reason"] = "passed"
    return record, findings


def _lint_each(
    configurations: list[dict[str, Any]],
    tools: dict[str, LintTool],
    acknowledged: dict[str, str],
    findings: list[dict[str, Any]],
    entries: list[dict[str, Any]],
    baseline: _Baseline | None,
    *,
    python: str | None,
    workspace: Path,
    files: list[str],
    timeout_seconds: float,
) -> None:
    changed_files = files
    for configuration in configurations:
        tool = tools[configuration["tool"]]
        files = changed_files
        if tool.name == "mypy":
            files = [path for path in files if not _mypy_excluded(workspace, path)]
        files = [path for path in files if not _pre_commit_skips(workspace, tool, path)]
        if not files:
            entries.append(
                {
                    **configuration,
                    "ran": True,
                    "reason": f"excluded: the target's {tool.name} configuration or "
                    "pre-commit hooks exclude every changed file",
                    "install": None,
                    "commands": [],
                    "results": [],
                }
            )
            continue
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
            if not (result["timed_out"] or result["exit_code"] != 0):
                continue
            output = _tail(result["output_tail"], 15)
            if baseline is not None and not result["timed_out"]:
                new, why = _new_findings(
                    result,
                    baseline,
                    workspace=workspace,
                    files=files,
                    timeout_seconds=timeout_seconds,
                )
                result["baseline"] = why
                if new == []:
                    findings.append(
                        {
                            "code": "lint-preexisting",
                            "blocking": False,
                            "detail": (
                                f"`{result['label']}` exited {result['exit_code']} on "
                                f"the changed files, but {why} with the same "
                                "output; the patch adds no finding. " + output
                            ),
                        }
                    )
                    continue
                if new is not None:
                    output = f"New since the base commit ({why}):\n" + "\n".join(new[:15])
            findings.append(
                {
                    "code": "lint-failed",
                    "blocking": True,
                    "detail": (
                        f"`{result['label']}` exited {result['exit_code']} on "
                        f"the changed files; the target's CI runs {tool.name} "
                        "and will fail. " + output
                    ),
                }
            )

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
