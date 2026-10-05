"""A maintainer asked for changes on a filed pull request.

`openai/openai-agents-python#4890` was filed from run
`20260906T104815Z-29582c`. seratch requested changes on 2026-09-07, and the run
record still read `READY_FOR_HUMAN_REVIEW` with no decision, because the
harness had no stage after filing. Every revision since has been hand work:
no reviewer pass, and no record that the run moved.

This reads the upstream review into the run the way `fetch-issue` reads an
issue, so the agents work against what the maintainer actually wrote, and
refuses to call the revision done until each requested change has an answer.
https://github.com/wolfgang-aura/Mailman/issues/60
"""
from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from collections.abc import Callable
from typing import Any

from mailman import identity
from mailman.executor import CommandResult, execute
from mailman.toolchain import resolve_tool

REVIEW_FILENAME = "maintainer-review.json"
REVIEW_MARKDOWN = "maintainer-review.md"
RESPONSE_FILENAME = "revision-response.json"
REVIEW_SCHEMA_VERSION = 1
#: A review state that asks for work. `COMMENTED` is included because
#: maintainers routinely request changes without setting the formal state.
CHANGE_STATES = ("CHANGES_REQUESTED", "COMMENTED")
ANSWERS = ("answered", "declined")
_PULL_REQUEST_URL = re.compile(
    r"^https://github\.com/(?P<repository>[\w.-]+/[\w.-]+)/pull/(?P<number>\d+)/?$"
)
_BODY_LIMIT = 4000
#: A heading that introduces the list of things a maintainer wants changed.
_CHANGE_HEADING = re.compile(
    r"(what i(?:'d| would) change|changes? requested|requested changes|"
    r"required changes|changes needed|blockers?|must[- ]fix|action items)",
    re.IGNORECASE,
)
_HEADING_LINE = re.compile(r"^\s*(?:#{1,6}\s+(?P<hash>.+?)\s*#*|\*\*(?P<bold>[^*]+)\*\*:?|(?P<colon>[^\s].{0,80}):)\s*$")
_LIST_ITEM = re.compile(r"^(?P<indent>\s{0,3})(?:\d+[.)]|[-*+])\s+(?P<text>\S.*)$")


def parse_pull_request(url: str) -> tuple[str, int]:
    match = _PULL_REQUEST_URL.match(url.strip())
    if not match:
        raise ValueError("expected https://github.com/OWNER/REPO/pull/NUMBER")
    return match["repository"], int(match["number"])


def _trim(text: str | None) -> str:
    body = (text or "").strip()
    return body if len(body) <= _BODY_LIMIT else body[:_BODY_LIMIT] + "\n[truncated]"


def _read_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _is_bot(user: dict[str, Any] | None) -> bool:
    if not isinstance(user, dict):
        return True
    login = (user.get("login") or "").lower()
    return (user.get("type") or "") == "Bot" or login.endswith(("[bot]", "-bot"))


def split_points(body: str) -> list[str]:
    """The numbered or bulleted items under a change heading, one per point.

    A review whose body lists four requests is four things to answer, not
    one: edgartools#1329 came back as `requested_changes: 1` and a single
    `answered` covered all four. An empty list means keep the whole body.
    https://github.com/wolfgang-aura/Mailman/issues/126
    """
    points: list[str] = []
    inside = False
    for line in (body or "").splitlines():
        heading = _HEADING_LINE.match(line)
        item = _LIST_ITEM.match(line)
        if heading and not item:
            title = heading["hash"] or heading["bold"] or heading["colon"] or ""
            inside = bool(_CHANGE_HEADING.search(title))
            continue
        if not inside:
            continue
        if item:
            points.append(item["text"].strip())
        elif line.strip() and points and line.startswith((" ", "\t")):
            points[-1] += " " + line.strip()
    return points


