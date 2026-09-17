from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mailman.executor import CommandResult, execute
from mailman.redaction import redact
from mailman.targeting import (
    attempt_is_dormant,
    is_stale_attempt,
    stale_attempt_row,
)
from mailman.toolchain import resolve_tool


PRIOR_ART_FILENAME = "prior-art.json"
PRIOR_ART_MARKDOWN = "prior-art.md"

#: What a reference the issue thread names turned out to be. Written beside the
#: duplicate search so a rejection can be read back: which reference, in which
#: repository, in what state.
CITED_PULL_REQUESTS_FILENAME = "cited-pull-requests.json"
# `updatedAt` is what says whether an open attempt is still moving, and
# `isDraft` is what a reader needs to judge one that is not. Both are cheap;
# neither was asked for while an open attempt was a flat refusal.
#: `author` is here so the closer can be compared against it: an author who
#: closes their own pull request has withdrawn it, and a maintainer who closes
#: it has rejected it. Those are opposite facts.
_CITED_FIELDS = (
    "number,state,mergedAt,mergeCommit,title,url,createdAt,updatedAt,isDraft,author"
)

_PULL_REQUEST_FIELDS = (
    "number,title,state,url,body,author,createdAt,updatedAt,closedAt,mergedAt,"
    "mergeCommit,isDraft,files,comments,reviews"
)

