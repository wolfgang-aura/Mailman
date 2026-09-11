"""Local, checkable evidence that a contribution was accepted.

Everything that proves a run mattered lives on github.com and can move. A pull
request gets closed and superseded. A fork gets deleted, taking a closed pull
request's diff with it. A branch gets force-pushed. Asked to show the accepted
contributions, a run directory could offer only its own account of its own
work.

So: the patch, in a file, at submission time; and the terminal state of the
pull request, recorded when it is known, with a permalink that keeps working
after the fork is gone.

See https://github.com/wolfgang-aura/Mailman/issues/51.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from mailman.executor import clamp_timeout_seconds

PROVENANCE_FILENAME = "provenance.json"
PATCH_FILENAME = "contribution.patch"
SUBMISSION_DIRECTORY = "submission"

PROVENANCE_SCHEMA_VERSION = 1

# How old a stored pull request state may be before the listing says so.
STALE_AFTER = timedelta(days=1)

_SLUG = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")


class ProvenanceError(ValueError):
    """The evidence could not be produced, so nothing should claim it exists."""


@dataclass(frozen=True)
class Contribution:
    """One run's upstream commit, and what became of it."""

    run_id: str
    repository: str
    commits: tuple[str, ...] = ()
    pull_request: int | None = None
    state: str | None = None
    merge_commit: str | None = None
    superseded_by: int | None = None
    patch_path: str | None = None
    checked_at: str | None = None
    competition: dict[str, Any] | None = None

    def permalinks(self) -> tuple[str, ...]:
        return tuple(
            f"https://github.com/{self.repository}/commit/{sha}" for sha in self.commits
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "repository": self.repository,
            "commits": list(self.commits),
            "permalinks": list(self.permalinks()),
            "pull_request": self.pull_request,
            "state": self.state,
            "merge_commit": self.merge_commit,
            "superseded_by": self.superseded_by,
            "patch_path": self.patch_path,
            "checked_at": self.checked_at,
            "competition": self.competition,
        }


def repository_slug(repository: str) -> str:
    """`owner/name`, from either a clone URL or a slug that is already one."""
    value = repository.strip()
    if _SLUG.match(value):
        return value
    trimmed = re.sub(r"^(?:https://|ssh://|git@)", "", value)
    trimmed = trimmed.replace("github.com:", "github.com/")
    trimmed = trimmed.removeprefix("github.com/")
    trimmed = trimmed.removesuffix(".git").strip("/")
    if not _SLUG.match(trimmed):
        raise ProvenanceError(f"cannot read an owner/name out of {repository!r}")
    return trimmed


