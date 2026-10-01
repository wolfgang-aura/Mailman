from __future__ import annotations

import json
import re
from collections.abc import Collection, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mailman.executor import CommandResult, execute
from mailman.maintainers import is_maintainer
from mailman.redaction import redact
from mailman.targeting import (
    attempt_is_closed_unmerged,
    attempt_is_dormant,
    attempt_is_project_voice,
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
#: `comments,reviews` answer whether the attempt is waiting on a maintainer
#: rather than abandoned; see `awaits_maintainer`.
_CITED_FIELDS = (
    "number,state,mergedAt,mergeCommit,title,url,createdAt,updatedAt,isDraft,author,"
    "comments,reviews,body,closingIssuesReferences,files"
)


def _is_test_path(path: str) -> bool:
    """A file a test suite owns: under a tests directory, or named as a test."""
    parts = [part.lower() for part in path.replace("\\", "/").split("/") if part]
    if not parts:
        return False
    name = parts[-1]
    return (
        any(part in {"test", "tests", "testing"} for part in parts[:-1])
        or name.startswith("test_")
        or name.endswith(("_test.py", "_tests.py"))
        or name == "conftest.py"
    )


def _touches_only_tests(payload: dict[str, Any]) -> bool:
    """True only when GitHub listed the files and every one is a test file."""
    files = payload.get("files")
    if not isinstance(files, list) or not files:
        return False
    paths = [str(row.get("path") or "") for row in files if isinstance(row, dict)]
    return len(paths) == len(files) and all(_is_test_path(path) for path in paths)

_PULL_REQUEST_FIELDS = (
    "number,title,state,url,body,author,createdAt,updatedAt,closedAt,mergedAt,"
    "mergeCommit,headRefOid,isDraft,files,comments,reviews,closingIssuesReferences"
)

# GitHub's author association for someone who can merge. A comment from one of
# these is a maintainer's decision; a comment from anyone else is an opinion.
# The same list decides what a closure means: see `closing_actor`.
_MAINTAINER_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})

#: GitHub's closing keywords. A pull request body that uses one against an
#: issue says the pull request is that issue's fix.
_CLOSING_KEYWORD = r"(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)"

_BODY_CHARACTER_LIMIT = 1200
_COMMENT_CHARACTER_LIMIT = 800
# Pages of a pull request timeline read for its last closure.
_TIMELINE_PAGES = 5


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
    # Inline review comments, in the REST shape `pulls/N/comments` returns. A
    # review that says "Some notes below." keeps its substance here: on
    # typeshed#15497 the required design was only inline. Mailman #308.
    inline = payload.get("reviewComments")
    for entry in inline if isinstance(inline, list) else []:
        if not isinstance(entry, dict) or not (entry.get("body") or "").strip():
            continue
        user = entry.get("user")
        association = (entry.get("author_association") or "").upper()
        line = entry.get("line") or entry.get("original_line")
        path = entry.get("path") or ""
        rows.append(
            {
                "kind": "inline",
                "author": user.get("login") if isinstance(user, dict) else None,
                "association": association,
                "maintainer": association in _MAINTAINER_ASSOCIATIONS,
                "state": None,
                "path": f"{path}:{line}" if path and line else path or None,
                "body": _trim(entry["body"], _COMMENT_CHARACTER_LIMIT),
            }
        )
    return rows