# GitHub's author association for someone who can merge. A comment from one of
# these is a maintainer's decision; a comment from anyone else is an opinion.
# The same list decides what a closure means: see `closing_actor`.
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
        # The last activity, which is what decides whether an open attempt
        # still claims the issue. See targeting.STALE_ATTEMPT_DAYS.
        "updated_at": payload.get("updatedAt"),
        "closed_at": payload.get("closedAt"),
        "is_draft": payload.get("isDraft"),
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
    # An attempt nobody has touched in months does not claim anything, so it
    # must not be announced as a reason to stop writing.
    live = [item for item in open_attempts if not is_stale_attempt(item)]
    lines.append(
        f"{len(attempts)} related pull request(s): {len(open_attempts)} open, "
        f"{len(closed)} closed without merging."
    )
    lines.append("")
    if live:
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
        if item.get("maintainer_closed"):
            closer = item.get("closed_by") or {}
            lines.extend(
                [
                    "**A maintainer closed this one:** "
                    f"{closer.get('detail', 'closed by the project')}. That is a "
                    "judgement about the change, not a branch somebody "
                    "abandoned. Re-filing it is how a contributor gets banned.",
                    "",
                ]
            )
        if item["outcome"] == "open" and item in live:
            lines.extend(
                [
                    "This one is still open, so someone is already working on this",
                    "issue. A second pull request would be a duplicate.",
                    "",
                ]
            )
        elif item["outcome"] == "open":
            lines.extend(
                [
                    "This one is open but dormant: nobody has touched it for months.",
                    "It is a prior attempt, not a claim. Read its diff and its review",
                    "comments before you start, and say in your pull request body that",
                    "yours supersedes it.",
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
        "schema_version": 2,
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
        if not isinstance(payload, dict):
            continue
        summary = summarize_pull_request(payload)
        if summary["outcome"] == "closed unmerged":
            # A maintainer's closure is a decision about the change; the
            # author's own is a withdrawal. `check-target` reads this to tell
            # a rejected attempt from a dormant one.
            summary["closed_by"] = closing_actor(
                run_directory,
                executable=command_executable,
                slug=slug,
                number=summary["number"],
                author=summary["author"],
                timeout_seconds=timeout_seconds,
                commands=record["commands"],
            )
            summary["maintainer_closed"] = bool(summary["closed_by"]["maintainer"])
        record["attempts"].append(summary)
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


def resolve_cited_pull_requests(
    run_directory: Path,
    *,
    references: Sequence[dict[str, Any]],
    executable: str | None = None,
    timeout_seconds: float = 60,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Ask GitHub what each reference the issue names actually is.

    One `gh pr view` per reference, in any repository. A reference that turns
    out to be an issue, a heading anchor or a version number is skipped without
    comment: `gh` exits non-zero and that is the whole answer.

    A merged pull request is recorded as merged, full stop. This stage has no
    clone, so it cannot ask whether the merge commit is already an ancestor of
    a base commit the way `check-target` can; the merge commit is recorded so a
    later stage can. See https://github.com/wolfgang-aura/Mailman/issues/98.

    An open attempt that has not moved for `STALE_ATTEMPT_DAYS`, and one closed
    without merging, is recorded under `stale` rather than `open`: it is a
    prior attempt to read, not a claim on the issue.
    """
    command_executable = executable or resolve_tool(run_directory, "gh")
    record: dict[str, Any] = {
        "schema_version": 3,
        "collected_at": datetime.now(UTC).isoformat(),
        "references": list(references),
        "resolved": [],
        "skipped": [],
        "open": [],
        "merged": [],
        "stale": [],
        "maintainer_closed": [],
        "decided_by": None,
        "commands": [],
        "success": True,
    }
    for reference in references:
        slug = str(reference.get("repository") or "")
        number = reference.get("number")
        if not slug or not isinstance(number, int):
            continue
        result: CommandResult = execute(
            [
                command_executable,
                "pr",
                "view",
                str(number),
                "--repo",
                slug,
                "--json",
                _CITED_FIELDS,
            ],
            working_directory=run_directory,
            timeout_seconds=timeout_seconds,
        )
        record["commands"].append(result.to_dict())
        if result.timed_out or result.exit_code != 0:
            record["skipped"].append(
                {
                    **reference,
                    "detail": "not a pull request, or not readable from here",
                }
            )
            continue
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            record["skipped"].append(
                {**reference, "detail": "the GitHub CLI returned unreadable JSON"}
            )
            continue
        if not isinstance(payload, dict):
            record["skipped"].append(
                {**reference, "detail": "the GitHub CLI returned no pull request"}
            )
            continue
        merge_commit = payload.get("mergeCommit")
        writer = payload.get("author")
        row = {
            "reference": reference.get("text"),
            "in": reference.get("in"),
            "repository": slug,
            "number": payload.get("number", number),
            "state": str(payload.get("state") or "").upper(),
            "title": payload.get("title"),
            "url": payload.get("url"),
            "merged_at": payload.get("mergedAt"),
            "created_at": payload.get("createdAt"),
            "updated_at": payload.get("updatedAt"),
            "is_draft": payload.get("isDraft"),
            "author": writer.get("login") if isinstance(writer, dict) else None,
            "author_association": None,
            "closed_by": None,
            "maintainer_closed": False,
            "merge_commit": (
                merge_commit.get("oid") if isinstance(merge_commit, dict) else None
            ),
        }
        if attempt_is_dormant(row, now=now):
            # Only now, and only for an attempt that has already stopped
            # moving. `gh pr view` has no author association at all, so this
            # costs one extra call — on the handful of references that are
            # about to stop blocking, never on the ones that already do.
            row["author_association"] = _author_association(
                run_directory,
                executable=command_executable,
                slug=slug,
                number=row["number"],
                timeout_seconds=timeout_seconds,
                commands=record["commands"],
            )
            if row["state"] == "CLOSED" and not row["merged_at"]:
                # Who closed it decides what the closure meant, and the same
                # rule applies: paid for only where it can change the answer.
                row["closed_by"] = closing_actor(
                    run_directory,
                    executable=command_executable,
                    slug=slug,
                    number=row["number"],
                    author=row["author"],
                    timeout_seconds=timeout_seconds,
                    commands=record["commands"],
                )
                row["maintainer_closed"] = bool(row["closed_by"]["maintainer"])
        record["resolved"].append(row)
        if row["maintainer_closed"]:
            record["maintainer_closed"].append(stale_attempt_row(row, now=now))
        elif is_stale_attempt(row, now=now):
            record["stale"].append(stale_attempt_row(row, now=now))
        elif row["state"] == "OPEN":
            record["open"].append(row)
        elif row["state"] == "MERGED":
            record["merged"].append(row)
    decided = (record["open"] or record["merged"] or [None])[0]
    record["decided_by"] = decided
    record["detail"] = _cited_detail(record)
    path = run_directory / CITED_PULL_REQUESTS_FILENAME
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8", newline="\n")
    return record


def _author_association(
    run_directory: Path,
    *,
    executable: str,
    slug: str,
    number: Any,
    timeout_seconds: float,
    commands: list[dict[str, Any]],
) -> str | None:
    """Who the attempt's author is to this repository, in GitHub's own word.

    `gh pr view` does not carry the author association at all, so the REST
    endpoint is the only place to read it. A dormant attempt written by an
    OWNER or a MEMBER is a maintainer's own work in progress and still claims
    the issue; one written by anybody else has stopped claiming it.
    """
    if not isinstance(number, int):
        return None
    result: CommandResult = execute(
        [
            executable,
            "api",
            f"repos/{slug}/pulls/{number}",
            "--jq",
            ".author_association",
        ],
        working_directory=run_directory,
        timeout_seconds=timeout_seconds,
    )
    commands.append(result.to_dict())
    if result.timed_out or result.exit_code != 0:
        return None
    association = (result.stdout or "").strip().strip('"').upper()
    return association or None


def _api(
    run_directory: Path,
    *,
    executable: str,
    path: str,
    timeout_seconds: float,
    commands: list[dict[str, Any]],
) -> Any | None:
    """One `gh api` read, recorded in the same command list as everything else."""
    result: CommandResult = execute(
        [executable, "api", path],
        working_directory=run_directory,
        timeout_seconds=timeout_seconds,
    )
    commands.append(result.to_dict())
    if result.timed_out or result.exit_code != 0:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def closing_actor(
    run_directory: Path,
    *,
    executable: str,
    slug: str,
    number: Any,
    author: str | None,
    timeout_seconds: float,
    commands: list[dict[str, Any]],
) -> dict[str, Any]:
    """Who closed this pull request, and whether they speak for the project.

    A closed attempt means two opposite things depending on who closed it.
    tqdm#1816 and #1818 were closed by their own authors: nobody judged the
    change, and that is the shape the stale-attempt rule is for. skfolio#307
    and wagtail#14384 were closed by maintainers who had rejected the change,
    and both passed the pre-screen as supersedable stale attempts anyway.

    `gh pr view` does not carry the closure at all. The issue timeline does:
    the last `closed` event names the actor, and the entries that carry an
    `author_association` say what that actor is to this repository. When the
    timeline cannot be read, the issue's own `closed_by` names the actor
    without an association, which is enough to tell a self-withdrawal from
    everything else.
    """
    found: dict[str, Any] = {
        "login": None,
        "association": None,
        "maintainer": False,
        "source": None,
        "detail": "who closed it could not be determined",
    }
    if not isinstance(number, int):
        return found
    associations: dict[str, str] = {}
    login: str | None = None
    association: str | None = None
    events = _api(
        run_directory,
        executable=executable,
        path=f"repos/{slug}/issues/{number}/timeline?per_page=100",
        timeout_seconds=timeout_seconds,
        commands=commands,
    )
    for event in events if isinstance(events, list) else []:
        if not isinstance(event, dict):
            continue
        actor = event.get("actor") if isinstance(event.get("actor"), dict) else None
        actor = actor or (event.get("user") if isinstance(event.get("user"), dict) else None)
        name = actor.get("login") if isinstance(actor, dict) else None
        kind = str(event.get("author_association") or "").upper()
        if name and kind:
            associations[name] = kind
        # Reopened and closed again: the last closure is the one that stands.
        if event.get("event") == "closed" and name:
            login = name
            association = kind or None
            found["source"] = "timeline"
    if login is None:
        issue = _api(
            run_directory,
            executable=executable,
            path=f"repos/{slug}/issues/{number}",
            timeout_seconds=timeout_seconds,
            commands=commands,
        )
        closed_by = issue.get("closed_by") if isinstance(issue, dict) else None
        if isinstance(closed_by, dict) and closed_by.get("login"):
            login = str(closed_by["login"])
            found["source"] = "closed_by"
    if login is None:
        return found
    association = association or associations.get(login)
    found["login"] = login
    found["association"] = association
    if author and login == author:
        found["detail"] = f"{login} closed their own pull request"
        return found
    if association in _MAINTAINER_ASSOCIATIONS:
        found["maintainer"] = True
        found["detail"] = (
            f"{login} ({association.lower()}) closed it, and did not write it"
        )
        return found
    found["detail"] = (
        f"{login} closed it, association "
        f"{association.lower() if association else 'unknown'}"
    )
    return found


def _cited_detail(record: dict[str, Any]) -> str:
    """One line naming the reference that decided it, in the words it was written."""
    decided = record.get("decided_by")
    stale = record.get("stale") or []
    rejected = record.get("maintainer_closed") or []
    if not decided:
        base = (
            f"{len(record.get('references') or [])} reference(s) read from the "
            "issue, none of them an open or merged pull request"
        )
        if rejected:
            named = ", ".join(
                f"{row.get('repository')}#{row.get('number')} "
                f"({(row.get('closed_by') or {}).get('detail', 'closed')})"
                for row in rejected
            )
            base += (
                f". {len(rejected)} attempt(s) a maintainer closed: {named}. "
                "That is a judgement about the change, not a dormant branch to "
                "supersede"
            )
        if not stale:
            return base
        named = ", ".join(
            f"{row.get('repository')}#{row.get('number')} ({row.get('state')}"
            + (
                f", {row['days_stale']} days since its last activity)"
                if row.get("days_stale") is not None
                else ")"
            )
            for row in stale
        )
        return (
            f"{base}. {len(stale)} stale prior attempt(s) the issue names: "
            f"{named}. Read each one before starting, and say in the pull "
            "request body that yours supersedes it"
        )
    state = "is open" if decided["state"] == "OPEN" else "was merged"
    tail = (
        ""
        if decided["state"] == "OPEN"
        else (
            ". This stage has no clone, so whether that merge is already in a "
            "base commit is not checked here"
        )
    )
    return (
        f"the issue names {decided['reference']} — "
        f"{decided['repository']}#{decided['number']}, which {state}: "
        f"{decided['url']}{tail}"
    )


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
