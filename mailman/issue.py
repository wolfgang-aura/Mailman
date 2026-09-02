from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

from mailman.executor import CommandResult, execute
from mailman.redaction import redact
from mailman.toolchain import resolve_tool


_ISSUE_URL_PATTERN = re.compile(
    r"^https://github\.com/(?P<owner>[A-Za-z0-9][A-Za-z0-9._-]*)"
    r"/(?P<repository>[A-Za-z0-9][A-Za-z0-9._-]*)"
    r"/issues/(?P<number>[1-9][0-9]*)$"
)

ISSUE_FIELDS = "number,title,body,state,url,author,labels,createdAt,updatedAt"


@dataclass(frozen=True)
class IssueReference:
    owner: str
    repository: str
    number: int
    url: str

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repository}#{self.number}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "owner": self.owner,
            "repository": self.repository,
            "number": self.number,
            "url": self.url,
        }


def parse_issue_url(url: str) -> IssueReference:
    """Return the owner, repository, and number of a GitHub issue URL."""
    match = _ISSUE_URL_PATTERN.fullmatch(url.strip())
    if match is None:
        raise ValueError(
            "issue must be a GitHub issue URL of the form "
            "https://github.com/<owner>/<repository>/issues/<number>"
        )
    return IssueReference(
        owner=match["owner"],
        repository=match["repository"],
        number=int(match["number"]),
        url=url.strip(),
    )


def _label_names(raw_labels: object) -> list[str]:
    if not isinstance(raw_labels, list):
        return []
    names: list[str] = []
    for label in raw_labels:
        if isinstance(label, dict) and isinstance(label.get("name"), str):
            names.append(label["name"])
        elif isinstance(label, str):
            names.append(label)
    return names


def render_issue(
    reference: IssueReference, payload: dict[str, Any], *, source: str, captured_at: str
) -> str:
    """Render captured issue fields as the private `issue.md` briefing."""
    title = payload.get("title")
    body = payload.get("body")
    author = payload.get("author")
    author_login = author.get("login") if isinstance(author, dict) else None
    labels = _label_names(payload.get("labels"))
    lines = [
        f"# {reference.slug}: {title}" if title else f"# {reference.slug}",
        "",
        f"- Source: {reference.url}",
        f"- Capture method: {source}",
        f"- Captured at: {captured_at}",
    ]
    if payload.get("state"):
        lines.append(f"- State: {payload['state']}")
    if author_login:
        lines.append(f"- Author: {author_login}")
    if labels:
        lines.append(f"- Labels: {', '.join(labels)}")
    if payload.get("createdAt"):
        lines.append(f"- Created at: {payload['createdAt']}")
    lines.extend(["", "## Issue body", ""])
    text = body if isinstance(body, str) and body.strip() else "_The issue has no body._"
    lines.append(redact(text).strip())
    lines.extend(
        [
            "",
            "## Capture boundary",
            "",
            "This file is the only issue text the agents see. Comments, linked",
            "pull requests, and any accepted upstream fix are deliberately absent.",
            "",
        ]
    )
    return "\n".join(lines)


def _write_record(run_directory: Path, record: dict[str, Any]) -> Path:
    destination = run_directory / "issue.json"
    temporary = destination.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    temporary.replace(destination)
    return destination


def capture_issue_from_github(
    run_directory: Path,
    *,
    issue_url: str,
    executable: str | None = None,
    timeout_seconds: float = 60,
) -> dict[str, Any]:
    """Capture one GitHub issue with the `gh` CLI and write `issue.md`."""
    reference = parse_issue_url(issue_url)
    command_executable = executable or resolve_tool(run_directory, "gh")
    result: CommandResult = execute(
        [
            command_executable,
            "issue",
            "view",
            reference.url,
            "--json",
            ISSUE_FIELDS,
        ],
        working_directory=run_directory,
        timeout_seconds=timeout_seconds,
    )
    captured_at = datetime.now(UTC).isoformat()
    record: dict[str, Any] = {
        "schema_version": 1,
        "source": "github-cli",
        "reference": reference.to_dict(),
        "captured_at": captured_at,
        "command": result.to_dict(),
        "success": False,
    }
    if result.timed_out or result.exit_code != 0:
        record["detail"] = "the issue could not be read with the GitHub CLI"
        _write_record(run_directory, record)
        return record
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        record["detail"] = f"the GitHub CLI returned unreadable JSON: {error}"
        _write_record(run_directory, record)
        return record
    if not isinstance(payload, dict):
        record["detail"] = "the GitHub CLI returned an unexpected payload"
        _write_record(run_directory, record)
        return record

    markdown = render_issue(
        reference, payload, source="github-cli", captured_at=captured_at
    )
    (run_directory / "issue.md").write_text(markdown, encoding="utf-8")
    record.update(
        {
            "success": True,
            "title": payload.get("title"),
            "state": payload.get("state"),
            "labels": _label_names(payload.get("labels")),
            "body_characters": len(payload.get("body") or ""),
            "issue_markdown": str((run_directory / "issue.md").resolve()),
        }
    )
    _write_record(run_directory, record)
    return record


def capture_issue_from_file(
    run_directory: Path, *, issue_url: str, source_file: Path, title: str | None = None
) -> dict[str, Any]:
    """Capture issue text a human transcribed, for hosts without the GitHub CLI."""
    reference = parse_issue_url(issue_url)
    path = source_file.resolve(strict=True)
    if not path.is_file():
        raise ValueError("issue source must be a file")
    body = path.read_text(encoding="utf-8")
    captured_at = datetime.now(UTC).isoformat()
    payload = {"title": title, "body": body}
    markdown = render_issue(
        reference, payload, source=f"manual file capture ({path.name})",
        captured_at=captured_at,
    )
    (run_directory / "issue.md").write_text(markdown, encoding="utf-8")
    record = {
        "schema_version": 1,
        "source": "file",
        "reference": reference.to_dict(),
        "captured_at": captured_at,
        "source_file": str(path),
        "source_sha256": sha256(path.read_bytes()).hexdigest(),
        "title": title,
        "body_characters": len(body),
        "issue_markdown": str((run_directory / "issue.md").resolve()),
        "success": True,
    }
    _write_record(run_directory, record)
    return record


def load_issue_record(run_directory: Path) -> dict[str, Any] | None:
    path = run_directory / "issue.json"
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else None