def _read_json(result: CommandResult) -> Any:
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def fetch_review(
    run_directory: Path,
    *,
    pull_request: str,
    executable: str | None = None,
    timeout_seconds: float = 60,
    acknowledge_foreign_commits: bool = False,
    _execute: Callable[..., CommandResult] = execute,
) -> dict[str, Any]:
    """Read the reviews, inline comments and conversation on a filed pull request."""
    repository, number = parse_pull_request(pull_request)
    command = executable or resolve_tool(run_directory, "gh")
    record: dict[str, Any] = {
        "schema_version": REVIEW_SCHEMA_VERSION,
        "fetched_at": datetime.now(UTC).isoformat(),
        "pull_request": pull_request.strip(),
        "repository": repository,
        "number": number,
        "success": False,
        "reviews": [],
        "comments": [],
        "requested_changes": [],
        "commands": [],
    }
    reviews = _execute(
        [command, "pr", "view", str(number), "--repo", repository,
         "--json", "author,commits,headRefOid,headRepositoryOwner,reviews,state,title,url"],
        working_directory=run_directory, timeout_seconds=timeout_seconds,
    )
    record["commands"].append(reviews.to_dict())
    payload = _read_json(reviews)
    if reviews.timed_out or reviews.exit_code != 0 or not isinstance(payload, dict):
        record["detail"] = "the pull request's reviews could not be read"
        return _write(run_directory, record)
    record["title"] = payload.get("title")
    record["pull_request_state"] = payload.get("state")
    record["head_sha"] = payload.get("headRefOid")
    record["foreign_commits"], record["foreign_approvals"] = foreign_changes(payload)
    record["foreign_commits_acknowledged"] = bool(acknowledge_foreign_commits)
    for row in payload.get("reviews") or []:
        if not isinstance(row, dict):
            continue
        record["reviews"].append({
            "id": f"review:{row.get('id')}",
            "author": (row.get("author") or {}).get("login"),
            "state": row.get("state"),
            "submitted_at": row.get("submittedAt"),
            "body": _trim(row.get("body")),
        })
    inline = _execute(
        [command, "api", f"repos/{repository}/pulls/{number}/comments",
         "--paginate"],
        working_directory=run_directory, timeout_seconds=timeout_seconds,
    )
    record["commands"].append(inline.to_dict())
    rows = _read_json(inline)
    if inline.timed_out or inline.exit_code != 0 or not isinstance(rows, list):
        # The review bodies are the part that carries the request. Losing the
        # inline comments is worth reporting, not worth refusing over.
        record["inline_comments_read"] = False
    else:
        record["inline_comments_read"] = True
        for row in rows:
            if not isinstance(row, dict):
                continue
            record["comments"].append({
                "id": f"comment:{row.get('id')}",
                "author": (row.get("user") or {}).get("login"),
                "path": row.get("path"),
                "line": row.get("line") or row.get("original_line"),
                "created_at": row.get("created_at"),
                "body": _trim(row.get("body")),
            })
    _read_conversation(record, payload, command, run_directory,
                       timeout_seconds, _execute)
    record["requested_changes"] = [
        change
        for item in record["reviews"]
        if item["state"] in CHANGE_STATES and item["body"]
        for change in _review_changes(item)
    ] + [
        {"id": item["id"], "author": item["author"], "kind": "comment",
         "path": item["path"], "line": item["line"]}
        for item in record["comments"]
    ]
    record["success"] = True
    record["change_count"] = len(record["requested_changes"])
    _write(run_directory, record)
    (run_directory / REVIEW_MARKDOWN).write_text(
        render(record), encoding="utf-8", newline="\n"
    )
    return record


