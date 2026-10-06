from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from collections.abc import Collection
from pathlib import Path
from typing import Any

from mailman.executor import CommandResult, execute
from mailman.finding import (
    is_finding_file,
    load_finding,
    render_finding_markdown,
    unmet_conditions,
)
from mailman.redaction import redact
from mailman.toolchain import resolve_tool


_ISSUE_URL_PATTERN = re.compile(
    r"^https://github\.com/(?P<owner>[A-Za-z0-9][A-Za-z0-9._-]*)"
    r"/(?P<repository>[A-Za-z0-9][A-Za-z0-9._-]*)"
    r"/issues/(?P<number>[1-9][0-9]*)$"
)

ISSUE_FIELDS = "number,title,body,state,url,author,labels,createdAt,updatedAt,comments"
#: Only these authors' comments reach the agents. An invited enhancement's
#: design lives in a maintainer comment (pandera#742), while other comments
#: carry claims, guesses and links to competing fixes.
MAINTAINER_ASSOCIATIONS = ("OWNER", "MEMBER", "COLLABORATOR")
_COMMENT_LIMIT = 3000
_COMMENT_COUNT = 8


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


def _label_descriptions(raw_labels: object) -> dict[str, str]:
    """Each label's project-written description, where it has one.

    Projects name a state such as "waiting on the team" in their own words,
    so the description is the portable signal. Mailman #280.
    """
    if not isinstance(raw_labels, list):
        return {}
    return {
        label["name"]: label["description"]
        for label in raw_labels
        if isinstance(label, dict)
        and isinstance(label.get("name"), str)
        and isinstance(label.get("description"), str)
        and label["description"].strip()
    }


def render_issue(
    reference: IssueReference,
    payload: dict[str, Any],
    *,
    source: str,
    captured_at: str,
    maintainers: Collection[str] = (),
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
        _maintainer_comment_lines(payload.get("comments"), maintainers=maintainers)
    )
    lines.extend(
        [
            "",
            "## Capture boundary",
            "",
            "This file is the only issue text the agents see. Comments from",
            "anyone but a maintainer, linked pull requests, and any accepted",
            "upstream fix are deliberately absent.",
            "",
        ]
    )
    return "\n".join(lines)


def _maintainer_comment_lines(
    raw_comments: object, *, maintainers: Collection[str] = ()
) -> list[str]:
    """The latest maintainer comments, oldest first, redacted and trimmed.

    `maintainers` is the repository screen's recorded login set, for the
    maintainers GitHub reports as CONTRIBUTOR. Compared here rather than
    through `mailman.maintainers`, which imports this module. Mailman #203.
    """
    listed = {str(name).lower() for name in maintainers}
    kept: list[tuple[str, str, str, str]] = []
    for comment in raw_comments if isinstance(raw_comments, list) else []:
        if not isinstance(comment, dict):
            continue
        author = comment.get("author")
        login = author.get("login") if isinstance(author, dict) else None
        association = comment.get("authorAssociation")
        body = comment.get("body")
        if (
            not isinstance(login, str)
            or login.lower().endswith(("[bot]", "-bot", "-robot"))
            or (
                association not in MAINTAINER_ASSOCIATIONS
                and login.lower() not in listed
            )
            or not isinstance(body, str)
            or not body.strip()
        ):
            continue
        created = str(comment.get("createdAt") or "")[:10]
        kept.append((login, association, created, body.strip()))
    if not kept:
        return []
    lines = ["", "## Maintainer comments", ""]
    for login, association, created, body in kept[-_COMMENT_COUNT:]:
        when = f", {created}" if created else ""
        if len(body) > _COMMENT_LIMIT:
            body = body[:_COMMENT_LIMIT] + "\n[truncated]"
        lines.extend([f"### {login} ({association}{when})", "", redact(body), ""])
    return lines


