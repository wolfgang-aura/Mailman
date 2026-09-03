"""Read who has already claimed the target issue, in the issue's own comments.

Every other prior-art gate reads pull requests. That misses the claim that has
not become one yet, which is the earlier and more common form: on
`openai/openai-agents-python`, of twenty unassigned open issues fourteen already
had an open pull request and several of the rest had been claimed in a comment.
`check-target` called one of those unclaimed. See
https://github.com/wolfgang-aura/Mailman/issues/36.

Three states are worth telling apart, because they carry different weight:

- The issue carries a GitHub assignee. Somebody owns it. Nothing to argue with.
- A maintainer answered a claim by handing the work over. Same conclusion,
  reached in prose rather than in the assignee field.
- Somebody offered and nobody answered. That is worth a human reading, not a
  hard stop, so it is the one state a flag can clear.

The comments are read for this judgement only. They are never written into
`issue.md`, which is the same rule that keeps a merged pull request's diff out
of an agent's prompt: Mailman must not hand an agent somebody else's answer.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from mailman.executor import CommandResult, execute
from mailman.issue import load_issue_record
from mailman.target_intel import _is_bot
from mailman.toolchain import resolve_tool

CLAIMS_FILENAME = "claims.json"
CLAIMS_SCHEMA_VERSION = 1

#: Who can hand out the work. `author_association` is GitHub's own answer to
#: that question and the only one available without another API call.
MAINTAINER_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})

_QUOTE_CHARACTER_LIMIT = 400

#: Asking after a bug is not claiming it. These run first, because several of
#: them contain the words a claim is made of: "is anyone working on this" would
#: otherwise read as "working on this".
_NOT_A_CLAIM = re.compile(
    r"\b(?:"
    r"(?:is |are |has |have )?(?:any\s?one|any\s?body|some\s?one|some\s?body)\b"
    r"|any (?:update|progress|news|luck)"
    r"|has this been"
    r"|i(?:'m| am| was)? ?(?:no longer|not) working on"
    r"|pull requests? (?:are )?welcome"
    r"|prs? (?:are )?welcome"
    r")",
    re.IGNORECASE,
)

#: Somebody saying they are taking the work. An offer phrased as a question
#: ("can I work on this?") counts: it is still an announcement of intent, and
#: filing over it is the duplicate this gate exists to stop.
_CLAIM = re.compile(
    r"\b(?:"
    r"i(?:'m|m| am) (?:currently )?(?:working on|taking|fixing|looking into)"
    r"|i(?:'ll|ll| will| can| could| would like to|'d like to|d like to"
    r"| want to| plan to| intend to) "
    r"(?:take|work on|pick|fix|handle|submit|open|raise|send|look into"
    r"|tackle|contribute|have a go|give)"
    r"|i(?:'ve|ve| have) (?:a|an|the) (?:pr|patch|fix|branch|change)"
    r"|(?:please )?assign (?:this |it |the issue |me )?(?:to )?(?:me\b|myself)"
    r"|assign me"
    r"|can i (?:take|work on|pick|try|have|give|attempt)"
    r"|may i (?:take|work on|pick|try|have|attempt)"
    r"|picking (?:this|it) up"
    r"|taking (?:this|it)(?: up| on)?\b"
    r"|let me (?:take|work on|handle|try|have|give)"
    r"|on it\b"
    r"|i(?:'m|m| am) on (?:this|it)\b"
    r"|working on (?:this|it) (?:now|already)"
    r")",
    re.IGNORECASE,
)

#: A maintainer handing the work to whoever asked. "PRs welcome" is not here on
#: purpose: it invites anybody, which is the opposite of a claim.
_ASSIGNMENT = re.compile(
    r"\b(?:"
    r"(?:i(?:'ve|ve| have) )?assigned (?:this |it |the issue )?to\b"
    r"|assigned to (?:you|@)"
    r"|(?:this |it )?(?:is |'s )?all yours\b"
    r"|it(?:'s| is) yours\b"
    r"|go ahead\b"
    r"|feel free to (?:open|submit|send|raise|pick|take|work)"
    r"|you can (?:take|work on|pick|have) (?:this|it)"
    r"|(?:please )?go for it\b"
    r")",
    re.IGNORECASE,
)


#: GitHub's comment box turns a typed apostrophe into U+2019, so the real
#: comment on openai/openai-agents-python #4775 reads "I’d like to work on
#: this issue" and matched nothing. Normalise before matching, never before
#: quoting: the operator should see what was actually written.
_APOSTROPHES = str.maketrans({"‘": "'", "’": "'", "ʼ": "'", "＇": "'"})


def _flat(text: str | None) -> str:
    return " ".join((text or "").split())


def _matchable(text: str) -> str:
    return text.translate(_APOSTROPHES)


def classify_comment(comment: dict[str, Any]) -> str | None:
    """Say whether one comment claims the work, hands it over, or neither.

    Returns `"claim"`, `"assignment"`, or `None`. A bot never claims anything,
    and only a maintainer can hand work over: the same sentence from an
    outsider is an opinion.
    """
    if not isinstance(comment, dict) or _is_bot(comment.get("user")):
        return None
    body = _matchable(_flat(comment.get("body")))
    if not body:
        return None
    maintainer = comment.get("author_association") in MAINTAINER_ASSOCIATIONS
    if maintainer and _ASSIGNMENT.search(body):
        return "assignment"
    if _NOT_A_CLAIM.search(body):
        return None
    return "claim" if _CLAIM.search(body) else None


def _row(comment: dict[str, Any]) -> dict[str, Any]:
    user = comment.get("user") or {}
    return {
        "author": user.get("login") if isinstance(user, dict) else None,
        "association": comment.get("author_association"),
        "created_at": comment.get("created_at"),
        "quote": _flat(comment.get("body"))[:_QUOTE_CHARACTER_LIMIT],
    }


def _write(run_directory: Path, record: dict[str, Any]) -> Path:
    destination = run_directory / CLAIMS_FILENAME
    temporary = destination.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    temporary.replace(destination)
    return destination


def load_claims(run_directory: Path) -> dict[str, Any] | None:
    path = run_directory / CLAIMS_FILENAME
    if not path.is_file():
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except json.JSONDecodeError:
        return None
    return loaded if isinstance(loaded, dict) else None


def read_claims(
    run_directory: Path,
    *,
    executable: str | None = None,
    timeout_seconds: float = 60,
    execute: Callable[..., CommandResult] = execute,
    pages: int = 4,
) -> dict[str, Any]:
    """Record who has claimed the run's target issue, from its own thread."""
    record: dict[str, Any] = {
        "schema_version": CLAIMS_SCHEMA_VERSION,
        "collected_at": datetime.now(UTC).isoformat(),
        "success": False,
        "claims": [],
        "assignments": [],
        "assignees": [],
        "comments_read": 0,
        "commands": [],
    }
    issue = load_issue_record(run_directory) or {}
    if issue.get("self_reported") is True:
        # A defect the operator wrote has no upstream thread, so there is
        # nothing to claim and nobody to have claimed it. That is a different
        # fact from "the thread was read and held no claim", and `check-target`
        # is entitled to see which one it got.
        record.update(
            {
                "success": True,
                "self_reported": True,
                "detail": (
                    "no upstream issue: this run started from a defect report, "
                    "so no thread exists to claim it in. The duplicate search "
                    "is the only prior-art evidence here."
                ),
            }
        )
        _write(run_directory, record)
        return record
    reference = issue.get("reference") or {}
    owner = reference.get("owner")
    name = reference.get("repository")
    number = reference.get("number")
    if not (owner and name and number):
        record["detail"] = (
            "no captured issue to read: run `mailman fetch-issue` first"
        )
        _write(run_directory, record)
        return record
    slug = f"{owner}/{name}"
    record["repository"] = slug
    record["issue_number"] = number

    command_executable = executable or resolve_tool(run_directory, "gh")

    def api(path: str) -> Any | None:
        result = execute(
            [command_executable, "api", path],
            working_directory=run_directory,
            timeout_seconds=timeout_seconds,
        )
        record["commands"].append(result.to_dict())
        if result.timed_out or result.exit_code != 0:
            return None
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            return None

    payload = api(f"repos/{slug}/issues/{number}")
    if not isinstance(payload, dict):
        # An unreadable thread is not an empty one. Recording it as clean is
        # exactly the false clearance this gate exists to prevent.
        record["detail"] = f"{slug}#{number} could not be read"
        _write(run_directory, record)
        return record
    record["assignees"] = [
        entry.get("login")
        for entry in payload.get("assignees") or []
        if isinstance(entry, dict) and entry.get("login")
    ]
    record["issue_state"] = payload.get("state")

    comments: list[dict[str, Any]] = []
    for page in range(1, pages + 1):
        got = api(
            f"repos/{slug}/issues/{number}/comments?per_page=100&page={page}"
        )
        if not isinstance(got, list):
            if page == 1:
                record["detail"] = (
                    f"the comments on {slug}#{number} could not be read"
                )
                _write(run_directory, record)
                return record
            break
        comments += got
        if len(got) < 100:
            break

    for comment in comments:
        kind = classify_comment(comment)
        if kind == "claim":
            record["claims"].append(_row(comment))
        elif kind == "assignment":
            record["assignments"].append(_row(comment))
    record["comments_read"] = len(comments)
    record["success"] = True
    _write(run_directory, record)
    return record


