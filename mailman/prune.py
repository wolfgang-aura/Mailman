"""Give back the disk a finished run no longer needs.

Each run keeps a full clone of the target in `workspace/` and a build
environment in `environment/`. Together they were 158 of 166 GB under
`.mailman/runs` on 2026-10-22, and nothing ever removed them. The reports next
to them are small and stay.

A run keeps both directories while anything may still use them: `ship` pushes
the branch from the workspace, PR follow-up merges main and re-verifies there,
and an active hunt may still orchestrate it. Before a workspace goes, the run's
own commits are bundled and the full diff against the base is saved, so
nothing the run produced is lost. `pruned.json` records what went and how to
rebuild it. https://github.com/wolfgang-aura/Mailman/issues/498
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from mailman.completion import read_object
from mailman.filed_watch import filed_rows, watch_path

RETENTION_DAYS = 14
PRUNABLE = ("workspace", "environment")
MARKER = "pruned.json"
BUNDLE = "pruned.bundle"
WORKTREE_DIFF = "pruned-worktree.diff"
# A diff this large is build output nobody ignored, not a candidate; keep the
# run rather than save it.
MAX_DIFF_BYTES = 50 * 1024 * 1024
GIT_TIMEOUT_SECONDS = 300

# A run in one of these is mid-stage, or a maintainer is waiting on it.
_ACTIVE_STATUSES = {
    "PRIMARY_RUNNING",
    "REVIEW_PENDING",
    "REVISION_REQUIRED",
    "VERIFICATION_PENDING",
    "MAINTAINER_CHANGES_REQUESTED",
}
_FINISHED_HUNTS = {"FILED", "ABANDONED"}
_RESOLVED_PULL_STATES = {"merged", "closed"}


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def last_activity(directory: Path, record: dict) -> datetime | None:
    """The latest moment the run record says anything happened."""
    moments = [_parse_time(record.get("created_at")), _parse_time(record.get("updated_at"))]
    moments += [_parse_time(entry.get("at")) for entry in record.get("history") or []
                if isinstance(entry, dict)]
    known = [moment for moment in moments if moment]
    if known:
        return max(known)
    try:
        return datetime.strptime(directory.name[:16], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None


def _hunt_runs(data_root: Path) -> set[str]:
    """Runs that belong to a hunt that is not finished."""
    held = set()
    for path in sorted((data_root.parent / "hunts").glob("*/hunt.json")):
        record = read_object(path)
        if record.get("status") in _FINISHED_HUNTS:
            continue
        held |= {entry.get("run_id") for entry in record.get("runs") or [] if entry.get("run_id")}
    return held


def _unresolved_filings(data_root: Path) -> dict[str, str]:
    """Filed runs whose pull request is not known to be merged or closed."""
    states = {}
    for row in read_object(watch_path(data_root)).get("rows") or []:
        if row.get("repository") and row.get("pull_request"):
            states[(row["repository"].lower(), int(row["pull_request"]))] = row.get("state")
    unresolved = {}
    for row in filed_rows(data_root):
        state = states.get((row["repository"].lower(), row["pull_request"]))
        if row.get("run_id") and state not in _RESOLVED_PULL_STATES:
            unresolved[row["run_id"]] = f"{row['url']} is {state or 'unknown'}"
    return unresolved


def tree_bytes(path: Path) -> int:
    """Bytes under `path`, never following a link or junction."""
    total = 0
    pending = [str(path)]
    while pending:
        try:
            entries = list(os.scandir(pending.pop()))
        except OSError:
            continue
        for entry in entries:
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if _is_link(info):
                continue
            if stat.S_ISDIR(info.st_mode):
                pending.append(entry.path)
            else:
                total += info.st_size
    return total


def _is_link(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _long(path: Path) -> str:
    text = os.path.abspath(str(path))
    if os.name == "nt" and not text.startswith("\\\\?\\"):
        return "\\\\?\\" + text
    return text


def _writable_then(function: Callable[[str], None], path: str) -> None:
    try:
        function(path)
    except PermissionError:
        # Git writes its pack files read-only.
        os.chmod(path, stat.S_IWRITE)
        function(path)


def remove_tree(path: Path) -> None:
    """Delete `path`. A link or junction inside it is removed, never entered."""
    pending: list[tuple[str, bool]] = [(_long(path), False)]
    while pending:
        current, emptied = pending.pop()
        if emptied:
            _writable_then(os.rmdir, current)
            continue
        pending.append((current, True))
        with os.scandir(current) as entries:
            for entry in entries:
                info = entry.stat(follow_symlinks=False)
                directory = stat.S_ISDIR(info.st_mode) or bool(
                    getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_DIRECTORY)
                if _is_link(info):
                    _writable_then(os.rmdir if directory else os.unlink, entry.path)
                elif directory:
                    pending.append((entry.path, False))
                else:
                    _writable_then(os.unlink, entry.path)


def _git(workspace: Path, *arguments: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(workspace), *arguments],
        capture_output=True, timeout=GIT_TIMEOUT_SECONDS, check=False, shell=False,
    )


def _said(result: subprocess.CompletedProcess) -> str:
    return (result.stderr or result.stdout or b"").decode("utf-8", "replace").strip()[:200]


def preserve_candidate(directory: Path, base_commit: str | None) -> dict:
    """Save the run's commits and full diff beside it before its clone goes.

    Raises `RuntimeError` when either cannot be saved; the run is then kept.
    """
    workspace = directory / "workspace"
    if not (workspace / ".git").exists():
        raise RuntimeError("workspace is not a git checkout")
    if not base_commit or _git(workspace, "cat-file", "-e", f"{base_commit}^{{commit}}").returncode:
        raise RuntimeError(f"base commit {base_commit or 'unrecorded'} is not in the workspace")
    saved: dict[str, Any] = {"bundle": None, "worktree_diff": None, "branches": []}

    listed = _git(workspace, "for-each-ref", "--format=%(refname)", "refs/heads/")
    if listed.returncode:
        raise RuntimeError(f"cannot list branches: {_said(listed)}")
    branches = []
    for ref in listed.stdout.decode("utf-8", "replace").split():
        ahead = _git(workspace, "rev-list", "--count", f"{base_commit}..{ref}")
        if ahead.returncode:
            raise RuntimeError(f"cannot count commits on {ref}: {_said(ahead)}")
        if int(ahead.stdout.strip() or 0):
            branches.append(ref)
    if branches:
        bundle = directory / BUNDLE
        made = _git(workspace, "bundle", "create", str(bundle), *branches, f"^{base_commit}")
        if made.returncode or not bundle.is_file():
            raise RuntimeError(f"bundle failed: {_said(made)}")
        saved["bundle"] = BUNDLE
        saved["branches"] = branches

    # Untracked files are part of the candidate too (Mailman #111).
    staged = _git(workspace, "add", "-A")
    if staged.returncode:
        raise RuntimeError(f"cannot stage the working tree: {_said(staged)}")
    diff = _git(workspace, "diff", "--cached", "--binary", base_commit)
    if diff.returncode:
        raise RuntimeError(f"cannot diff against the base: {_said(diff)}")
    if len(diff.stdout) > MAX_DIFF_BYTES:
        raise RuntimeError(f"working-tree diff is {len(diff.stdout)} bytes, over {MAX_DIFF_BYTES}")
    if diff.stdout:
        (directory / WORKTREE_DIFF).write_bytes(diff.stdout)
        saved["worktree_diff"] = WORKTREE_DIFF
    return saved


def plan_prune(data_root: Path, *, now: datetime | None = None,
               retention_days: int = RETENTION_DAYS,
               live_runs: set[str] = frozenset()) -> list[dict]:
    """One row per run: whether its clone and environment may go, and why."""
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=retention_days)
    in_hunts = _hunt_runs(data_root)
    filings = _unresolved_filings(data_root)
    rows = []
    for directory in sorted(path for path in data_root.iterdir() if path.is_dir()):
        record = read_object(directory / "run.json")
        present = [name for name in PRUNABLE
                   if (directory / name).is_dir() and not _is_link(os.lstat(directory / name))]
        row = {"run_id": directory.name, "status": record.get("status"),
               "present": present, "prune": False, "reason": ""}
        activity = last_activity(directory, record)
        if not present:
            row["reason"] = "nothing to prune"
        elif not record:
            row["reason"] = "no run.json"
        elif directory.name in live_runs:
            row["reason"] = "a process is running from its environment"
        elif directory.name in in_hunts:
            row["reason"] = "belongs to an unfinished hunt"
        elif directory.name in filings:
            row["reason"] = f"filed: {filings[directory.name]}"
        elif record.get("status") in _ACTIVE_STATUSES:
            row["reason"] = f"status {record['status']}"
        elif activity is None or activity > cutoff:
            row["reason"] = f"active within {retention_days} days"
        else:
            row["prune"] = True
            row["reason"] = f"last active {activity.date().isoformat()}"
        rows.append(row)
    return rows


def prune_run(directory: Path, present: list[str], *, now: datetime | None = None) -> dict:
    """Preserve the candidate, write the marker, then delete the directories."""
    record = read_object(directory / "run.json")
    earlier = read_object(directory / MARKER)
    if earlier:
        # An interrupted prune already saved the candidate and may have
        # deleted part of the clone; finish the deletion.
        saved = {key: earlier.get(key) for key in ("bundle", "worktree_diff", "branches")}
        present = sorted(set(present) | set(earlier.get("removed") or []))
        present = [name for name in present if (directory / name).is_dir()]
    else:
        saved = (preserve_candidate(directory, record.get("base_commit"))
                 if "workspace" in present else {})
    sizes = {name: tree_bytes(directory / name) for name in present}
    marker = {
        "schema_version": 1,
        "pruned_at": (now or datetime.now(UTC)).isoformat(),
        "removed": present,
        "bytes_freed": sum(sizes.values()),
        "bytes_by_directory": sizes,
        "base_commit": record.get("base_commit"),
        "repository": record.get("repository"),
        **saved,
        "restore": (
            "Clone the repository into workspace/, check out base_commit, then "
            f"`git fetch ../{BUNDLE} 'refs/heads/*:refs/heads/*'` for the branches, or "
            f"`git apply --index ../{WORKTREE_DIFF}` for the working tree. "
            "Rebuild environment/ with `mailman prepare-environment RUN_ID`."
        ),
    }
    # Written before anything is deleted, so an interrupted prune still says
    # where the candidate went.
    temporary = directory / (MARKER + ".tmp")
    temporary.write_text(json.dumps(marker, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(directory / MARKER)
    for name in present:
        remove_tree(directory / name)
    return marker
