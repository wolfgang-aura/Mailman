from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mailman.executor import CommandResult, execute
from mailman.redaction import redact
from mailman.toolchain import resolve_tool


PRIOR_ART_FILENAME = "prior-art.json"
PRIOR_ART_MARKDOWN = "prior-art.md"

_PULL_REQUEST_FIELDS = (
    "number,title,state,url,body,author,createdAt,closedAt,mergedAt,"
    "mergeCommit,files,comments,reviews"
)

# GitHub's author association for someone who can merge. A comment from one of
# these is a maintainer's decision; a comment from anyone else is an opinion.
_MAINTAINER_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})

_BODY_CHARACTER_LIMIT = 1200
_COMMENT_CHARACTER_LIMIT = 800


def _trim(text: str | None, limit: int) -> str:
    cleaned = redact((text or "").strip())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[:limit].rstrip() + "\n\n_(truncated)_"


def _comment_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for source, kind in ((payload.get("comments"), "comment"), (payload.get("reviews"), "review")):
        if not isinstance(source, list):
            continue
        for entry in source:
            if not isinstance(entry, dict):
                continue
            author = entry.get("author")
            login = author.get("login") if isinstance(author, dict) else None
            association = (entry.get("authorAssociation") or "").upper()
            body = entry.get("body") or ""
            if not body.strip():
                continue
            rows.append(
                {
                    "kind": kind,
                    "author": login,
                    "association": association,
                    "maintainer": association in _MAINTAINER_ASSOCIATIONS,
                    "state": entry.get("state"),
                    "body": _trim(body, _COMMENT_CHARACTER_LIMIT),
                }
            )
    return rows


def _outcome(payload: dict[str, Any]) -> str:
    if payload.get("mergedAt"):
        return "merged"
    if payload.get("closedAt"):
        return "closed unmerged"
    return "open"


def summarize_pull_request(payload: dict[str, Any]) -> dict[str, Any]:
    """Reduce one pull request to what an engineer needs before retrying it.

    A merged pull request's body and files are withheld. Mailman must not hand
    an agent the accepted fix, which is the same rule that keeps comments out of
    the captured issue. A closed attempt is the opposite case: why it was
    rejected is exactly the context that stops the next attempt repeating it.
    """
    outcome = _outcome(payload)
    author = payload.get("author")
    files = payload.get("files")
    changed = [
        entry.get("path")
        for entry in files
        if isinstance(entry, dict) and entry.get("path")
    ] if isinstance(files, list) else []
    summary: dict[str, Any] = {
        "number": payload.get("number"),
        "title": payload.get("title"),
        "url": payload.get("url"),
        "outcome": outcome,
        "author": author.get("login") if isinstance(author, dict) else None,
        "created_at": payload.get("createdAt"),
        "closed_at": payload.get("closedAt"),
        "withheld": outcome == "merged",
    }
    if outcome == "merged":
        summary["body"] = None
        summary["changed_files"] = []
        summary["comments"] = []
        # The merge commit is a forty character name, not the fix. Recording it
        # is what lets `check-target` ask whether this merge is already an
        # ancestor of the run's base commit, which is the difference between
        # "upstream already ships this" and "upstream shipped something else to
        # the same function". See
        # https://github.com/wolfgang-aura/Mailman/issues/46.
        merge_commit = payload.get("mergeCommit")
        summary["merge_commit"] = (
            merge_commit.get("oid") if isinstance(merge_commit, dict) else None
        )
        return summary
    summary["body"] = _trim(payload.get("body"), _BODY_CHARACTER_LIMIT)
    summary["changed_files"] = changed
    summary["comments"] = _comment_rows(payload)
    return summary


