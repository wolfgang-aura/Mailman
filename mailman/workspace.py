from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from mailman.executor import execute


@dataclass(frozen=True)
class WorkspaceState:
    path: Path
    head: str
    clean: bool
    changes: tuple[str, ...] = ()

    def describe_changes(self, limit: int = 10) -> str:
        """Name the paths that make a workspace dirty, so a human can act."""
        if not self.changes:
            return "no recorded changes"
        shown = ", ".join(self.changes[:limit])
        if len(self.changes) > limit:
            return f"{shown}, and {len(self.changes) - limit} more"
        return shown


def _git(workspace: Path, arguments: list[str]) -> str:
    completed = subprocess.run(
        ["git", "-C", str(workspace), *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
        shell=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise ValueError(f"Git workspace check failed: {detail}")
    return completed.stdout.strip()


def inspect_workspace(path: Path) -> WorkspaceState:
    workspace = path.resolve(strict=True)
    if not workspace.is_dir():
        raise ValueError("workspace must be a directory")
    head = _git(workspace, ["rev-parse", "HEAD"])
    status = _git(workspace, ["status", "--porcelain=v1", "--untracked-files=all"])
    changes = tuple(line.strip() for line in status.splitlines() if line.strip())
    return WorkspaceState(
        path=workspace, head=head, clean=not changes, changes=changes
    )


def workspace_fingerprint(path: Path) -> str:
    """Digest a workspace's uncommitted state.

    Status alone cannot tell that an already modified file was modified again,
    so the tracked diff goes into the digest too. Untracked files contribute
    their paths, which is what status reports for them.
    """
    workspace = path.resolve(strict=True)
    status = _git(workspace, ["status", "--porcelain=v1", "--untracked-files=all"])
    diff = _git(workspace, ["diff", "HEAD"])
    return hashlib.sha256((status + "\n" + diff).encode("utf-8")).hexdigest()


def commit_is_ancestor(workspace: Path, ancestor: str) -> bool:
    completed = subprocess.run(
        ["git", "-C", str(workspace), "merge-base", "--is-ancestor", ancestor, "HEAD"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
        shell=False,
    )
    if completed.returncode == 0:
        return True
    if completed.returncode == 1:
        return False
    detail = completed.stderr.strip() or completed.stdout.strip()
    raise ValueError(f"Git ancestry check failed: {detail}")


def _write_workspace_record(destination: Path, record: dict[str, object]) -> None:
    temporary = destination.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    temporary.replace(destination)


def prepare_workspace(
    *,
    repository: str,
    base_commit: str,
    run_directory: Path,
    timeout_seconds: float,
) -> dict[str, object]:
    destination = run_directory / "workspace"
    record_path = run_directory / "workspace.json"
    if destination.exists():
        if not destination.is_dir():
            raise ValueError("workspace path exists and is not a directory")
        if not record_path.is_file():
            raise ValueError("existing workspace has no preparation record")
        state = inspect_workspace(destination)
        origin = _git(destination, ["remote", "get-url", "origin"])
        if origin != repository or state.head != base_commit or not state.clean:
            raise ValueError("existing workspace does not match the recorded clean base")
        record = json.loads(record_path.read_text(encoding="utf-8"))
        if (
            record.get("repository") != repository
            or record.get("base_commit") != base_commit
            or not record.get("success")
            or not isinstance(record.get("clone"), dict)
            or not isinstance(record.get("checkout"), dict)
        ):
            raise ValueError("existing workspace preparation record does not match")
        record.update(
            {
                "head": state.head,
                "clean": state.clean,
                "reused": True,
                "reuse_count": int(record.get("reuse_count", 0)) + 1,
            }
        )
        _write_workspace_record(record_path, record)
        return record

    empty_hooks = run_directory / "empty-git-hooks"
    empty_hooks.mkdir(exist_ok=True)
    controlled_environment = {
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
    }
    clone = execute(
        [
            "git",
            "-c",
            f"core.hooksPath={empty_hooks}",
            "-c",
            f"init.templateDir={empty_hooks}",
            "clone",
            "--no-checkout",
            "--origin",
            "origin",
            repository,
            str(destination),
        ],
        working_directory=run_directory,
        timeout_seconds=timeout_seconds,
        environment=controlled_environment,
    )
    record: dict[str, object] = {
        "repository": repository,
        "base_commit": base_commit,
        "path": str(destination.resolve()),
        "reused": False,
        "reuse_count": 0,
        "clone": clone.to_dict(),
        "checkout": None,
        "success": False,
    }
    if clone.timed_out or clone.exit_code != 0:
        _write_workspace_record(record_path, record)
        return record

    checkout = execute(
        [
            "git",
            "-c",
            f"core.hooksPath={empty_hooks}",
            "-C",
            str(destination),
            "checkout",
            "--detach",
            base_commit,
        ],
        working_directory=run_directory,
        timeout_seconds=timeout_seconds,
        environment=controlled_environment,
    )
    record["checkout"] = checkout.to_dict()
    if checkout.timed_out or checkout.exit_code != 0:
        _write_workspace_record(record_path, record)
        return record

    state = inspect_workspace(destination)
    record.update(
        {
            "head": state.head,
            "clean": state.clean,
            "changes": list(state.changes),
            "success": state.head == base_commit and state.clean,
        }
    )
    if state.head == base_commit and not state.clean:
        # A fresh clone that is already dirty is a property of the target
        # repository, not of the run. Name the paths instead of making the
        # human re-run Git to find them.
        record["detail"] = (
            "the checkout is dirty immediately after cloning: "
            f"{state.describe_changes()}"
        )
    _write_workspace_record(record_path, record)
    return record