def maintainer_command_lines(
    raw_comments: object, *, maintainers: Collection[str] = ()
) -> list[str]:
    """Slash-command lines maintainers wrote, each on its own line.

    A triage workflow such as peft's keeps an outside pull request only when
    its issue carries an approval command from a maintainer. Mailman #417.
    """
    listed = {str(name).lower() for name in maintainers}
    found: list[str] = []
    for comment in raw_comments if isinstance(raw_comments, list) else []:
        if not isinstance(comment, dict):
            continue
        author = comment.get("author")
        login = author.get("login") if isinstance(author, dict) else None
        body = comment.get("body")
        if (
            not isinstance(login, str)
            or not isinstance(body, str)
            or (
                comment.get("authorAssociation") not in MAINTAINER_ASSOCIATIONS
                and login.lower() not in listed
            )
        ):
            continue
        found += [
            line.strip() for line in body.splitlines() if line.strip().startswith("/")
        ]
    return found


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
    maintainers: Collection[str] = (),
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
        reference,
        payload,
        source="github-cli",
        captured_at=captured_at,
        maintainers=maintainers,
    )
    (run_directory / "issue.md").write_text(markdown, encoding="utf-8")
    record.update(
        {
            "success": True,
            "title": payload.get("title"),
            "state": payload.get("state"),
            "labels": _label_names(payload.get("labels")),
            "label_descriptions": _label_descriptions(payload.get("labels")),
            "created_at": payload.get("createdAt"),
            "body_characters": len(payload.get("body") or ""),
            "maintainer_commands": maintainer_command_lines(
                payload.get("comments"), maintainers=maintainers
            ),
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



def render_defect_report(
    *, title: str | None, body: str, source_file: Path, captured_at: str
) -> str:
    """Render a defect the operator found into the same briefing shape.

    A self-reported defect reaches the agents through exactly the file an
    upstream issue would, so nothing downstream has to know which it was.
    """
    heading = f"# Self-reported defect: {title}" if title else "# Self-reported defect"
    lines = [
        heading,
        "",
        "- Source: no upstream issue; this defect was found and written here",
        f"- Capture method: defect report ({source_file.name})",
        f"- Captured at: {captured_at}",
        "",
        "## Defect report",
        "",
        redact(body).strip(),
        "",
        "## Capture boundary",
        "",
        "There is no upstream issue thread, so there are no comments and no",
        "linked pull requests to withhold. The duplicate search is the only",
        "prior-art evidence for this run, and the reproduction is the only",
        "evidence the defect is real.",
        "",
    ]
    return "\n".join(lines)


def capture_defect_report(
    run_directory: Path, *, source_file: Path, title: str | None = None
) -> dict[str, Any]:
    """Capture a defect the operator wrote, for a target with no tracker.

    See https://github.com/wolfgang-aura/Mailman/issues/45: the repositories
    that merge outside work fastest are often the ones whose contributors never
    file issues, so their trackers hold questions and their defects do not
    exist upstream until somebody fixes one.
    """
    path = source_file.resolve(strict=True)
    if not path.is_file():
        raise ValueError("defect report must be a file")
    finding = load_finding(path) if is_finding_file(path) else None
    if finding is not None:
        # A finding record carries its conditions, and the briefing has to
        # keep saying which of them this host could not supply. Mailman #54.
        body = render_finding_markdown(finding)
        title = title or finding["title"]
    else:
        body = path.read_text(encoding="utf-8")
    if not body.strip():
        raise ValueError("defect report is empty")
    captured_at = datetime.now(UTC).isoformat()
    markdown = render_defect_report(
        title=title, body=body, source_file=path, captured_at=captured_at
    )
    (run_directory / "issue.md").write_text(markdown, encoding="utf-8")
    record = {
        "schema_version": 1,
        "source": "defect-report",
        "self_reported": True,
        "reference": None,
        "captured_at": captured_at,
        "source_file": str(path),
        "source_sha256": sha256(path.read_bytes()).hexdigest(),
        "title": title,
        "body_characters": len(body),
        "issue_markdown": str((run_directory / "issue.md").resolve()),
        "success": True,
    }
    if finding is not None:
        record["finding"] = finding
        record["unmet_conditions"] = [
            condition["name"] for condition in unmet_conditions(finding)
        ]
    _write_record(run_directory, record)
    return record


def issue_opened_at(run_directory: Path) -> datetime | None:
    """When the run's issue was opened, from its record or its captured text.

    A record written before `created_at` was kept still has the date in the
    rendered issue.md, so an older run is read from there (#178).
    """
    stamp = (load_issue_record(run_directory) or {}).get("created_at")
    if not isinstance(stamp, str):
        markdown = run_directory / "issue.md"
        stamp = None
        if markdown.is_file():
            for line in markdown.read_text(encoding="utf-8").splitlines()[:20]:
                if line.startswith("- Created at: "):
                    stamp = line.removeprefix("- Created at: ").strip()
                    break
    if not stamp:
        return None
    try:
        opened = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return opened if opened.tzinfo else opened.replace(tzinfo=UTC)


def predates_issue(row: dict[str, Any], opened: datetime | None) -> bool:
    """Whether a pull request was opened before the issue was.

    Such a pull request cannot be an attempt at the issue. xarray#9513 and
    #4461 matched a text search for #10639, predated it by one and five
    years, and were reported as stale attempts the body had to supersede
    (#178). A row that cites the issue is kept whatever its date.
    """
    if opened is None or row.get("references_issue"):
        return False
    stamp = row.get("created_at") or row.get("createdAt")
    if not isinstance(stamp, str):
        return False
    try:
        created = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return False
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    return created < opened


def load_issue_record(run_directory: Path) -> dict[str, Any] | None:
    path = run_directory / "issue.json"
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else None
