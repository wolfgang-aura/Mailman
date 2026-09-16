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
from collections.abc import Iterable, Sequence
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
    # A reporter who already wrote the fix. domokane/FinancePy#262 to #268
    # each carried a candidate patch and an offer to turn it into a pull
    # request, and the gate read them as unclaimed. See
    # https://github.com/wolfgang-aura/Mailman/issues/93.
    r"|i(?:'d| would) be (?:glad|happy|pleased) to "
    r"(?:prepare|open|submit|send|raise|help|take|work|fix|make|turn|contribute)"
    r"|(?:attached|linked|local|my) candidate"
    r"|candidate (?:patch|fix|source|diff|change)"
    r"|proposed (?:correction|fix|patch|change|diff)"
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


#: The three ways a pull request is named in an issue thread. A full URL and
#: `owner/repo#N` can point anywhere; a bare `#N` means the repository the
#: issue lives in. deepset-ai/haystack#12777 ends with
#: "**Draft implementation:** [#12775](https://github.com/deepset-ai/haystack/pull/12775)"
#: and the duplicate search never found it, because #12775 does not cite the
#: issue back. See https://github.com/wolfgang-aura/Mailman/issues/98.
_PULL_REQUEST_URL = re.compile(
    r"https?://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/pull/(\d+)",
    re.IGNORECASE,
)
_HASH_REFERENCE = re.compile(
    r"(?:\b([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+))?#(\d+)\b"
)

#: How many references one thread may hand to the resolver. Each one is a `gh`
#: call, and a long thread quoting version numbers and colours would otherwise
#: buy a page of them. First mention first: the reference that decides a screen
#: is normally the one the reporter wrote into the body.
REFERENCE_LIMIT = 10


def pull_request_references(
    texts: Sequence[str | None],
    *,
    repository: str,
    exclude: Iterable[tuple[str, int]] = (),
    limit: int = REFERENCE_LIMIT,
) -> list[dict[str, Any]]:
    """Every pull-request reference written in these texts, first mention first.

    Nothing here decides whether the number is a pull request at all: `#12`
    reads the same whether it names an issue, a pull request or a heading
    anchor, and only `gh pr view` can tell them apart. This finds candidates
    and records how each was written, so a rejection can quote the reference
    that caused it rather than assert one.
    """
    skip = {(slug.lower(), number) for slug, number in exclude}
    found: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for text in texts:
        flat = _flat(text)
        if not flat:
            continue
        for pattern in (_PULL_REQUEST_URL, _HASH_REFERENCE):
            for match in pattern.finditer(flat):
                owner, name, digits = match.groups()
                slug = f"{owner}/{name}" if owner and name else repository
                number = int(digits)
                key = (slug.lower(), number)
                if key in seen or key in skip or number <= 0:
                    continue
                seen.add(key)
                found.append(
                    {
                        "text": match.group(0),
                        "repository": slug,
                        "number": number,
                    }
                )
    return found[:limit]


def _cross_referenced_urls(timeline: Any) -> list[str]:
    """The pull requests GitHub itself linked to this issue.

    python-jsonschema/jsonschema#1497 is the case text alone misses: its one
    comment says "the issue/PR in referencing" without a number, and the open
    cross-repository pull request, `python-jsonschema/referencing#367`, is on
    the timeline instead. One more read of a page already being paged.
    """
    if not isinstance(timeline, list):
        return []
    urls: list[str] = []
    for entry in timeline:
        if not isinstance(entry, dict) or entry.get("event") != "cross-referenced":
            continue
        source = entry.get("source")
        issue = source.get("issue") if isinstance(source, dict) else None
        url = issue.get("html_url") if isinstance(issue, dict) else None
        if isinstance(url, str) and url:
            urls.append(url)
    return urls


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


def triage_warning(run_directory: Path) -> str | None:
    """Why the issue may not be a bug: nobody who speaks for the project said so.

    An outside reporter, no maintainer reply. pytest-dev/pytest#14992 looked
    exactly like this when #14993 was filed against it; the maintainer's
    first reply, eleven hours later, said the use case was unsupported, and
    the reporter withdrew the premise. Older claims records lack the fields
    and get no warning, which is a gap and not a pass.
    See https://github.com/wolfgang-aura/Mailman/issues/88 and /issues/90. skfolio#316 was
    filed on an issue in the same state, four days old and unanswered; the
    maintainer closed it in fifteen minutes: the behaviour was a choice.
    """
    claims = load_claims(run_directory) or {}
    reporter = claims.get("reporter_association")
    replied = claims.get("maintainer_replied")
    if reporter is None or replied is None:
        return None
    if reporter in MAINTAINER_ASSOCIATIONS or replied:
        return None
    return (
        f"the issue was reported from outside the project ({reporter}) and no "
        "owner, member or collaborator has replied on it. Nobody who can "
        "speak for the project has said this is a bug they want fixed. "
        "pytest-dev/pytest#14993 was filed on an issue in this state; the "
        "premise turned out to be wrong and the pull request closed unread."
    )


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

    def api(path: str, **query: int | str) -> Any | None:
        # Query parameters go in as fields rather than in the path. `gh` adds
        # them to the query string of a GET either way, and the recorded
        # command then carries no `&`, which is a command separator to every
        # Windows shell that ever re-runs it.
        command = [command_executable, "api", path]
        if query:
            command.append("-X")
            command.append("GET")
            for key, value in query.items():
                command += ["-f", f"{key}={value}"]
        result = execute(
            command,
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
    record["issue_closed_at"] = payload.get("closed_at")
    # Who reported it, and whether anyone who can speak for the project has
    # answered. pytest-dev/pytest#14992 was thirteen hours old, reported from
    # outside, unanswered, and its premise was wrong; the fix filed against it
    # closed without a word. See wolfgang-aura/Mailman#88.
    record["reporter_association"] = payload.get("author_association")

    comments: list[dict[str, Any]] = []
    for page in range(1, pages + 1):
        got = api(
            f"repos/{slug}/issues/{number}/comments", per_page=100, page=page
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

    # The report itself is the reporter's first comment. A reporter who says
    # "proposed correction" and pastes the diff has claimed the work as surely
    # as one who comments "I'll open a PR" later.
    report = {
        "id": payload.get("id"),
        "user": payload.get("user"),
        "author_association": payload.get("author_association"),
        "body": payload.get("body"),
        "created_at": payload.get("created_at"),
        "html_url": payload.get("html_url"),
    }
    for comment in [report, *comments]:
        kind = classify_comment(comment)
        if kind == "claim":
            record["claims"].append(_row(comment))
        elif kind == "assignment":
            record["assignments"].append(_row(comment))
    record["comments_read"] = len(comments)
    # Every pull request the thread names, unresolved. Deciding what each one
    # is costs a `gh pr view` per reference, so the read stops at collecting
    # them and `prescreen` pays for the ones it wants.
    # https://github.com/wolfgang-aura/Mailman/issues/98
    timeline = api(f"repos/{slug}/issues/{number}/timeline", per_page=100)
    record["references"] = pull_request_references(
        [
            payload.get("body"),
            *(
                comment.get("body")
                for comment in comments
                if isinstance(comment, dict)
            ),
            *_cross_referenced_urls(timeline),
        ],
        repository=slug,
        exclude=[(slug, int(number))],
    )
    record["maintainer_replied"] = any(
        comment.get("author_association") in MAINTAINER_ASSOCIATIONS
        for comment in comments
        if isinstance(comment, dict)
    )
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
