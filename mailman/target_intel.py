"""Read how a target repository actually hands out and accepts work.

Every screen in here was run by hand on 2026-09-03 across ninety candidate
repositories, and the hand pass was most of that session's cost. Worse, it was
not recorded, so the next session would have re-derived it or, more likely,
skipped it. Three findings made the difference and none of them is a judgement
call:

- Counting outside merges by `author_association` treats dependabot as a
  `CONTRIBUTOR`. `PyCQA/bandit` looked alive on one such merge and has merged no
  human outside pull request since May 2026.
- Treating any referencing pull request as a claim reads a bot-policed target as
  fully saturated. On `langchain-ai/langchain` that subtraction left 3 unassigned
  open bugs; counting only open or merged pull requests as a claim left 53.
- The enforcement bot says what it enforces, in plain text, in the comment it
  leaves when it closes a pull request. Reading that is one API call and is worth
  more than every heuristic above.

See https://github.com/wolfgang-aura/Mailman/issues/34 and
https://github.com/wolfgang-aura/Mailman/issues/35.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from mailman.executor import CommandResult, execute
from mailman.toolchain import resolve_tool

TARGET_INTEL_FILENAME = "target-intel.json"
TARGET_INTEL_MARKDOWN = "target-intel.md"
TARGET_INTEL_SCHEMA_VERSION = 1

#: Author associations that mean the author is not on the maintainer team.
OUTSIDE_ASSOCIATIONS = frozenset(
    {"CONTRIBUTOR", "NONE", "FIRST_TIME_CONTRIBUTOR", "FIRST_TIMER"}
)

#: A bot leaves an HTML marker in the comment it posts, so its rules can be
#: counted rather than read one at a time. `<!-- require-issue-link -->` and
#: `<!-- block-fork-main -->` are two of langchain's; sqlfluff's agentscan uses
#: `<!-- agentscan:possible-bot-comment:v1 -->`.
_MARKER = re.compile(r"<!--\s*([a-z0-9][a-z0-9:._-]{2,60})\s*-->", re.IGNORECASE)

_ISSUE_REFERENCE = re.compile(r"#(\d{2,7})")
_ISSUE_URL_REFERENCE = re.compile(r"issues/(\d{2,7})")
#: Branch names carry the reference when the body forgets to: `fix/issue-104`,
#: `issue_104`. Mailman's own convention, `mailman/issue-3497`, is one of these.
_BRANCH_REFERENCE = re.compile(r"issue[-_]?(\d{2,7})", re.IGNORECASE)


def repository_slug(repository: str) -> str:
    """Reduce a clone URL or a slug to `owner/name`."""
    slug = repository.removesuffix(".git").rstrip("/")
    for prefix in ("https://github.com/", "git@github.com:", "ssh://git@github.com/"):
        slug = slug.removeprefix(prefix)
    return slug


def _is_bot(user: dict[str, Any] | None) -> bool:
    """Decide whether an account is a bot, by type and by name.

    `author_association` does not carry this. Dependabot merges as a
    `CONTRIBUTOR`, which is what made `PyCQA/bandit` score a fresh outside merge
    while it had merged no human contribution in four months.
    """
    if not isinstance(user, dict):
        return True
    if (user.get("type") or "") == "Bot":
        return True
    login = (user.get("login") or "").lower()
    return login.endswith(("[bot]", "-bot")) or login.startswith("dependabot")


def is_outside_human(row: dict[str, Any]) -> bool:
    return (
        row.get("author_association") in OUTSIDE_ASSOCIATIONS
        and not _is_bot(row.get("user"))
    )


def referenced_issues(row: dict[str, Any]) -> set[str]:
    """Every issue number a pull request's text points at."""
    head = row.get("head") or {}
    text = " ".join(
        [
            row.get("title") or "",
            (row.get("body") or "")[:2000],
            head.get("ref") or "" if isinstance(head, dict) else "",
        ]
    )
    return (
        set(_ISSUE_REFERENCE.findall(text))
        | set(_ISSUE_URL_REFERENCE.findall(text))
        | set(_BRANCH_REFERENCE.findall(text))
    )


def classify_claims(pull_requests: list[dict[str, Any]]) -> dict[str, set[str]]:
    """Split referenced issues by whether the referencing row is a live claim.

    An open or merged pull request claims its issue. A closed unmerged one does
    not: on a bot-policed target it is usually the record of someone being
    refused, which is the opposite of a claim. This is the same distinction
    https://github.com/wolfgang-aura/Mailman/issues/32 and
    https://github.com/wolfgang-aura/Mailman/issues/33 taught the duplicate gate.
    """
    claiming: set[str] = set()
    abandoned: set[str] = set()
    for row in pull_requests:
        target = (
            claiming
            if row.get("state") == "open" or row.get("merged_at")
            else abandoned
        )
        target.update(referenced_issues(row))
    return {"claiming": claiming, "abandoned": abandoned}


