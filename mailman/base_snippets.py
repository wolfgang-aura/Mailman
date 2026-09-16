"""Ask whether the line the issue complains about is still in the base tree.

An issue that quotes the source it is about answers its own question. If
`raise CompilerPanic(f"Invalid path: {c.resolved_path}")` is not in the tree
the run is about to work against, and the file that line came from is still
there, then somebody upstream already changed that line and the run is aimed at
a fix that shipped.

Nothing else sees this. `mailman/reproduction.py` only decides `already-fixed-
upstream` when prior art links a merged pull request to the issue, so a fix that
arrived without a back-reference is invisible to every gate before a person
reads the source. Hunt 20260916T192039Z-16eb22 opened its only run on such an
issue and dropped it after `prepare-workspace`.
https://github.com/wolfgang-aura/Mailman/issues/103

The check is deliberately narrow. A snippet counts only when the issue also
names the file it came from, which is the shape of a reporter quoting the code
he is complaining about rather than proposing the code he wants written. A
proposed patch under "How can it be fixed?" is absent at base for the obvious
reason, and reading that as evidence of a shipped fix would drop good targets.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mailman.executor import CommandResult, execute
from mailman.workspace import WORKSPACE_DIRECTORY

BASE_SNIPPET_CHECK_FILENAME = "base-snippet-check.json"

#: Each snippet is one search, so the count is capped rather than trusted.
MAX_SNIPPETS = 20
#: Below this a "snippet" is a fragment that matches everywhere: `if x:`,
#: `return None`, a bare closing bracket.
MINIMUM_SNIPPET_CHARACTERS = 12

#: A source file with an optional line or line range: `vyper/ir/optimizer.py:551`
#: or `vyper/compiler/output_bundle.py:120-121`. This is what ties a quoted
#: line to a file, and without one a quoted line decides nothing.
_PATH_REFERENCE = re.compile(
    r"(?<![\w./-])([\w.-]+(?:/[\w.-]+)*\.pyi?x?)(?::(\d+)(?:-\d+)?)?"
)
#: A line that is nothing but a path reference, with or without a comment
#: marker in front of it. It names the file the lines under it came from; it is
#: not itself a line to search for.
_PATH_ONLY_LINE = re.compile(
    r"^\s*(?:#|//|--)?\s*" + _PATH_REFERENCE.pattern + r"\s*$"
)
#: What makes a line read as source rather than as prose or shell output.
_SOURCE_MARKERS = (
    re.compile(r"\braise\s"),
    re.compile(r"\bdef\s"),
    re.compile(r"\bimport\s"),
    re.compile(r"\bself\."),
    re.compile(r"\w\s*\([^()]*\)"),
)
#: Console and REPL prompts. The transcript of a reproduction is not source,
#: and a shell line with a parenthesised aside in it reads as a call.
_PROMPTS = ("$ ", "> ", ">>> ", "... ", "% ", "PS ")
_BACKTICKED = re.compile(r"`([^`\n]{%d,200})`" % MINIMUM_SNIPPET_CHARACTERS)
_FENCE = ("```", "~~~")


def looks_like_source(text: str) -> bool:
    """Whether this line reads as code rather than as prose or shell output."""
    stripped = text.strip()
    if not stripped or stripped.startswith(_PROMPTS):
        return False
    return any(marker.search(stripped) for marker in _SOURCE_MARKERS)


def _issue_body(markdown: str) -> str:
    """The reporter's own words, without the capture header or footer."""
    body = markdown.split("## Issue body", 1)[-1]
    return body.split("## Capture boundary", 1)[0]


def issue_snippets(
    markdown: str, *, limit: int = MAX_SNIPPETS
) -> list[dict[str, Any]]:
    """Source lines the issue quotes, each with the file it names when it does.

    Fenced blocks first, because that is where a reporter pastes the code, and
    a path comment inside a fence sets the file for the lines under it. Inline
    backticked fragments are recorded too, but they carry no file and so can
    never decide anything on their own.
    """
    found: list[dict[str, Any]] = []
    seen: set[str] = set()

    def keep(text: str, *, source: str, path: str | None, line: int | None) -> None:
        cleaned = text.strip()
        if (
            len(cleaned) < MINIMUM_SNIPPET_CHARACTERS
            or cleaned in seen
            or not looks_like_source(cleaned)
            or _PATH_ONLY_LINE.match(cleaned)
        ):
            return
        seen.add(cleaned)
        found.append(
            {"text": cleaned, "source": source, "path": path, "line": line}
        )

    in_fence = False
    path: str | None = None
    line_number: int | None = None
    for raw in _issue_body(markdown).splitlines():
        if raw.strip().startswith(_FENCE):
            in_fence = not in_fence
            # A fence ends the file a previous fence's comment named.
            path, line_number = None, None
            continue
        if not in_fence:
            for match in _BACKTICKED.finditer(raw):
                keep(match.group(1), source="inline", path=None, line=None)
            continue
        only_path = _PATH_ONLY_LINE.match(raw)
        if only_path:
            path = only_path.group(1)
            line_number = int(only_path.group(2)) if only_path.group(2) else None
            continue
        keep(raw, source="fence", path=path, line=line_number)
        if len(found) >= limit:
            break
    return found[:limit]