def foreign_changes(
    payload: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Commits on our head branch that we did not author, and approvals of them.

    On openai-agents-python#4890 a maintainer pushed two commits to our branch
    and another approved them; a day later a revision nearly force-pushed over
    both. A commit counts as foreign when it names at least one GitHub login
    and none is the fork owner's; a commit with no linked login is not judged.
    https://github.com/wolfgang-aura/Mailman/issues/140
    """
    owners = {
        login for login in (
            (payload.get("author") or {}).get("login"),
            (payload.get("headRepositoryOwner") or {}).get("login"),
        ) if login
    }
    commits: list[dict[str, Any]] = []
    for commit in payload.get("commits") or []:
        if not isinstance(commit, dict):
            continue
        logins = [
            (who or {}).get("login") for who in commit.get("authors") or []
            if (who or {}).get("login")
        ]
        if logins and not owners.intersection(logins):
            commits.append({
                "sha": commit.get("oid"),
                "authors": logins,
                "committed_at": commit.get("committedDate"),
                "headline": commit.get("messageHeadline"),
            })
    shas = {commit["sha"] for commit in commits}
    head = payload.get("headRefOid")
    approvals = [
        {"id": f"review:{row.get('id')}",
         "author": (row.get("author") or {}).get("login"),
         "sha": (row.get("commit") or {}).get("oid"),
         "submitted_at": row.get("submittedAt")}
        for row in payload.get("reviews") or []
        if isinstance(row, dict) and row.get("state") == "APPROVED"
        and ((row.get("commit") or {}).get("oid") in shas
             or (head in shas and not (row.get("commit") or {}).get("oid")))
    ]
    return commits, approvals


def foreign_detail(record: dict[str, Any]) -> str | None:
    """One sentence naming what someone else pushed, or None when nothing."""
    commits = record.get("foreign_commits") or []
    approvals = record.get("foreign_approvals") or []
    if not commits and not approvals:
        return None
    parts = [
        f"{str(c.get('sha'))[:7]} by {', '.join(c.get('authors') or [])}"
        for c in commits
    ]
    text = (f"{len(commits)} commit(s) on the head branch were not pushed by us: "
            + "; ".join(parts)) if commits else ""
    if approvals:
        text += ("; " if text else "") + "; ".join(
            f"{a.get('author')} approved {str(a.get('sha'))[:7]}, a head we did not push"
            for a in approvals
        )
    return text + (". Fetch the branch, build on its head, and never force-push "
                   "over it; rerun fetch-review with --acknowledge-foreign-commits "
                   "once this has been read")


def _review_changes(item: dict[str, Any]) -> list[dict[str, Any]]:
    base = {"author": item["author"], "kind": "review", "state": item["state"]}
    points = split_points(item["body"])
    if not points:
        return [{"id": item["id"], **base}]
    return [
        {"id": f"{item['id']}:{index}", **base, "point": index, "text": _trim(text)}
        for index, text in enumerate(points, start=1)
    ]


def _read_conversation(
    record: dict[str, Any],
    payload: dict[str, Any],
    command: str,
    run_directory: Path,
    timeout_seconds: float,
    run: Callable[..., CommandResult],
) -> None:
    """Add the conversation comments a maintainer wrote after our last commit.

    Maintainers often decide in the pull request's conversation rather than in
    a review: nicegui#6345 and pymc#8442 both did, and the review record came
    back empty. Only comments by someone other than the author, newer than
    the author's last commit, ask for the revision this run is making.
    https://github.com/wolfgang-aura/Mailman/issues/128
    """
    author = (payload.get("author") or {}).get("login") or ""
    last_own: datetime | None = None
    for commit in payload.get("commits") or []:
        if not isinstance(commit, dict):
            continue
        logins = {(who or {}).get("login") for who in commit.get("authors") or []}
        at = _read_timestamp(commit.get("committedDate"))
        if author in logins and at and (last_own is None or at > last_own):
            last_own = at
    result = run(
        [command, "api", f"repos/{record['repository']}/issues/{record['number']}/comments",
         "--paginate"],
        working_directory=run_directory, timeout_seconds=timeout_seconds,
    )
    record["commands"].append(result.to_dict())
    rows = _read_json(result)
    if result.timed_out or result.exit_code != 0 or not isinstance(rows, list):
        record["conversation_comments_read"] = False
        return
    record["conversation_comments_read"] = True
    for row in rows:
        if not isinstance(row, dict):
            continue
        user = row.get("user") or {}
        if user.get("login") == author or _is_bot(user):
            continue
        at = _read_timestamp(row.get("created_at"))
        if last_own is not None and (at is None or at <= last_own):
            continue
        record["comments"].append({
            "id": f"comment:{row.get('id')}",
            "author": user.get("login"),
            "path": None,
            "line": None,
            "created_at": row.get("created_at"),
            "body": _trim(row.get("body")),
            "source": "conversation",
        })


def _write(run_directory: Path, record: dict[str, Any]) -> dict[str, Any]:
    (run_directory / REVIEW_FILENAME).write_text(
        json.dumps(record, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    return record


def load_review(run_directory: Path) -> dict[str, Any] | None:
    path = run_directory / REVIEW_FILENAME
    if not path.is_file():
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except json.JSONDecodeError:
        return None
    return loaded if isinstance(loaded, dict) else None


def load_review_markdown(run_directory: Path) -> str | None:
    path = run_directory / REVIEW_MARKDOWN
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8").strip()
    return text or None


def render(record: dict[str, Any]) -> str:
    """The maintainer's own words, for the prompt and for a human."""
    lines = [
        f"# Upstream review on {record['repository']}#{record['number']}",
        "",
        "A maintainer read the filed pull request and asked for changes. This "
        "is what they wrote. Answer it directly. An approach they ruled out "
        "stays ruled out, and a constraint they named is not negotiable.",
        "",
    ]
    foreign = foreign_detail(record)
    if foreign:
        lines += ["## Someone else pushed to this branch", ""]
        lines += [
            f"- `{str(c.get('sha'))[:7]}` by {', '.join(c.get('authors') or [])} "
            f"at {c.get('committed_at')}: {c.get('headline') or ''}".rstrip(": ")
            for c in record.get("foreign_commits") or []
        ]
        lines += [
            f"- {a.get('author')} approved `{str(a.get('sha'))[:7]}` at "
            f"{a.get('submitted_at')}, a head we did not push"
            for a in record.get("foreign_approvals") or []
        ]
        lines += [
            "", "Start the revision from the pull request's current head. A "
            "force-push would discard these commits and the approval on them.", "",
        ]
    points: dict[str, list[dict[str, Any]]] = {}
    for change in record.get("requested_changes", []):
        if change.get("point"):
            points.setdefault(change["id"].rsplit(":", 1)[0], []).append(change)
    for item in record.get("reviews", []):
        if not item.get("body"):
            continue
        lines += [
            f"## {item['id']} by {item['author']} ({item['state']})", "",
            item["body"], "",
        ]
        if item["id"] in points:
            lines += ["Answer each point separately:", ""]
            lines += [f"- `{change['id']}`: {change['text']}" for change in points[item["id"]]]
            lines.append("")
    for item in record.get("comments", []):
        if item.get("path"):
            where = f"{item.get('path')}:{item.get('line')}"
        elif item.get("source") == "conversation":
            where = "the conversation"
        else:
            where = "general"
        lines += [
            f"## {item['id']} by {item['author']} on {where}", "",
            item.get("body", ""), "",
        ]
    if not record.get("inline_comments_read", True):
        lines += [
            "Inline comments could not be read. Open the pull request and "
            "check for line comments before treating this as complete.", "",
        ]
    if record.get("conversation_comments_read") is False:
        lines += [
            "The pull request's conversation comments could not be read. Open "
            "the pull request and read the conversation before treating this "
            "as complete.", "",
        ]
    return "\n".join(lines).rstrip() + "\n"


def load_response(run_directory: Path) -> dict[str, Any] | None:
    path = run_directory / RESPONSE_FILENAME
    if not path.is_file():
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except json.JSONDecodeError:
        return None
    return loaded if isinstance(loaded, dict) else None


def init_response(run_directory: Path) -> dict[str, Any]:
    """Write an empty response with one slot per requested change."""
    review = load_review(run_directory)
    if not review or not review.get("success"):
        raise ValueError("run `mailman fetch-review` first")
    record = {
        "schema_version": REVIEW_SCHEMA_VERSION,
        "pull_request": review["pull_request"],
        "answers": [
            {"id": item["id"], "answer": "", "note": "",
             "_answer_must_be_one_of": list(ANSWERS)}
            for item in review["requested_changes"]
        ],
    }
    (run_directory / RESPONSE_FILENAME).write_text(
        json.dumps(record, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    return record


def _revision_identity_violations(run_directory: Path, review: dict[str, Any]) -> list[str]:
    """Follow-up commits that would publish an address, newest first.

    Only commits after the pull request's head count: anything at or before it
    is already upstream, maintainer commits included.
    See https://github.com/wolfgang-aura/Mailman/issues/414
    """
    head = str(review.get("head_sha") or "")
    exported = run_directory / "export" / "export.json"
    workspace = run_directory / "workspace"
    if exported.is_file():
        try:
            workspace = Path(json.loads(exported.read_text(encoding="utf-8")).get("workspace")
                             or workspace)
        except (json.JSONDecodeError, AttributeError):
            pass
    if not head or not (workspace / ".git").exists():
        return []
    commits = identity.branch_commits(workspace, head)
    found = identity.author_violations(commits, identity.resolve_identity(run_directory.parent))
    return [item["sha"] for item in found]


def check_revision(run_directory: Path) -> dict[str, Any]:
    """Refuse a revision that leaves a requested change unanswered.

    A maintainer names a constraint once. A revision that quietly skips one of
    them costs a second review round, which is the expensive kind.
    """
    review = load_review(run_directory)
    if not review or not review.get("success"):
        return {"ok": False, "reason": "no-review",
                "detail": "no readable upstream review: run `mailman fetch-review RUN_ID --pr URL`"}
    foreign = foreign_detail(review)
    if foreign and not review.get("foreign_commits_acknowledged"):
        return {"ok": False, "reason": "foreign-commits", "detail": foreign}
    misattributed = _revision_identity_violations(run_directory, review)
    if misattributed:
        # The diagnostic names commits, never the private addresses.
        return {"ok": False, "reason": "author-identity", "commits": misattributed,
                "detail": "correct author/committer identity on " + ", ".join(misattributed)}
    wanted ={item["id"] for item in review.get("requested_changes", [])}
    if not wanted:
        return {"ok": True, "reason": "nothing-requested", "answered": [],
                "detail": f"{review['repository']}#{review['number']} has no requested change to answer"}
    response = load_response(run_directory)
    if not response:
        return {"ok": False, "reason": "no-response",
                "detail": f"run `mailman revision-response {run_directory.name} --init` and answer all {len(wanted)} requested changes"}
    answers = {}
    problems = []
    for row in response.get("answers", []):
        if not isinstance(row, dict) or not row.get("id"):
            continue
        answer = str(row.get("answer") or "").strip().lower()
        if answer not in ANSWERS:
            continue
        if answer == "declined" and not str(row.get("note") or "").strip():
            problems.append(f"{row['id']} is declined with no note saying why")
            continue
        answers[row["id"]] = answer
    missing = sorted(wanted - set(answers))
    if missing or problems:
        return {
            "ok": False, "reason": "unanswered",
            "missing": missing, "problems": problems,
            "detail": "; ".join([
                *([f"unanswered: {', '.join(missing)}"] if missing else []),
                *problems,
            ]),
        }
    return {"ok": True, "reason": "answered",
            "answered": sorted(answers), "declined": sorted(
                key for key, value in answers.items() if value == "declined"),
            "detail": f"all {len(wanted)} requested changes have an answer"}
