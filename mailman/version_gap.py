"""List what landed between the release an issue was reported on and base.

A reporter running an old release can describe a bug that the next release
already fixed, in a commit that cites some other issue or none. beets#7019 was
reported on 2.13.1; e5846915d, "modify: fix selecting objects", had fixed it in
v2.14.0 three weeks earlier and cited bug 4880. Prescreen and the base-snippet
check both passed, because the issue quotes no source line and no merged pull
request links it. https://github.com/wolfgang-aura/Mailman/issues/103

This check does not decide anything. It finds the release tag the issue names,
and when base is ahead of it, lists the commits since that tag whose subjects
share words with the issue title or which touch a file the issue names. A
person reads a handful of subjects before the run spends an environment build.
"""

from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mailman.base_snippets import _PATH_REFERENCE, _issue_body
from mailman.executor import execute
from mailman.workspace import WORKSPACE_DIRECTORY

VERSION_GAP_FILENAME = "version-gap.json"

#: How many related commits the record keeps. Past this a person stops reading.
MAX_RELATED = 8
#: A subject must share this many title words to count. One shared word is the
#: command name, which half the history of a CLI shares.
MINIMUM_SHARED_WORDS = 2

#: "beets version: 2.13.1", "Version 2.13", "on v2.13.1".
_VERSION = re.compile(
    r"(?i)(?:\bversion\b[^\n\d]{0,30}|\bv)(\d+\.\d+(?:\.\d+)?(?:[-.]?(?:post|rc|a|b)\d*)?)\b"
)
#: A pinned requirement, as `pip freeze` or pypdf's `_debug_versions` prints it:
#: "pypdf==5.1.0". https://github.com/wolfgang-aura/Mailman/issues/296
_PINNED = re.compile(r"(?<![\w.-])([A-Za-z][\w.-]*)==v?(\d+\.\d+(?:\.\d+)?(?:[-.]?(?:post|rc|a|b)\d*)?)\b")
#: The capture heading, "# owner/repo#N: title", names the target.
_HEADING_REPOSITORY = re.compile(r"^#\s+[\w.-]+/([\w.-]+)#\d+")
_WORD = re.compile(r"[a-z][a-z0-9_]{3,}")
_STOPWORDS = frozenset(
    "when with from that this than then into instead using uses used does doesn "
    "should would could have been being after before while about there their "
    "which what where error issue problem value values option options fails "
    "failing failed wrong incorrect correct correctly work works working "
    "string object objects user defined return returns".split()
)


#: A heading or bold label that names a version: "### Pylint version",
#: "**Version**". An issue form puts the answer on the lines below it.
_VERSION_HEADING = re.compile(r"(?i)^\s*(?:#{1,6}\s+.*\bversions?\b.*|\*\*[^*]*\bversions?\b[^*]*\*\*:?)\s*$")
_BARE_VERSION = re.compile(r"(?<![\w.])v?(\d+\.\d+(?:\.\d+)?(?:[-.]?(?:post|rc|a|b)\d*)?)\b")
#: Lines read under a version heading before the answer is taken to be absent.
_HEADING_LINES = 6


def _heading_versions(body: str, wanted: set[str]) -> list[str]:
    """Numbers under an issue form's version heading, up to the next heading.

    A pin for some other package is skipped here too: numpy==2.1.0 under
    "### Environment versions" is not the release the reporter ran.
    """
    found: list[str] = []
    lines = body.splitlines()
    for index, line in enumerate(lines):
        if not _VERSION_HEADING.match(line):
            continue
        for below in lines[index + 1 : index + 1 + _HEADING_LINES]:
            if below.lstrip().startswith("#"):
                break
            below = _PINNED.sub(
                lambda pin: pin.group(0) if _is_target(pin.group(1), wanted) else "", below
            )
            found.extend(_BARE_VERSION.findall(below))
    return found