def _normalize(text: str) -> str:
    """Every whitespace character dropped, not merely collapsed.

    A formatter that breaks one call across three lines puts a newline and an
    indent inside the very line the issue quoted, so collapsing runs to single
    spaces is not enough. Dropping whitespace altogether can only make this
    check quieter, which is the safe direction for a code that stops a run.
    """
    return "".join(text.split())


def _present_in_tree(
    workspace: Path, text: str, *, executable: str, timeout_seconds: float
) -> tuple[bool | None, dict[str, Any]]:
    """Whether the tree holds this line literally, anywhere.

    Anywhere, not only in the file the issue named: a line that moved to
    another module is still present, and reporting that as a shipped fix would
    drop a live target.
    """
    result: CommandResult = execute(
        [executable, "grep", "--fixed-strings", "--quiet", "-e", text],
        working_directory=workspace,
        timeout_seconds=timeout_seconds,
    )
    record = {"exit_code": result.exit_code, "timed_out": result.timed_out}
    if result.timed_out or result.exit_code not in (0, 1):
        return None, record
    return result.exit_code == 0, record


def _present_ignoring_whitespace(workspace: Path, snippet: dict[str, Any]) -> bool:
    """A second look at the named file, tolerant of reformatting.

    A literal search calls a line absent when upstream only rewrapped it. The
    file is already known and already open in the worst case, so reading it is
    cheaper than a wrong `already-fixed-upstream`.
    """
    path = snippet.get("path")
    if not path:
        return False
    target = (workspace / str(path)).resolve()
    if not target.is_relative_to(workspace.resolve()) or not target.is_file():
        return False
    return _normalize(snippet["text"]) in _normalize(
        target.read_text(encoding="utf-8", errors="replace")
    )


def _detail(record: dict[str, Any]) -> str:
    decided = record.get("decided_by")
    checked = len(record.get("snippets") or [])
    if not decided:
        return (
            f"{checked} quoted source line(s) read from the issue, none of them "
            "missing from the base tree"
        )
    return (
        f"the issue quotes {decided['path']}"
        + (f":{decided['line']}" if decided.get("line") else "")
        + f" as {decided['text']!r}, and that line is not in the base tree at "
        f"{record.get('base_commit')} although {decided['path']} still exists. "
        "Upstream changed that line; this issue is already fixed at base"
    )


def check_base_snippets(
    run_directory: Path,
    *,
    base_commit: str | None = None,
    workspace: Path | None = None,
    executable: str = "git",
    timeout_seconds: float = 60,
) -> dict[str, Any]:
    """Search the base tree for the source lines the issue quotes."""
    tree = workspace or (run_directory / WORKSPACE_DIRECTORY)
    record: dict[str, Any] = {
        "schema_version": 1,
        "checked_at": datetime.now(UTC).isoformat(),
        "base_commit": base_commit,
        "workspace": str(tree),
        "snippets": [],
        "findings": [],
        "already_fixed": False,
        "decided_by": None,
        "success": False,
    }
    issue_path = run_directory / "issue.md"
    if not issue_path.is_file():
        record["detail"] = "no captured issue to read source lines out of"
        return _write(run_directory, record)
    if not (tree / ".git").exists():
        record["detail"] = f"no prepared workspace at {tree}"
        return _write(run_directory, record)
    snippets = issue_snippets(issue_path.read_text(encoding="utf-8", errors="replace"))
    root = tree.resolve()
    for snippet in snippets:
        row = dict(snippet)
        path = snippet.get("path")
        row["file_exists"] = bool(
            path
            and (tree / str(path)).resolve().is_relative_to(root)
            and (tree / str(path)).is_file()
        )
        present, command = _present_in_tree(
            tree, snippet["text"], executable=executable, timeout_seconds=timeout_seconds
        )
        row["search"] = command
        if present is None:
            row["present"] = None
        elif present:
            row["present"] = True
        else:
            row["present"] = _present_ignoring_whitespace(tree, snippet)
            row["matched_ignoring_whitespace"] = row["present"]
        record["snippets"].append(row)
        # A line with no file behind it proves nothing on its own, and a file
        # that is gone explains the absence without a fix.
        if row["present"] is False and row["file_exists"]:
            record["findings"].append(row)
    record["success"] = True
    record["already_fixed"] = bool(record["findings"])
    record["decided_by"] = record["findings"][0] if record["findings"] else None
    record["detail"] = _detail(record)
    return _write(run_directory, record)


def load_base_snippet_check(run_directory: Path) -> dict[str, Any]:
    path = run_directory / BASE_SNIPPET_CHECK_FILENAME
    if not path.is_file():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _write(run_directory: Path, record: dict[str, Any]) -> dict[str, Any]:
    (run_directory / BASE_SNIPPET_CHECK_FILENAME).write_text(
        json.dumps(record, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    return record
