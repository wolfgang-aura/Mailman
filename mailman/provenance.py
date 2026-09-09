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
    now: datetime | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Re-read one run's pull request state from GitHub and store the answer.

    Unlike `record_provenance` this reads neither the workspace nor the patch,
    so it still works after the clone is gone. A lookup that fails leaves every
    stored field alone and returns why: a state written on filing day beats one
    invented now.
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
    path = provenance_path(run_directory)
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8", newline="\n")
    return record, None


def refresh_contributions(
    data_root: Path,
    *,
    state_lookup: Any = pull_request_state,
    now: datetime | None = None,
) -> tuple[list[Contribution], list[str]]:
    """Every recorded run, re-read from GitHub, with whatever could not be."""
    found: list[Contribution] = []
    failures: list[str] = []
    if not data_root.is_dir():
        return found, failures
    for directory in sorted(path for path in data_root.glob("*") if path.is_dir()):
        record, failure = refresh_state(directory, state_lookup=state_lookup, now=now)
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