def enforcement_markers(comments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Name the automated rules a repository enforces, from the bot's own words."""
    found: dict[str, dict[str, Any]] = {}
    for comment in comments:
        if not _is_bot(comment.get("user")):
            continue
        body = comment.get("body") or ""
        for marker in _MARKER.findall(body):
            entry = found.setdefault(
                marker.lower(),
                {"marker": marker.lower(), "count": 0, "quote": "", "seen_on": []},
            )
            entry["count"] += 1
            if not entry["quote"]:
                entry["quote"] = " ".join(body.split())[:400]
            source = comment.get("_pull_request")
            if source and source not in entry["seen_on"]:
                entry["seen_on"].append(source)
    return sorted(found.values(), key=lambda entry: -entry["count"])


class _Gh:
    """One `gh api` caller that records every command it ran."""

    def __init__(
        self, executable: str, working_directory: Path, timeout_seconds: float
    ) -> None:
        self.executable = executable
        self.working_directory = working_directory
        self.timeout_seconds = timeout_seconds
        self.commands: list[dict[str, Any]] = []
        self.failures: list[str] = []

    def json(self, path: str) -> Any | None:
        result: CommandResult = execute(
            [self.executable, "api", path],
            working_directory=self.working_directory,
            timeout_seconds=self.timeout_seconds,
        )
        self.commands.append(result.to_dict())
        if result.timed_out or result.exit_code != 0:
            self.failures.append(path)
            return None
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            self.failures.append(path)
            return None

    def pages(self, path: str, *, pages: int) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        joiner = "&" if "?" in path else "?"
        for page in range(1, pages + 1):
            got = self.json(f"{path}{joiner}per_page=100&page={page}")
            if not isinstance(got, list):
                break
            rows += got
            if len(got) < 100:
                break
        return rows


def _merge_path_rows(
    gh: _Gh, slug: str, merged: list[dict[str, Any]], limit: int
) -> list[dict[str, Any]]:
    """For each recent outside merge, read how its author got the work.

    This is the screen the operator asked to be non-negotiable: never judge a
    target by its policy text alone, read the pull requests that actually merged
    and the threads that preceded them. On `langchain-ai/langchain` it is what
    showed that every merged outside fix had its linked issue assigned to the
    pull request's author first, and that one of them was assigned to somebody
    other than the reporter, which the recorded policy denied.
    """
    rows: list[dict[str, Any]] = []
    for pull in merged[:limit]:
        number = pull.get("number")
        issues = sorted(referenced_issues(pull), key=int)
        author = ((pull.get("user") or {}).get("login")) or ""
        entry: dict[str, Any] = {
            "pull_request": number,
            "author": author,
            "merged_at": pull.get("merged_at"),
            "title": pull.get("title"),
            "linked_issues": [int(number_) for number_ in issues],
            "issues": [],
            "author_was_assigned": None,
            "assigned_to_reporter": None,
            "thread_before_the_pull_request": [],
        }
        for issue_number in issues[:2]:
            issue = gh.json(f"repos/{slug}/issues/{issue_number}")
            if not isinstance(issue, dict) or "pull_request" in issue:
                continue
            assignees = [
                (assignee.get("login") or "")
                for assignee in issue.get("assignees") or []
            ]
            reporter = (issue.get("user") or {}).get("login") or ""
            entry["issues"].append(
                {
                    "number": issue.get("number"),
                    "reporter": reporter,
                    "assignees": assignees,
                    "created_at": issue.get("created_at"),
                    "title": issue.get("title"),
                }
            )
            entry["author_was_assigned"] = author in assignees
            entry["assigned_to_reporter"] = bool(assignees) and reporter in assignees
            comments = gh.json(f"repos/{slug}/issues/{issue_number}/comments")
            opened = pull.get("created_at") or ""
            if isinstance(comments, list):
                entry["thread_before_the_pull_request"] = [
                    {
                        "author": (comment.get("user") or {}).get("login"),
                        "association": comment.get("author_association"),
                        "created_at": comment.get("created_at"),
                        "won": ((comment.get("user") or {}).get("login")) == author,
                        "body": " ".join((comment.get("body") or "").split())[:900],
                    }
                    for comment in comments
                    if not _is_bot(comment.get("user"))
                    and (comment.get("created_at") or "") <= opened
                ]
        rows.append(entry)
    return rows


def collect_target_intel(
    run_directory: Path,
    *,
    repository: str,
    window_days: int = 14,
    merge_paths: int = 6,
    enforcement_samples: int = 8,
    executable: str | None = None,
    timeout_seconds: float = 120,
) -> dict[str, Any]:
    """Record how a target merges outside work, before a run is spent on it."""
    slug = repository_slug(repository)
    gh = _Gh(
        executable or resolve_tool(run_directory, "gh"), run_directory, timeout_seconds
    )
    since = (datetime.now(UTC) - timedelta(days=window_days)).date().isoformat()
    record: dict[str, Any] = {
        "schema_version": TARGET_INTEL_SCHEMA_VERSION,
        "collected_at": datetime.now(UTC).isoformat(),
        "repository": slug,
        "window_days": window_days,
        "since": since,
        "success": False,
    }

    meta = gh.json(f"repos/{slug}")
    if not isinstance(meta, dict):
        record["detail"] = f"{slug} could not be read"
        _write(run_directory, record)
        return record
    record["stars"] = meta.get("stargazers_count")
    record["default_branch"] = meta.get("default_branch")

    closed = gh.pages(f"repos/{slug}/pulls?state=closed&sort=updated&direction=desc", pages=8)
    opened = gh.pages(f"repos/{slug}/pulls?state=open&sort=updated&direction=desc", pages=8)
    if not closed and not opened:
        record["detail"] = "no pull requests could be read"
        _write(run_directory, record)
        return record

    merged_outside = sorted(
        [
            row
            for row in closed
            if row.get("merged_at") and is_outside_human(row)
        ],
        key=lambda row: row["merged_at"],
        reverse=True,
    )
    merged_recent = [row for row in merged_outside if row["merged_at"][:10] >= since]
    merged_bots = [
        row
        for row in closed
        if row.get("merged_at")
        and row["merged_at"][:10] >= since
        and _is_bot(row.get("user"))
    ]
    refused_recent = [
        row
        for row in closed
        if not row.get("merged_at")
        and is_outside_human(row)
        and (row.get("closed_at") or "")[:10] >= since
    ]
    record["freshness"] = {
        "human_outside_merges": len(merged_recent),
        "bot_merges_excluded": len(merged_bots),
        "outside_pull_requests_closed_unmerged": len(refused_recent),
        "latest_human_outside_merge": (
            merged_recent[0]["merged_at"] if merged_recent else None
        ),
        "pull_requests_scanned": len(closed) + len(opened),
    }

    claims = classify_claims(closed + opened)
    issues = gh.pages(
        f"repos/{slug}/issues?state=open&sort=created&direction=desc", pages=6
    )
    open_issues = [row for row in issues if "pull_request" not in row]
    unassigned = [row for row in open_issues if not row.get("assignee")]
    naive = [
        row
        for row in unassigned
        if str(row["number"]) not in (claims["claiming"] | claims["abandoned"])
    ]
    aware = [row for row in unassigned if str(row["number"]) not in claims["claiming"]]
    record["saturation"] = {
        "open_issues_read": len(open_issues),
        "unassigned": len(unassigned),
        "unclaimed_counting_any_pull_request": len(naive),
        "unclaimed_counting_only_open_or_merged": len(aware),
        "candidates": [
            {
                "number": row["number"],
                "created_at": row["created_at"],
                "comments": row.get("comments", 0),
                "labels": [label["name"] for label in row.get("labels") or []],
                "title": row.get("title"),
                "refused_attempts_exist": str(row["number"]) in claims["abandoned"],
            }
            for row in aware[:40]
        ],
    }

    bot_comments: list[dict[str, Any]] = []
    for pull in refused_recent[:enforcement_samples]:
        comments = gh.json(f"repos/{slug}/issues/{pull['number']}/comments")
        if isinstance(comments, list):
            for comment in comments:
                comment["_pull_request"] = pull["number"]
            bot_comments += comments
    record["enforcement"] = enforcement_markers(bot_comments)

    record["merge_path"] = _merge_path_rows(gh, slug, merged_recent, merge_paths)
    judged = [
        row for row in record["merge_path"] if row["author_was_assigned"] is not None
    ]
    assigned = [row for row in judged if row["author_was_assigned"]]
    record["assessment"] = {
        "merge_path_rows_read": len(judged),
        # Stated as a ratio as well as a boolean. Against langchain the strict
        # boolean is False only because a docs pull request merged unassigned
        # while every code fix was assigned, and a reader who sees the ratio
        # asks the right follow-up question where a bare False ends the thought.
        "merges_whose_author_held_the_assignment": len(assigned),
        "assignment_looks_required": bool(judged) and len(assigned) == len(judged),
        "assignment_seen_on_some_merges": bool(assigned),
        "assignment_reaches_non_reporters": any(
            row["author_was_assigned"] and row["assigned_to_reporter"] is False
            for row in judged
        ),
        "automated_enforcement": [entry["marker"] for entry in record["enforcement"]],
        "passes_freshness_bar": len(merged_recent) > 0,
    }
    record["commands"] = gh.commands
    record["read_failures"] = gh.failures
    record["success"] = True
    _write(run_directory, record)
    (run_directory / TARGET_INTEL_MARKDOWN).write_text(
        render_target_intel(record), encoding="utf-8", newline="\n"
    )
    return record


def render_target_intel(record: dict[str, Any]) -> str:
    """Put the merge path in front of a human in the order it should be read."""
    lines = [f"# How {record.get('repository')} hands out work", ""]
    if not record.get("success"):
        lines += [f"The read failed: {record.get('detail', 'unknown')}", ""]
        return "\n".join(lines)
    freshness = record.get("freshness", {})
    saturation = record.get("saturation", {})
    assessment = record.get("assessment", {})
    lines += [
        f"- Stars: {record.get('stars')}",
        f"- Human outside merges in {record.get('window_days')} days: "
        f"{freshness.get('human_outside_merges')} "
        f"(bot merges excluded: {freshness.get('bot_merges_excluded')})",
        f"- Outside pull requests closed unmerged in the same window: "
        f"{freshness.get('outside_pull_requests_closed_unmerged')}",
        f"- Of {saturation.get('unassigned')} unassigned open issues, "
        f"{saturation.get('unclaimed_counting_only_open_or_merged')} have no open "
        f"or merged pull request against them. Counting any referencing pull "
        f"request as a claim, including ones closed unread, would say "
        f"{saturation.get('unclaimed_counting_any_pull_request')}. No label or "
        f"age filter is applied here.",
        "",
    ]
    if record.get("enforcement"):
        lines += ["## Automated rules the repository enforces", ""]
        for entry in record["enforcement"]:
            seen = ", ".join(f"#{number}" for number in entry.get("seen_on", [])[:4])
            lines.append(f"- `{entry['marker']}`, seen {entry['count']} time(s) on {seen}")
            lines.append(f"  > {entry['quote'][:300]}")
        lines.append("")
    lines += ["## What the merges that landed actually did", ""]
    held = assessment.get("merges_whose_author_held_the_assignment", 0)
    read = assessment.get("merge_path_rows_read", 0)
    if read:
        lines.append(
            f"{held} of the {read} outside merges traced here had the linked issue "
            "assigned to the pull request's author."
        )
    if assessment.get("assignment_looks_required"):
        lines.append(
            "That is all of them, so treat assignment as a precondition rather "
            "than a formality: a pull request opened before it is closed unread."
        )
    elif assessment.get("assignment_seen_on_some_merges"):
        lines.append(
            "That is some of them, not all. Read the rows below before deciding "
            "whether the gate applies to the kind of change you intend; on "
            "langchain-ai/langchain it binds code fixes while documentation and "
            "chore pull requests merge without it."
        )
    if assessment.get("assignment_reaches_non_reporters"):
        lines.append(
            "At least one of them was assigned to somebody other than the "
            "issue's reporter, so asking on another person's report can work."
        )
    if read:
        lines.append("")
    for row in record.get("merge_path", []):
        lines.append(
            f"### #{row['pull_request']} by {row['author']} — {row.get('title', '')}"
        )
        for issue in row.get("issues", []):
            lines.append(
                f"- Linked issue #{issue['number']}, reported by "
                f"{issue['reporter']}, assigned to "
                f"{', '.join(issue['assignees']) or 'nobody'}"
            )
        thread = row.get("thread_before_the_pull_request") or []
        if thread:
            lines += ["", "The thread before the pull request opened:", ""]
            for comment in thread:
                mark = "WON" if comment.get("won") else "   "
                lines.append(
                    f"- `{mark}` **{comment['author']}** "
                    f"({comment['association']}): {comment['body'][:400]}"
                )
        lines.append("")
    return "\n".join(lines)


def _write(run_directory: Path, record: dict[str, Any]) -> Path:
    destination = run_directory / TARGET_INTEL_FILENAME
    temporary = destination.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    temporary.replace(destination)
    return destination


def load_target_intel(run_directory: Path) -> dict[str, Any] | None:
    path = run_directory / TARGET_INTEL_FILENAME
    if not path.is_file():
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return loaded if isinstance(loaded, dict) else None