def _git(workspace: Path, arguments: list[str], *, timeout: float = 60) -> str:
    completed = subprocess.run(
        ["git", "-C", str(workspace), *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=clamp_timeout_seconds(timeout),
        check=False,
        shell=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise ProvenanceError(f"git {' '.join(arguments)} failed: {detail}")
    return completed.stdout


def write_patch(workspace: Path, base_commit: str, destination: Path) -> Path:
    """The run's commits as a file `git am` can replay, authorship intact.

    A diff would be smaller and would lose the author, the message and the
    co-author trailer, which are the parts that say whose work it was.
    """
    patch = _git(
        workspace, ["format-patch", f"{base_commit}..HEAD", "--stdout"], timeout=120
    )
    if not patch.strip():
        raise ProvenanceError(
            f"no commits between {base_commit} and HEAD, so there is nothing to record"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(patch, encoding="utf-8", newline="\n")
    return destination


def head_branch_tip(repository: str, head: str, *, timeout: float = 60) -> str | None:
    """The commit `OWNER:BRANCH` currently points at on github.com.

    Read from the fork itself rather than from the workspace, because the
    workspace is what goes stale: python/mypy#21961 was squashed and
    force-pushed after a reviewer asked, and provenance recorded two permalinks
    to commits that no longer existed on any branch.

    Returns None when the ref cannot be read at all, which the caller reports
    rather than treating as agreement.
    See https://github.com/wolfgang-aura/Mailman/issues/84.
    """
    owner, _, branch = head.partition(":")
    if not branch:
        owner, branch = "", head
    name = repository_slug(repository).split("/", 1)[1]
    fork = f"https://github.com/{owner or repository_slug(repository).split('/')[0]}/{name}.git"
    completed = subprocess.run(
        ["git", "ls-remote", fork, f"refs/heads/{branch}"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=clamp_timeout_seconds(timeout),
        check=False,
        shell=False,
    )
    if completed.returncode != 0:
        return None
    for line in completed.stdout.splitlines():
        sha = line.split("\t", 1)[0].strip()
        if sha:
            return sha
    return None


def pull_request_state(repository: str, number: int) -> dict[str, Any]:
    """What GitHub says became of the pull request, or why it could not say."""
    slug = repository_slug(repository)
    if shutil.which("gh") is None:
        return {"available": False, "detail": "gh is not installed"}
    try:
        completed = subprocess.run(
            [
                "gh",
                "pr",
                "view",
                str(number),
                "--repo",
                slug,
                "--json",
                "state,mergedAt,mergeCommit,url,title",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=clamp_timeout_seconds(30),
            check=False,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"available": False, "detail": str(error)}
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        return {"available": False, "detail": detail}
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        return {"available": False, "detail": f"unreadable response ({error})"}
    merge_commit = payload.get("mergeCommit") or {}
    return {
        "available": True,
        "state": payload.get("state"),
        "merged_at": payload.get("mergedAt"),
        "merge_commit": merge_commit.get("oid")
        if isinstance(merge_commit, dict)
        else None,
        "url": payload.get("url"),
        "title": payload.get("title"),
    }


_ISSUE_URL = re.compile(
    r"^https://github\.com/([A-Za-z0-9._-]+/[A-Za-z0-9._-]+)/issues/(\d+)/?$"
)


def upstream_issue_number(run_directory: Path, repository: str) -> int | None:
    """The issue this run set out to fix, if the run names one in `repository`.

    `run.json` carries the issue URL from the start of the run; the staged
    `submission.json` carries the number and target again. Either will do, and
    an issue in some other repository says nothing about this one.
    """
    slug = repository_slug(repository).lower()
    run_record = _read_json(run_directory / "run.json")
    match = _ISSUE_URL.match(str(run_record.get("issue") or "").strip())
    if match is not None and match.group(1).lower() == slug:
        return int(match.group(2))
    submission = _read_json(run_directory / SUBMISSION_DIRECTORY / "submission.json")
    number = submission.get("issue_number")
    target = str(submission.get("target") or "").lower()
    if isinstance(number, int) and number > 0 and target == slug:
        return number
    return None


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def competing_pull_requests(
    repository: str, issue_number: int, *, own_number: int
) -> dict[str, Any]:
    """Every other pull request in `repository` that references the issue.

    Read from the issue's timeline, where GitHub records each cross-reference
    as it is made. That catches `Fixes #N` in a body, a mention in a comment and
    a manual link, which a text search for `#N` does not. Pull requests from
    other repositories reference issues too and are left out; so are ones that
    closed without merging, which stopped competing.
    See https://github.com/wolfgang-aura/Mailman/issues/86.
    """
    slug = repository_slug(repository)
    if shutil.which("gh") is None:
        return {"available": False, "detail": "gh is not installed"}
    try:
        completed = subprocess.run(
            [
                "gh",
                "api",
                f"repos/{slug}/issues/{issue_number}/timeline",
                "--paginate",
                "--slurp",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=clamp_timeout_seconds(60),
            check=False,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"available": False, "detail": str(error)}
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        return {"available": False, "detail": detail}
    try:
        pages = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        return {"available": False, "detail": f"unreadable response ({error})"}
    events: list[Any] = []
    for page in pages if isinstance(pages, list) else []:
        events.extend(page if isinstance(page, list) else [])
    return {
        "available": True,
        "pull_requests": competitors_from_timeline(events, slug, own_number=own_number),
    }


def competitors_from_timeline(
    events: list[Any], repository: str, *, own_number: int
) -> list[dict[str, Any]]:
    """The competing pull requests in a GitHub issue timeline, oldest first."""
    found: dict[int, dict[str, Any]] = {}
    repository_url = f"https://api.github.com/repos/{repository}".lower()
    for event in events:
        if not isinstance(event, dict) or event.get("event") != "cross-referenced":
            continue
        source = event.get("source") or {}
        issue = source.get("issue") if isinstance(source, dict) else None
        if not isinstance(issue, dict) or not issue.get("pull_request"):
            continue
        if str(issue.get("repository_url") or "").lower() != repository_url:
            continue
        number = issue.get("number")
        if not isinstance(number, int) or number == own_number:
            continue
        pull_request = issue.get("pull_request")
        merged = isinstance(pull_request, dict) and bool(pull_request.get("merged_at"))
        state = "merged" if merged else str(issue.get("state") or "").lower()
        if state not in {"open", "merged"}:
            continue
        user = issue.get("user") or {}
        found[number] = {
            "number": number,
            "state": state,
            "author": user.get("login") if isinstance(user, dict) else None,
            "url": issue.get("html_url"),
            "created_at": issue.get("created_at"),
        }
    return [found[number] for number in sorted(found)]


def submission_directory(run_directory: Path) -> Path:
    return run_directory / SUBMISSION_DIRECTORY


def provenance_path(run_directory: Path) -> Path:
    return submission_directory(run_directory) / PROVENANCE_FILENAME


def load_provenance(run_directory: Path) -> dict[str, Any] | None:
    path = provenance_path(run_directory)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def record_provenance(
    *,
    run_id: str,
    run_directory: Path,
    repository: str,
    base_commit: str,
    workspace: Path | None = None,
    pull_request: int | None = None,
    head: str | None = None,
    superseded_by: int | None = None,
    state_lookup: Any = pull_request_state,
    head_lookup: Any = head_branch_tip,
) -> dict[str, Any]:
    """Write the patch and everything known about where the work ended up."""
    slug = repository_slug(repository)
    directory = submission_directory(run_directory)
    directory.mkdir(parents=True, exist_ok=True)
    existing = load_provenance(run_directory) or {}

    clone = workspace if workspace is not None else run_directory / "workspace"
    commits: list[str] = list(existing.get("commits") or [])
    patch_path: str | None = existing.get("patch_path")
    if clone.is_dir():
        commits = [
            line.strip()
            for line in _git(
                clone, ["rev-list", "--reverse", f"{base_commit}..HEAD"]
            ).splitlines()
            if line.strip()
        ]
        if commits:
            patch_path = str(
                write_patch(clone, base_commit, directory / PATCH_FILENAME)
            )

    branch = head or existing.get("head")
    if branch and commits:
        # Recording a head ref and then not reading it is worse than not
        # recording it: the permalinks would name commits the branch no longer
        # has. See https://github.com/wolfgang-aura/Mailman/issues/84.
        tip = head_lookup(slug, branch)
        if tip is None:
            raise ProvenanceError(
                f"could not read the tip of {branch} on github.com, so the "
                "commits in this workspace cannot be shown to be what was "
                "filed. Check the fork exists and the branch name is right."
            )
        if tip != commits[-1]:
            raise ProvenanceError(
                f"{branch} points at {tip}, but this workspace ends at "
                f"{commits[-1]}. The branch was force-pushed, or the workspace "
                "moved on. Reset the workspace to the pushed head and re-run, "
                "so the permalinks name commits the branch actually has."
            )

    record: dict[str, Any] = {
        "schema_version": PROVENANCE_SCHEMA_VERSION,
        "run_id": run_id,
        "repository": slug,
        "base_commit": base_commit,
        "head": head or existing.get("head"),
        "commits": commits,
        "permalinks": [f"https://github.com/{slug}/commit/{sha}" for sha in commits],
        "patch_path": patch_path,
        "pull_request": pull_request or existing.get("pull_request"),
        "state": existing.get("state"),
        "merge_commit": existing.get("merge_commit"),
        "superseded_by": superseded_by or existing.get("superseded_by"),
        "recorded_at": datetime.now(UTC).isoformat(),
        "checked_at": existing.get("checked_at"),
        "lookup": existing.get("lookup"),
    }
    number = record["pull_request"]
    if number:
        lookup = state_lookup(slug, int(number))
        record["lookup"] = lookup
        record["checked_at"] = datetime.now(UTC).isoformat()
        if lookup.get("available"):
            record["state"] = lookup.get("state")
            record["merge_commit"] = lookup.get("merge_commit")
            record["url"] = lookup.get("url")

    path = provenance_path(run_directory)
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8", newline="\n")
    return record


def contribution_from_record(record: dict[str, Any]) -> Contribution:
    return Contribution(
        run_id=str(record.get("run_id", "")),
        repository=str(record.get("repository", "")),
        commits=tuple(str(sha) for sha in record.get("commits") or ()),
        pull_request=record.get("pull_request"),
        state=record.get("state"),
        merge_commit=record.get("merge_commit"),
        superseded_by=record.get("superseded_by"),
        patch_path=record.get("patch_path"),
        checked_at=record.get("checked_at"),
        competition=record.get("competition")
        if isinstance(record.get("competition"), dict)
        else None,
    )


def _read_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def state_is_stale(checked_at: str | None, *, now: datetime | None = None) -> bool:
    """Whether a stored state is old enough that it may already be wrong.

    A pull request closes, merges or gets superseded without telling us. The
    only honest thing a record can say about an old reading is how old it is.
    """
    moment = _read_timestamp(checked_at)
    if moment is None:
        return True
    return (now or datetime.now(UTC)) - moment > STALE_AFTER


def refresh_state(
    run_directory: Path,
    *,
    state_lookup: Any = pull_request_state,
    competitor_lookup: Any = competing_pull_requests,
    now: datetime | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Re-read one run's pull request state from GitHub and store the answer.

    Unlike `record_provenance` this reads neither the workspace nor the patch,
    so it still works after the clone is gone. A lookup that fails leaves every
    stored field alone and returns why: a state written on filing day beats one
    invented now.

    While the pull request is open, the issue it fixes is read too, for a pull
    request someone else filed against it. python/mypy#21967 was opened against
    the issue python/mypy#21961 fixed and nothing here noticed for a day.
    See https://github.com/wolfgang-aura/Mailman/issues/86.
    """
    record = load_provenance(run_directory)
    if record is None:
        return None, None
    number = record.get("pull_request")
    if not number:
        return record, None
    slug = str(record.get("repository") or "")
    lookup = state_lookup(slug, int(number))
    if not lookup.get("available"):
        detail = lookup.get("detail") or "gh gave no reason"
        return record, f"{slug}#{number}: {detail}"
    record["lookup"] = lookup
    record["checked_at"] = (now or datetime.now(UTC)).isoformat()
    record["state"] = lookup.get("state")
    record["merge_commit"] = lookup.get("merge_commit")
    if lookup.get("url"):
        record["url"] = lookup.get("url")
    record["competition"] = _read_competition(
        run_directory, slug, int(number), record["state"], competitor_lookup, now=now
    )
    path = provenance_path(run_directory)
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8", newline="\n")
    competition = record["competition"]
    if competition.get("detail"):
        detail = competition["detail"]
        return record, f"{slug}#{number}: competitors unchecked: {detail}"
    return record, None


def _read_competition(
    run_directory: Path,
    slug: str,
    number: int,
    state: str | None,
    competitor_lookup: Any,
    *,
    now: datetime | None,
) -> dict[str, Any]:
    """What was read about other pull requests on the run's issue, or why not.

    Only an open pull request can be overtaken, so a closed or merged one is
    not looked up; its record says why the list is empty.
    """
    checked_at = (now or datetime.now(UTC)).isoformat()
    if (state or "").upper() != "OPEN":
        return {"checked_at": checked_at, "skipped": f"the pull request is {state}"}
    issue_number = upstream_issue_number(run_directory, slug)
    if issue_number is None:
        return {
            "checked_at": checked_at,
            "detail": "run.json names no issue in this repository",
        }
    lookup = competitor_lookup(slug, issue_number, own_number=number)
    if not lookup.get("available"):
        return {
            "checked_at": checked_at,
            "issue": issue_number,
            "detail": lookup.get("detail") or "gh gave no reason",
        }
    return {
        "checked_at": checked_at,
        "issue": issue_number,
        "pull_requests": list(lookup.get("pull_requests") or []),
    }


def refresh_contributions(
    data_root: Path,
    *,
    state_lookup: Any = pull_request_state,
    competitor_lookup: Any = competing_pull_requests,
    now: datetime | None = None,
) -> tuple[list[Contribution], list[str]]:
    """Every recorded run, re-read from GitHub, with whatever could not be."""
    found: list[Contribution] = []
    failures: list[str] = []
    if not data_root.is_dir():
        return found, failures
    for directory in sorted(path for path in data_root.glob("*") if path.is_dir()):
        record, failure = refresh_state(
            directory,
            state_lookup=state_lookup,
            competitor_lookup=competitor_lookup,
            now=now,
        )
        if record is None:
            continue
        if failure:
            failures.append(failure)
        found.append(contribution_from_record(record))
    return found, failures


def collect_contributions(data_root: Path) -> list[Contribution]:
    """Every run that recorded provenance, oldest run id first."""
    found: list[Contribution] = []
    if not data_root.is_dir():
        return found
    for directory in sorted(path for path in data_root.glob("*") if path.is_dir()):
        record = load_provenance(directory)
        if record is None:
            continue
        found.append(contribution_from_record(record))
    return found


def unrecorded_submissions(data_root: Path) -> list[str]:
    """Runs whose submission is ready and whose provenance was never written.

    Nothing after `handoff` requires `provenance` to run, so a pull request the
    operator filed and moved on from is absent from the ledger entirely, and
    the ledger looks complete either way. Two PRs filed on 2026-09-09 were
    invisible until a hand audit found them.
    See https://github.com/wolfgang-aura/Mailman/issues/84.
    """
    pending: list[str] = []
    if not data_root.is_dir():
        return pending
    for directory in sorted(path for path in data_root.glob("*") if path.is_dir()):
        submission = directory / SUBMISSION_DIRECTORY / "submission.json"
        if not submission.is_file():
            continue
        try:
            record = json.loads(submission.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(record, dict) or record.get("ready") is not True:
            continue
        if load_provenance(directory) is None:
            pending.append(directory.name)
    return pending


def render_contributions(
    contributions: list[Contribution], *, now: datetime | None = None
) -> str:
    """A table to hand someone, with the permalink that outlives the fork.

    Every state carries the moment it was read. Without that, a state stored on
    filing day reads exactly like one read a minute ago.
    """
    if not contributions:
        return "no run has recorded provenance yet"
    lines = []
    for entry in contributions:
        state = entry.state or "unrecorded"
        pull_request = f"#{entry.pull_request}" if entry.pull_request else "unsubmitted"
        lines.append(f"{entry.run_id}  {entry.repository}  {pull_request}  {state}")
        if entry.pull_request:
            lines.append(f"    {_reading_age(entry, now=now)}")
            lines.extend(f"    {line}" for line in _competition_lines(entry))
        for link in entry.permalinks():
            lines.append(f"    {link}")
        if entry.merge_commit:
            lines.append(
                f"    merged as https://github.com/{entry.repository}"
                f"/commit/{entry.merge_commit}"
            )
        if entry.patch_path:
            lines.append(f"    patch {entry.patch_path}")
    return "\n".join(lines)


def competitors(entry: Contribution) -> list[dict[str, Any]]:
    """The other pull requests read against this run's issue, if any were read."""
    competition = entry.competition or {}
    found = competition.get("pull_requests")
    if not isinstance(found, list):
        return []
    return [item for item in found if isinstance(item, dict)]


def _competition_lines(entry: Contribution) -> list[str]:
    """What is known about other pull requests on the issue.

    Silence is not an option for an open pull request: an unchecked one would
    read exactly like an unchallenged one.
    """
    if (entry.state or "").upper() != "OPEN":
        return []
    competition = entry.competition
    if competition is None:
        return [
            "competing pull requests never read -- run `mailman contributions --refresh`"
        ]
    if competition.get("detail"):
        return [f"competing pull requests unchecked: {competition['detail']}"]
    found = competitors(entry)
    if not found:
        return [f"no competing pull request on issue #{competition.get('issue')}"]
    return [
        f"COMPETING: #{item.get('number')} {item.get('state')} by {item.get('author')}, "
        f"opened {item.get('created_at')} {item.get('url')}"
        for item in found
    ]


def _reading_age(entry: Contribution, *, now: datetime | None = None) -> str:
    if not entry.checked_at:
        return "state never read from GitHub -- run `mailman contributions --refresh`"
    if state_is_stale(entry.checked_at, now=now):
        return (
            f"state read {entry.checked_at}, stale -- "
            "run `mailman contributions --refresh`"
        )
    return f"state read {entry.checked_at}"


def deletion_is_safe(record: dict[str, Any]) -> tuple[bool, str]:
    """Whether the fork holding this work can go without losing evidence.

    Two ways to be safe, and one of them does not depend on GitHub: the pull
    request merged, so the commits are on the upstream default branch; or the
    patch is on this disk. Anything else and the diff dies with the fork.
    """
    state = (record.get("state") or "").upper()
    if state == "MERGED" or record.get("merge_commit"):
        return True, "the pull request merged, so the commits are upstream"
    patch = record.get("patch_path")
    if patch and Path(patch).is_file():
        return True, f"the patch is on disk at {patch}"
    if state == "OPEN":
        return False, "the pull request is open, and deleting the fork would close it"
    return False, (
        "nothing proves this work survives the fork: it has not merged and no "
        "patch was written. Run `mailman provenance` before deleting anything."
    )