#: A project voice doubting that a person wrote the attempt. towncrier#756 was
#: closed by its own author, but only after two members asked "was any LLM
#: used to generate this PR" and quoted GPTZero. Another agent-written pull
#: request on the same issue is the pattern they named. Mailman #304.
#:
#: Each form names the pull request or its author, not AI in general: on an
#: agent or LLM framework "this is an LLM provider bug", "the AI-generated
#: response" and "the code generated by the LLM" are the project's subject.
#: The forms are pinned by a table test of both kinds of sentence (#332).
_AI_TOOL = r"(?:llms?|ai|chatgpt|copilot|claude|gpt-?\d*)"
_AI_WROTE = rf"(?:{_AI_TOOL}|machine)[- ](?:generated|written|authored)"
_AI_MADE = rf"(?:{_AI_TOOL}|machine)[- ](?:generated|written|authored|assisted)"
_WORK = (
    r"(?:pr|pull request|change|changes|code|patch|diff|commit|contribution|"
    r"submission|description)s?"
)
_THE_WORK = rf"(?:this|the|your)(?: \w+)? {_WORK}"
_AI_AUTHORSHIP_DOUBT = re.compile(
    r"\b(?:"
    # "was any LLM used", "were any AI tools used", "was Claude Code used"
    rf"(?:was|were) (?:any |an |the )?{_AI_TOOL}"
    r"(?: (?:tools?|assistants?|assistance|code|models?))? (?:used|involved)"
    rf"|did you (?:use|write this with) (?:any |an |the )?{_AI_TOOL}\b"
    # "is this AI-generated", "this PR is clearly AI generated", "looks AI
    # generated to me". A bare "is" is left out: "the output is AI-generated".
    rf"|(?:is|was) (?:this|it)(?: \w+)? {_AI_MADE}"
    rf"|(?:{_THE_WORK} (?:is|was)|(?:{_THE_WORK} )?(?:looks|seems|reads))"
    rf"(?: \w+ly)?(?: like)?(?: it was)?(?: an?)? {_AI_MADE}"
    # "we do not accept AI-generated pull requests", but not "we welcome
    # AI-assisted contributions"
    rf"|{_AI_WROTE} (?:prs?|pull requests?|code|patch(?:es)?|changes?|contributions?|submissions?)\b"
    # "was this written by AI", "was this code written with an LLM", "looks
    # like it was generated by an LLM". The work needs a question or "this":
    # "the code generated by the LLM is invalid" is a bug report.
    rf"|(?:(?:is|was|were) (?:{_THE_WORK}|this|it)|(?:this|your)(?: \w+)? {_WORK}"
    r"|(?:looks|seems) like (?:this|it) (?:is|was))"
    r" (?:been )?(?:generated|written|authored) (?:by|with|using) "
    rf"(?:an? |the )?{_AI_TOOL}\b"
    r"|gptzero|pangram"
    r")",
    re.IGNORECASE,
)


def ai_authorship_doubt(
    payload: dict[str, Any], maintainers: Collection[str] = ()
) -> str | None:
    """The login of a project voice who questioned AI authorship, if any."""
    for source in ("comments", "reviews"):
        entries = payload.get(source)
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, dict):
                continue
            who = entry.get("author")
            login = who.get("login") if isinstance(who, dict) else None
            association = (entry.get("authorAssociation") or "").upper()
            if not is_maintainer(
                {"association": association, "login": login}, maintainers
            ):
                continue
            if _AI_AUTHORSHIP_DOUBT.search(str(entry.get("body") or "")):
                return login or association.lower()
    return None


def _note_ai_doubt(
    closed_by: dict[str, Any],
    payload: dict[str, Any],
    maintainers: Collection[str] = (),
) -> dict[str, Any]:
    """Count a withdrawal under AI-authorship doubt as the project saying no."""
    if closed_by.get("maintainer"):
        return closed_by
    doubter = ai_authorship_doubt(payload, maintainers)
    if not doubter:
        return closed_by
    return {
        **closed_by,
        "maintainer": True,
        "detail": f"{doubter} questioned AI authorship before it was closed",
    }


def awaits_maintainer(
    payload: dict[str, Any], maintainers: Collection[str] = ()
) -> bool:
    """Whether the author answered a maintainer last, so the next move is theirs.

    An open attempt that has sat for months is abandoned only if its author
    stopped. When a maintainer reviewed it and the author replied after, the
    silence is the maintainer's, and a second pull request would compete with
    work they are still weighing. copier-org/copier#2754 is the case: reviewed
    and answered the same day, then idle for 85 days. See
    https://github.com/wolfgang-aura/Mailman/issues/157.

    Empty-bodied reviews count: a reply to an inline thread is one.
    """
    writer = payload.get("author")
    author = writer.get("login") if isinstance(writer, dict) else None
    if not author:
        return False
    last_maintainer = last_author = ""
    for source, stamp in (("comments", "createdAt"), ("reviews", "submittedAt")):
        entries = payload.get(source)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            when = str(entry.get(stamp) or "")
            if not when:
                continue
            who = entry.get("author")
            login = who.get("login") if isinstance(who, dict) else None
            association = (entry.get("authorAssociation") or "").upper()
            if login == author:
                last_author = max(last_author, when)
            elif is_maintainer(
                {"association": association, "login": login}, maintainers
            ):
                last_maintainer = max(last_maintainer, when)
    return bool(last_maintainer) and last_author > last_maintainer


