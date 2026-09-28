"""Chain the deterministic steps between engineering and filing.

After `orchestrate` completes, a candidate went through eight commands, each
one a separate coordinator turn: export, submission checks, decision
validation, finalize, commit, author check, handoff and handoff check. The
coordinator still writes the three things only it can write (the target
policy, the final body and decision.json); `package` runs everything else and
stops at the first failure with the stage that failed.
https://github.com/wolfgang-aura/Mailman/issues/167
"""

from __future__ import annotations

import re
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from mailman.identity import Identity

DIFF_HEADER = re.compile(r"^diff --git a/(.+?) b/(.+)$", re.MULTILINE)


def changed_paths(diff: str) -> list[str]:
    """Every path the exported diff touches, deletions included."""
    paths: list[str] = []
    for old, new in DIFF_HEADER.findall(diff):
        for path in (old, new):
            if path not in paths:
                paths.append(path)
    return paths


def _git(workspace: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(workspace), *arguments],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=120, check=False,
    )


def commit_candidate(workspace: Path, *, base_commit: str, branch: str,
                     message: str, identity: Identity, paths: Sequence[str]) -> str:
    """Commit the exported paths on `branch` and return the commit.

    Already committed and clean is success, so `package` can be rerun after a
    later stage fails. Only the exported paths are staged: a workspace also
    holds reproducer output and caches that must not reach the pull request.
    """
    if not paths:
        raise ValueError("the exported diff names no paths to commit")
    current = _git(workspace, "branch", "--show-current").stdout.strip()
    if current != branch:
        exists = _git(workspace, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
        switched = _git(workspace, "switch", *(() if exists.returncode == 0 else ("-c",)), branch)
        if switched.returncode:
            raise ValueError(f"git switch {branch} failed: {switched.stderr.strip()}")
    # A path named in the diff but absent from both the tree and the index
    # makes `git add` fatal, so stage only what git reports as changed.
    listed = _git(workspace, "ls-files", "--modified", "--deleted", "--others",
                  "--exclude-standard", "--", *paths)
    pending = sorted({line for line in listed.stdout.splitlines() if line})
    if pending:
        added = _git(workspace, "add", "-A", "--", *pending)
        if added.returncode:
            raise ValueError(f"git add failed: {added.stderr.strip()}")
    staged = _git(workspace, "diff", "--cached", "--quiet")
    if staged.returncode:
        committed = _git(
            workspace, "-c", f"user.name={identity.name}", "-c", f"user.email={identity.email}",
            "commit", "-q", "-m", message,
        )
        if committed.returncode:
            raise ValueError(f"git commit failed: {committed.stderr.strip()}")
    ahead = _git(workspace, "rev-list", "--count", f"{base_commit}..HEAD").stdout.strip()
    if ahead in ("", "0"):
        raise ValueError(f"branch {branch} has no commit on top of {base_commit[:12]}")
    leftover = _git(workspace, "status", "--porcelain", "--", *paths).stdout.strip()
    if leftover:
        raise ValueError(f"exported paths still differ from the commit:\n{leftover}")
    return _git(workspace, "rev-parse", "HEAD").stdout.strip()


def run_stages(stages: Sequence[tuple[str, Callable[[], int]]],
               *, stream=None) -> tuple[int, list[dict]]:
    """Run stages in order and stop at the first non-zero exit."""
    stream = stream or sys.stderr
    record: list[dict] = []
    for name, stage in stages:
        print(f"package: {name} ...", file=stream, flush=True)
        started = time.monotonic()
        try:
            code = stage()
        except ValueError as error:
            print(f"error: {error}", file=stream)
            code = 2
        record.append({"stage": name, "exit_code": code,
                       "seconds": round(time.monotonic() - started, 1)})
        if code:
            print(f"package: stopped at {name} (exit {code}). Fix it and rerun "
                  "`mailman package`; finished stages are rerun safely.", file=stream)
            return code, record
    return 0, record