def render_prior_art(record: dict[str, Any]) -> str:
    """Render the section both prompts carry."""
    attempts = record.get("attempts", [])
    lines = [
        "# What has already been tried",
        "",
        f"Searched on {record.get('collected_at', 'an unrecorded date')} in "
        f"`{record.get('repository', 'the target repository')}`.",
        "",
    ]
    if not attempts:
        lines.extend(
            [
                "No earlier pull request was found for this issue. That is a",
                "reason for care, not for confidence: the search may simply have",
                "missed one.",
                "",
            ]
        )
        return "\n".join(lines)
    open_attempts = [item for item in attempts if item["outcome"] == "open"]
    closed = [item for item in attempts if item["outcome"] == "closed unmerged"]
    lines.append(
        f"{len(attempts)} related pull request(s): {len(open_attempts)} open, "
        f"{len(closed)} closed without merging."
    )
    lines.append("")
    if open_attempts:
        lines.extend(
            [
                "**An open pull request already claims this issue.** Nothing below",
                "is worth writing until a maintainer says otherwise.",
                "",
            ]
        )
    if closed:
        lines.extend(
            [
                "A closed attempt means someone already wrote this fix and it was",
                "not accepted. Read why before writing the same thing again.",
                "",
            ]
        )
    for item in attempts:
        lines.append(f"## #{item['number']} — {item['title']}")
        lines.append("")
        lines.append(f"- Outcome: **{item['outcome']}**")
        lines.append(f"- URL: {item['url']}")
        if item["changed_files"]:
            listed = ", ".join(f"`{path}`" for path in item["changed_files"][:10])
            lines.append(f"- Files touched: {listed}")
        lines.append("")
        if item["withheld"]:
            lines.extend(
                [
                    "This one was merged. Its contents are deliberately withheld so",
                    "that the accepted change cannot be copied.",
                    "",
                ]
            )
            continue
        if item["body"]:
            lines.extend(["### What it claimed", "", item["body"], ""])
        maintainer_comments = [row for row in item["comments"] if row["maintainer"]]
        other_comments = [row for row in item["comments"] if not row["maintainer"]]
        if maintainer_comments:
            lines.extend(["### Maintainer response", ""])
            for row in maintainer_comments:
                label = f"{row['author']} ({row['association'].lower()})"
                state = f", {row['state'].lower()}" if row.get("state") else ""
                lines.extend([f"**{label}{state}:**", "", row["body"], ""])
        if other_comments:
            lines.extend(
                [
                    f"_{len(other_comments)} other comment(s) from non-maintainers, "
                    "not reproduced._",
                    "",
                ]
            )
        if item["outcome"] == "open":
            lines.extend(
                [
                    "This one is still open, so someone is already working on this",
                    "issue. A second pull request would be a duplicate.",
                    "",
                ]
            )
        elif not item["comments"]:
            lines.extend(
                [
                    "Closed with no comment at all. Silence from a maintainer is",
                    "usually a judgement about the approach, not an oversight.",
                    "",
                ]
            )
    return "\n".join(lines)


def collect_prior_art(
    run_directory: Path,
    *,
    repository: str,
    numbers: list[int],
    executable: str | None = None,
    timeout_seconds: float = 60,
) -> dict[str, Any]:
    """Read each earlier pull request and write the prior art record."""
    slug = repository.removesuffix(".git").rstrip("/")
    for prefix in ("https://github.com/", "git@github.com:", "ssh://git@github.com/"):
        slug = slug.removeprefix(prefix)
    command_executable = executable or resolve_tool(run_directory, "gh")
    record: dict[str, Any] = {
        "schema_version": 1,
        "collected_at": datetime.now(UTC).isoformat(),
        "repository": slug,
        "requested": numbers,
        "attempts": [],
        "commands": [],
        "success": False,
    }
    for number in numbers:
        result: CommandResult = execute(
            [
                command_executable,
                "pr",
                "view",
                str(number),
                "--repo",
                slug,
                "--json",
                _PULL_REQUEST_FIELDS,
            ],
            working_directory=run_directory,
            timeout_seconds=timeout_seconds,
        )
        record["commands"].append(result.to_dict())
        if result.timed_out or result.exit_code != 0:
            record["detail"] = f"pull request {number} could not be read"
            _write(run_directory, record)
            return record
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            record["detail"] = f"the GitHub CLI returned unreadable JSON: {error}"
            _write(run_directory, record)
            return record
        if isinstance(payload, dict):
            record["attempts"].append(summarize_pull_request(payload))
    record["success"] = True
    record["attempt_count"] = len(record["attempts"])
    record["closed_unmerged"] = sum(
        1 for item in record["attempts"] if item["outcome"] == "closed unmerged"
    )
    record["open"] = sum(1 for item in record["attempts"] if item["outcome"] == "open")
    _write(run_directory, record)
    (run_directory / PRIOR_ART_MARKDOWN).write_text(
        render_prior_art(record), encoding="utf-8", newline="\n"
    )
    return record


def load_prior_art_markdown(run_directory: Path) -> str | None:
    path = run_directory / PRIOR_ART_MARKDOWN
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8").strip()
    return text or None


def _write(run_directory: Path, record: dict[str, Any]) -> Path:
    path = run_directory / PRIOR_ART_FILENAME
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8", newline="\n")
    return path
