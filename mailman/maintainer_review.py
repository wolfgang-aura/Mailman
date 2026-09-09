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
from typing import Any

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


def parse_pull_request(url: str) -> tuple[str, int]:
    match = _PULL_REQUEST_URL.match(url.strip())
    if not match:
        raise ValueError("expected https://github.com/OWNER/REPO/pull/NUMBER")
    return match["repository"], int(match["number"])


def _trim(text: str | None) -> str:
    body = (text or "").strip()
    return body if len(body) <= _BODY_LIMIT else body[:_BODY_LIMIT] + "\n[truncated]"


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
) -> dict[str, Any]:
    """Read the review bodies and inline comments on a filed pull request."""
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
    reviews = execute(
        [command, "pr", "view", str(number), "--repo", repository,
         "--json", "reviews,state,title,url"],
        working_directory=run_directory, timeout_seconds=timeout_seconds,
    )
    record["commands"].append(reviews.to_dict())
    payload = _read_json(reviews)
    if reviews.timed_out or reviews.exit_code != 0 or not isinstance(payload, dict):
        record["detail"] = "the pull request's reviews could not be read"
        return _write(run_directory, record)
    record["title"] = payload.get("title")
    record["pull_request_state"] = payload.get("state")
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
    inline = execute(
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
    record["requested_changes"] = [
        {"id": item["id"], "author": item["author"], "kind": "review",
         "state": item["state"]}
        for item in record["reviews"]
        if item["state"] in CHANGE_STATES and item["body"]
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
    for item in record.get("reviews", []):
        if not item.get("body"):
            continue
        lines += [
            f"## {item['id']} by {item['author']} ({item['state']})", "",
            item["body"], "",
        ]
    for item in record.get("comments", []):
        where = f"{item.get('path')}:{item.get('line')}" if item.get("path") else "general"
        lines += [
            f"## {item['id']} by {item['author']} on {where}", "",
            item.get("body", ""), "",
        ]
    if not record.get("inline_comments_read", True):
        lines += [
            "Inline comments could not be read. Open the pull request and "
            "check for line comments before treating this as complete.", "",
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


def check_revision(run_directory: Path) -> dict[str, Any]:
    """Refuse a revision that leaves a requested change unanswered.

    A maintainer names a constraint once. A revision that quietly skips one of
    them costs a second review round, which is the expensive kind.
    """
    review = load_review(run_directory)
    if not review or not review.get("success"):
        return {"ok": False, "reason": "no-review",
                "detail": "no readable upstream review: run `mailman fetch-review RUN_ID --pr URL`"}
    wanted = {item["id"] for item in review.get("requested_changes", [])}
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