def render_claims(record: dict[str, Any]) -> str:
    """Render the thread's verdict, in the words that decide a run."""
    if record.get("self_reported") is True:
        return (
            "# Claims\n\n"
            "This run started from a defect report, not an upstream issue.\n"
            "There is no thread, so there is nothing to claim and nobody to\n"
            "have claimed it. Prior art here rests entirely on the duplicate\n"
            "search.\n"
        )
    slug = record.get("repository")
    number = record.get("issue_number")
    lines = [
        f"# Claims on {slug}#{number}",
        "",
        f"- Comments read: {record.get('comments_read', 0)}",
        f"- Assignees: {', '.join(record.get('assignees') or []) or 'none'}",
    ]
    if not record.get("success"):
        lines += ["", f"Unread: {record.get('detail', 'unknown failure')}"]
        return "\n".join(lines) + "\n"
    for heading, key in (
        ("## Claims", "claims"),
        ("## Maintainer replies handing the work over", "assignments"),
    ):
        rows = record.get(key) or []
        if not rows:
            continue
        lines += ["", heading, ""]
        for row in rows:
            lines.append(
                f"- **{row.get('author')}** ({row.get('association')}, "
                f"{row.get('created_at')}): {row.get('quote')}"
            )
    if not (record.get("claims") or record.get("assignments")):
        lines += ["", "No claim was made in this issue's comments."]
    return "\n".join(lines) + "\n"
