"""Checks between completed engineering and a filing the operator can approve."""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

from mailman.artifacts import load_run, write_run
from mailman.identity import author_violations, branch_commits, resolve_identity
from mailman.models import RunStatus, utc_now
from mailman.review_decision import load_decision


def read_object(path: Path) -> dict:
    if not path.is_file():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected an object in {path}")  # noqa: TRY004 -- invalid file content
    return value


def git_bytes(workspace: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(workspace), *args], capture_output=True,
        timeout=60, check=False, shell=False,
    )
    if result.returncode:
        raise ValueError("Git inspection failed: " + result.stderr.decode("utf-8", "replace"))
    return result.stdout


def candidate_digest(workspace: Path, base: str) -> str:
    """Cover tracked and untracked bytes, unchanged by staging or committing."""
    digest = hashlib.sha256()
    # A temporary index lets Git hash the candidate without changing its real index.
    import os
    import tempfile
    with tempfile.TemporaryDirectory(prefix="mailman-index-") as name:
        environment = {**os.environ, "GIT_INDEX_FILE": str(Path(name) / "index")}
        for args in (["read-tree", "HEAD"], ["add", "--all"], ["write-tree"]):
            result = subprocess.run(
                ["git", "-C", str(workspace), *args], env=environment,
                capture_output=True, timeout=60, check=False, shell=False,
            )
            if result.returncode:
                raise ValueError("cannot fingerprint candidate: " + result.stderr.decode("utf-8", "replace"))
        digest.update(base.encode("utf-8"))
        digest.update(result.stdout.strip())
    return digest.hexdigest()


def check_authorship(directory: Path, *, head: str | None = None) -> dict:
    run, _ = load_run(directory.name, directory.parent)
    exported = read_object(directory / "export" / "export.json")
    workspace = Path(exported.get("workspace") or directory / "workspace")
    identity = resolve_identity(directory.parent)
    if identity is None:
        raise ValueError("identity-missing: configure `mailman identity` before preparing a submission")
    ref = "HEAD"
    if head:
        branch = head.split(":", 1)[-1]
        ref = "refs/heads/" + branch
        tip = git_bytes(workspace, "rev-parse", "--verify", ref).decode().strip()
        current = git_bytes(workspace, "rev-parse", "HEAD").decode().strip()
        if tip != current:
            raise ValueError("head-mismatch: the filing branch differs from the inspected workspace HEAD")
        if git_bytes(workspace, "status", "--porcelain").strip():
            raise ValueError("uncommitted-candidate: commit the reviewed change before handoff")
    commits = branch_commits(workspace, run.base_commit)
    if head and not commits:
        raise ValueError("empty-branch: no candidate commits exist on the filing branch")
    violations = author_violations(commits, identity)
    if violations:
        # The diagnostic names commits, never the private addresses.
        raise ValueError("author-identity: correct author/committer identity on " + ", ".join(v["sha"] for v in violations))
    return {"ok": True, "commits": len(commits), "head": git_bytes(workspace, "rev-parse", ref).decode().strip()}


def finalize_review(directory: Path) -> dict:
    run, _ = load_run(directory.name, directory.parent)
    if run.status not in (RunStatus.ENGINEERING_COMPLETE, RunStatus.READY_FOR_HUMAN_REVIEW):
        raise ValueError("engineering-incomplete: complete independent verification and review first")
    decision = load_decision(directory)
    orchestration = read_object(directory / "orchestration.json")
    finals = [s for s in orchestration.get("steps", []) if s.get("name") == "verification:final" and s.get("ok")]
    if not finals:
        raise ValueError("final-verification-missing: no passing final verification exists")
    evidence = finals[-1].get("data", {})
    expected = evidence.get("candidate_digest")
    if not expected:
        raise ValueError("candidate-unbound: repeat verification and independent review with this Mailman version")
    workspace = Path(evidence["workspace"])
    if candidate_digest(workspace, run.base_commit) != expected:
        raise ValueError("candidate-changed: repeat verification and independent review for the edited candidate")
    if run.status is RunStatus.ENGINEERING_COMPLETE:
        run.transition(RunStatus.READY_FOR_HUMAN_REVIEW, "decision validated against the verified candidate")
        write_run(run, directory)
    record = {
        "schema_version": 1, "at": utc_now(), "candidate_digest": expected,
        "decision_digest": hashlib.sha256((directory / "decision.json").read_bytes()).hexdigest(),
        "recommendation": decision.recommendation,
    }
    (directory / "completion.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return record