def closes_issue(body: Any, *, repository: str, number: int | None) -> bool:
    """Whether a pull request body uses a closing keyword on this issue.

    `Fixes #7`, `fixes owner/name#7` and `Closes <issue URL>` all count; a
    mention without the keyword, or a keyword on another issue, does not.
    """
    if not isinstance(body, str) or not body or not isinstance(number, int):
        return False
    slug = re.escape(repository)
    target = (
        rf"(?:(?:{slug})?#{number}"
        rf"|https?://github\.com/{slug}/issues/{number})(?![\w/])"
    )
    pattern = rf"\b{_CLOSING_KEYWORD}\b:?\s+{target}"
    return re.search(pattern, body, flags=re.IGNORECASE) is not None


def row_state_open(payload: dict[str, Any]) -> bool:
    return str(payload.get("state") or "").upper() == "OPEN"


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
        "awaiting_maintainer": outcome == "open" and awaits_maintainer(payload),
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
        # A rewritten history drops the merge sha but keeps the branch head.
        # Mailman #298.
        summary["head_sha"] = payload.get("headRefOid")
        # Whether it only added tests, and what GitHub says it closed: a
        # skip predicate naming the issue is not its fix. The paths stay
        # withheld; only the verdict is kept. Mailman #368.
        summary["test_only"] = _touches_only_tests(payload)
        summary["closes"] = _closing_references(payload.get("closingIssuesReferences"))
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
        if item.get("author"):
            association = item.get("author_association")
            lines.append(
                f"- Author: {item['author']}"
                + (f" ({association.lower()})" if association else "")
                + (", speaks for the project" if item.get("author_is_project_voice") else "")
            )
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
                where = f" on `{row['path']}`" if row.get("path") else ""
                lines.extend([f"**{label}{state}{where}:**", "", row["body"], ""])
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
    maintainers: Collection[str] = (),
) -> dict[str, Any]:
    """Read each earlier pull request and write the prior art record.

    `maintainers` is the login set the repository screen recorded. A timeline
    `closed` event carries no association, so without it a silent close by a
    maintainer reads as an outsider's. Mailman #345.
    """
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
        if _outcome(payload) != "merged":
            # `gh pr view` carries review summaries, not the inline comments
            # under them. An unreadable list leaves the attempt as it was.
            inline = _api(
                run_directory,
                executable=command_executable,
                path=f"repos/{slug}/pulls/{number}/comments?per_page=100",
                timeout_seconds=timeout_seconds,
                commands=record["commands"],
            )
            if isinstance(inline, list):
                payload["reviewComments"] = inline
        summary = summarize_pull_request(payload)
        # Who the author is to the project. A project voice's closed attempt
        # is its own fix parked, not an outsider's abandoned one; without the
        # association the stale rule called marimo#9862 abandoned. `gh pr view`
        # does not carry it, so a merged attempt, which nothing asks about,
        # does not pay the extra call. Mailman #199.
        association = _first_association(payload)
        if association is None and summary["outcome"] != "merged":
            association = _author_association(
                run_directory,
                executable=command_executable,
                slug=slug,
                number=summary["number"],
                timeout_seconds=timeout_seconds,
                commands=record["commands"],
            )
        writer = {"association": association, "author": summary["author"]}
        summary["author_association"] = association
        summary["author_is_maintainer"] = is_maintainer(
            writer, maintainers, associations=frozenset({"OWNER", "MEMBER"})
        )
        summary["author_is_project_voice"] = is_maintainer(writer, maintainers)
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
                maintainers=maintainers,
            )
            summary["closed_by"] = _note_ai_doubt(
                summary["closed_by"], payload, maintainers
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


def _closing_references(value: object) -> list[str]:
    """`closingIssuesReferences` as `owner/repo#N` strings."""
    closes: list[str] = []
    for entry in value if isinstance(value, list) else []:
        if not isinstance(entry, dict) or not isinstance(entry.get("number"), int):
            continue
        repository = entry.get("repository")
        owner = repository.get("owner") if isinstance(repository, dict) else None
        name = repository.get("name") if isinstance(repository, dict) else None
        login = owner.get("login") if isinstance(owner, dict) else None
        if login and name:
            closes.append(f"{login}/{name}#{entry['number']}".lower())
    return closes


#: What `gh pr view` says when the number is an issue, or the repository or
#: number does not exist: GitHub's answer that this is not a pull request.
#: Anything else (a rate limit, a timeout, a network error) is no answer at
#: all. Mailman #344.
_NOT_A_PULL_REQUEST = re.compile(
    r"Could not resolve to a (?:PullRequest|Repository)\b|no pull requests? found",
    re.IGNORECASE,
)


def resolve_cited_pull_requests(
    run_directory: Path,
    *,
    references: Sequence[dict[str, Any]],
    executable: str | None = None,
    timeout_seconds: float = 60,
    now: datetime | None = None,
    repository: str | None = None,
    issue_number: int | None = None,
    maintainers: Collection[str] = (),
) -> dict[str, Any]:
    """Ask GitHub what each reference the issue names actually is.

    One `gh pr view` per reference, in any repository. A reference that turns
    out to be an issue, a heading anchor or a version number is skipped without
    comment: GitHub says it cannot resolve that pull request. Any other failure
    is recorded under `unread` and the record is not a success: an open cited
    pull request read as "not a PR" cleared the issue (#344).

    A merged pull request is recorded as merged, full stop. This stage has no
    clone, so it cannot ask whether the merge commit is already an ancestor of
    a base commit the way `check-target` can; the merge commit is recorded so a
    later stage can. See https://github.com/wolfgang-aura/Mailman/issues/98.

    An open attempt that has not moved for `STALE_ATTEMPT_DAYS`, and one closed
    without merging, is recorded under `stale` rather than `open`: it is a
    prior attempt to read, not a claim on the issue.

    `maintainers` is the login set the repository screen recorded for
    `repository`; it counts only for pull requests in that repository. A
    closed, dormant attempt a maintainer wrote whose body closes
    `issue_number` is recorded under `maintainer_owned`: the project's own
    parked fix. Mailman #203.
    """
    command_executable = executable or resolve_tool(run_directory, "gh")
    record: dict[str, Any] = {
        "schema_version": 3,
        "collected_at": datetime.now(UTC).isoformat(),
        "references": list(references),
        "resolved": [],
        "skipped": [],
        "unread": [],
        "open": [],
        "merged": [],
        # Merged, but only tests that name the issue, and not closing it: a
        # skip predicate or an xfail, not the fix. Mailman #368.
        "test_only": [],
        "stale": [],
        "maintainer_closed": [],
        "maintainer_owned": [],
        "decided_by": None,
        "commands": [],
        "success": True,
    }

    def unread(reference: dict[str, Any], detail: str) -> None:
        record["unread"].append({**reference, "detail": detail})
        record["success"] = False

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
            stderr = str(getattr(result, "stderr", "") or "")
            if not result.timed_out and _NOT_A_PULL_REQUEST.search(stderr):
                record["skipped"].append(
                    {**reference, "detail": "not a pull request"}
                )
            else:
                first = stderr.strip().splitlines()[0] if stderr.strip() else ""
                unread(
                    reference,
                    "timed out" if result.timed_out else first or "gh failed",
                )
            continue
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            unread(reference, "the GitHub CLI returned unreadable JSON")
            continue
        if not isinstance(payload, dict):
            unread(reference, "the GitHub CLI returned no pull request")
            continue
        merge_commit = payload.get("mergeCommit")
        writer = payload.get("author")
        # The recorded maintainer set belongs to the issue's repository.
        home = bool(repository) and slug.lower() == str(repository).lower()
        known = maintainers if home else ()
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
            "author_is_maintainer": False,
            "closes_issue": home
            and closes_issue(
                payload.get("body"), repository=slug, number=issue_number
            ),
            "closed_by": None,
            "maintainer_closed": False,
            "awaiting_maintainer": row_state_open(payload)
            and awaits_maintainer(payload, known),
            "merge_commit": (
                merge_commit.get("oid") if isinstance(merge_commit, dict) else None
            ),
            # The issues GitHub will close when this merges, as OWNER/REPO#N.
            # A sibling repository's merge fixes the issue only if it is one.
            # Mailman #206.
            "closes": _closing_references(payload.get("closingIssuesReferences")),
            "test_only": _touches_only_tests(payload),
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
            row["author_is_maintainer"] = is_maintainer(
                {"association": row["author_association"], "author": row["author"]},
                known,
                associations=frozenset({"OWNER", "MEMBER"}),
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
                    maintainers=known,
                )
                row["closed_by"] = _note_ai_doubt(row["closed_by"], payload, known)
                row["maintainer_closed"] = bool(row["closed_by"]["maintainer"])
        record["resolved"].append(row)
        if row["maintainer_closed"]:
            record["maintainer_closed"].append(stale_attempt_row(row, now=now))
        elif (
            row["closes_issue"]
            and attempt_is_closed_unmerged(row)
            and attempt_is_dormant(row, now=now)
            and attempt_is_project_voice(row)
        ):
            record["maintainer_owned"].append(stale_attempt_row(row, now=now))
        elif is_stale_attempt(row, now=now):
            record["stale"].append(stale_attempt_row(row, now=now))
        elif row["state"] == "OPEN":
            record["open"].append(row)
        elif (
            row["state"] == "MERGED"
            and row["test_only"]
            and not row["closes_issue"]
            and f"{str(repository or '').lower()}#{issue_number}"
            not in [str(ref).lower() for ref in row["closes"]]
        ):
            record["test_only"].append(row)
        elif row["state"] == "MERGED":
            record["merged"].append(row)
    decided = (record["open"] or record["merged"] or [None])[0]
    record["decided_by"] = decided
    record["detail"] = _cited_detail(record)
    if record["unread"]:
        named = ", ".join(
            f"{row.get('repository')}#{row.get('number')} ({row.get('detail')})"
            for row in record["unread"]
        )
        record["detail"] = (
            f"{len(record['unread'])} cited reference(s) could not be read: "
            f"{named}. {record['detail']}"
        )
    path = run_directory / CITED_PULL_REQUESTS_FILENAME
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8", newline="\n")
    return record


#: GitHub's `author_association` values are one upper-case word. Anything else
#: on stdout is not an answer.
_ASSOCIATION_WORD = re.compile(r"[A-Z_]+")


def _first_association(payload: dict[str, Any]) -> str | None:
    """The author association a payload already carries, in either spelling."""
    for key in ("authorAssociation", "author_association"):
        value = str(payload.get(key) or "").strip().upper()
        if _ASSOCIATION_WORD.fullmatch(value):
            return value
    return None


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
    return association if _ASSOCIATION_WORD.fullmatch(association) else None


def _api(
    run_directory: Path,
    *,
    executable: str,
    path: str,
    timeout_seconds: float,
    commands: list[dict[str, Any]],
    query: dict[str, int | str] | None = None,
) -> Any | None:
    """One `gh api` read, recorded in the same command list as everything else."""
    command = [executable, "api", path]
    if query:
        # Fields, not `?a=1&b=2`: `&` is a command separator to a Windows
        # shell, including the one behind a `gh.cmd` shim.
        command += ["-X", "GET"]
        for key, value in query.items():
            command += ["-f", f"{key}={value}"]
    result: CommandResult = execute(
        command,
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
    maintainers: Collection[str] = (),
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
    # The last closure can sit past the first hundred events; reading one
    # page took an early self-close for the one that stands.
    events: list[Any] = []
    complete = False
    for page in range(1, _TIMELINE_PAGES + 1):
        got = _api(
            run_directory,
            executable=executable,
            path=f"repos/{slug}/issues/{number}/timeline",
            timeout_seconds=timeout_seconds,
            commands=commands,
            query={"per_page": 100, "page": page},
        )
        if not isinstance(got, list):
            break
        events += got
        if len(got) < 100:
            complete = True
            break
    for event in events:
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
    if not complete:
        # A page went unread or the cap was reached: a closure seen so far may
        # not be the last one. `closed_by` names the last closer; the events
        # read still supply associations.
        login = association = None
        found["source"] = None
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
    if is_maintainer({"association": association, "login": login}, maintainers):
        found["maintainer"] = True
        found["detail"] = (
            f"{login} ({(association or 'maintainer').lower()}) closed it, "
            "and did not write it"
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
    owned = record.get("maintainer_owned") or []
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
        if owned:
            named = ", ".join(
                f"{row.get('repository')}#{row.get('number')} by {row.get('author')}"
                for row in owned
            )
            base += (
                f". {len(owned)} closed fix(es) a maintainer wrote for this "
                f"issue: {named}. The project's own fix is parked, not abandoned"
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
