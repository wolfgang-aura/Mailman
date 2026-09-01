from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class WorkspaceState:
    path: Path
    head: str
    clean: bool


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
    return WorkspaceState(path=workspace, head=head, clean=not bool(status))


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