def _distribution(name: str) -> str:
    """A package name as PEP 503 compares it."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _spellings(name: str) -> set[str]:
    """The ways a repository and its distribution spell one name.

    dateutil/dateutil ships python-dateutil; encode/django-rest-framework
    ships djangorestframework. Separators go, then one `python`/`py` prefix
    or `python` suffix. Mailman #336.
    """
    core = re.sub(r"[-_.]+", "", name).lower()
    forms = {core}
    for prefix in ("python", "py"):
        if core.startswith(prefix) and len(core) > len(prefix):
            forms.add(core[len(prefix) :])
            break
    if core.endswith("python") and len(core) > len("python"):
        forms.add(core[: -len("python")])
    return forms


def _is_target(name: str, wanted: set[str]) -> bool:
    return not wanted or any(_spellings(name) & _spellings(other) for other in wanted)


def reported_versions(markdown: str, names: Iterable[str] = ()) -> list[str]:
    """Version numbers the issue body names, in the order it names them.

    Numbers under a version heading come first: an issue form's answer is the
    release the reporter ran. https://github.com/wolfgang-aura/Mailman/issues/261

    A `name==X.Y.Z` pin counts only for the target itself: `names`, and the
    repository in the capture heading. A pasted `pip freeze` lists every
    other package too, and one of those versions can match a tag here.
    """
    body = _issue_body(markdown)
    heading = _HEADING_REPOSITORY.match(markdown.splitlines()[0] if markdown else "")
    wanted = {_distribution(name) for name in [*names, *(heading.groups() if heading else ())]}
    pinned = [version for name, version in _PINNED.findall(body) if _is_target(name, wanted)]
    return list(dict.fromkeys([*_heading_versions(body, wanted), *pinned, *_VERSION.findall(body)]))


def _project_names(tree: Path) -> list[str]:
    """The distribution name the target's own pyproject.toml declares."""
    try:
        data = tomllib.loads((tree / "pyproject.toml").read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return []
    project = data.get("project")
    name = project.get("name") if isinstance(project, dict) else None
    return [name] if isinstance(name, str) and name else []


def _title(markdown: str) -> str:
    first = markdown.splitlines()[0] if markdown else ""
    return first.split(":", 1)[-1] if first.startswith("#") else first


def title_words(title: str) -> list[str]:
    return list(dict.fromkeys(w for w in _WORD.findall(title.lower()) if w not in _STOPWORDS))


def _same_word(left: str, right: str) -> bool:
    """`select` and `selecting`, `object` and `objects`.

    An inflection adds a few characters. `position` inside
    `positionexecutorsimulator` is a different word.
    """
    shorter, longer = sorted((left, right), key=len)
    if len(longer) - len(shorter) > 4:
        return False
    return longer.startswith(shorter) or (len(shorter) >= 6 and longer[:6] == shorter[:6])


def shared_words(subject: str, words: list[str]) -> list[str]:
    subject_words = _WORD.findall(subject.lower())
    return [word for word in words if any(_same_word(word, other) for other in subject_words)]


def matching_tag(version: str, tags: list[str]) -> str | None:
    """The tag that marks this release: `2.13.1`, `v2.13.1` or `beets-2.13.1`."""
    for tag in tags:
        name = tag.rsplit("/", 1)[-1]
        if name in (version, f"v{version}") or name.endswith((f"-{version}", f"-v{version}")):
            return tag
    return None


def _git(workspace: Path, *arguments: str, timeout_seconds: float) -> str | None:
    result = execute(["git", *arguments], working_directory=workspace, timeout_seconds=timeout_seconds)
    if result.timed_out or result.exit_code != 0:
        return None
    return result.stdout


def _tracked_paths(tree: Path, named: list[str], *, timeout_seconds: float) -> list[str]:
    """The files the issue names, as paths in the tree.

    A Windows traceback path (`site-packages\\pypdf\\_cmap.py`) reaches the
    reference pattern as its last segment. A name that is not a file at the
    root counts when exactly one tracked file ends with it.
    https://github.com/wolfgang-aura/Mailman/issues/296
    """
    paths = [p for p in named if (tree / p).is_file()]
    missing = [p for p in named if p not in paths]
    if missing:
        tracked = (_git(tree, "ls-files", timeout_seconds=timeout_seconds) or "").splitlines()
        for name in missing:
            matches = [t for t in tracked if t.endswith("/" + name)]
            if len(matches) == 1:
                paths.append(matches[0])
    return list(dict.fromkeys(paths))


def check_version_gap(
    run_directory: Path, *, workspace: Path | None = None, timeout_seconds: float = 60
) -> dict[str, Any]:
    tree = workspace or (run_directory / WORKSPACE_DIRECTORY)
    record: dict[str, Any] = {
        "schema_version": 1,
        "checked_at": datetime.now(UTC).isoformat(),
        "reported_versions": [],
        "tag": None,
        "commits_since": 0,
        "related": [],
        "success": False,
    }
    issue_path = run_directory / "issue.md"
    if not issue_path.is_file() or not (tree / ".git").exists():
        record["detail"] = "no captured issue or no prepared workspace"
        return _write(run_directory, record)
    markdown = issue_path.read_text(encoding="utf-8", errors="replace")
    record["reported_versions"] = reported_versions(markdown, _project_names(tree))
    tags = (_git(tree, "tag", "--list", timeout_seconds=timeout_seconds) or "").split()
    tag = next((t for v in record["reported_versions"] if (t := matching_tag(v, tags))), None)
    record["success"] = True
    if tag is None:
        record["detail"] = "the issue names no release that is tagged here"
        return _write(run_directory, record)
    record["tag"] = tag
    if _git(tree, "merge-base", "--is-ancestor", tag, "HEAD", timeout_seconds=timeout_seconds) is None:
        record["detail"] = f"reported on {tag}, which is not an ancestor of base"
        return _write(run_directory, record)
    log = _git(tree, "log", "--format=%H%x09%s", f"{tag}..HEAD", timeout_seconds=timeout_seconds) or ""
    commits = [line.split("\t", 1) for line in log.splitlines() if "\t" in line]
    record["commits_since"] = len(commits)
    if not commits:
        record["detail"] = f"reported on {tag}, which is base"
        return _write(run_directory, record)
    words = title_words(_title(markdown))
    named = list(dict.fromkeys(m.group(1) for m in _PATH_REFERENCE.finditer(_issue_body(markdown))))
    paths = _tracked_paths(tree, named, timeout_seconds=timeout_seconds)
    touching: set[str] = set()
    if paths:
        touched = _git(tree, "log", "--format=%H", f"{tag}..HEAD", "--", *paths, timeout_seconds=timeout_seconds)
        touching = set((touched or "").split())
    related = []
    for commit, subject in commits:
        shared = shared_words(subject, words)
        if len(shared) >= MINIMUM_SHARED_WORDS or commit in touching:
            related.append({"commit": commit, "subject": subject, "shared_words": shared, "touches_named_file": commit in touching})
    related.sort(key=lambda row: (row["touches_named_file"], len(row["shared_words"])), reverse=True)
    record["related"] = related[:MAX_RELATED]
    record["detail"] = _detail(record)
    return _write(run_directory, record)


def _detail(record: dict[str, Any]) -> str:
    head = f"reported on {record['tag']}; base is {record['commits_since']} commit(s) ahead"
    if not record["related"]:
        return head + ", none of them sharing the title's words or touching a file the issue names"
    subjects = "; ".join(f"{row['commit'][:9]} {row['subject']}" for row in record["related"][:3])
    return head + f". Read before building: {subjects}"


def _write(run_directory: Path, record: dict[str, Any]) -> dict[str, Any]:
    (run_directory / VERSION_GAP_FILENAME).write_text(
        json.dumps(record, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    return record
