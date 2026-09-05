from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from mailman.identity import (
    Identity,
    IdentityError,
    apply_identity,
    author_violations,
    branch_commits,
    is_private_email,
    load_identity,
    resolve_identity,
    save_identity,
)


PRIVATE = "169568318+wolfgang-aura@users.noreply.github.com"
PERSONAL = "someone.real@gmail.com"


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return completed.stdout.strip()


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "--initial-branch=main")
    _git(path, "config", "user.name", "Machine Default")
    _git(path, "config", "user.email", PERSONAL)
    (path / "file.txt").write_text("base\n", encoding="utf-8")
    _git(path, "add", "file.txt")
    _git(path, "commit", "-m", "base")
    return path


def test_private_email_recognises_the_noreply_suffix() -> None:
    assert is_private_email(PRIVATE)
    assert is_private_email("  " + PRIVATE.upper() + " ")
    assert not is_private_email(PERSONAL)


def test_resolve_prefers_the_environment_over_the_data_root(tmp_path: Path) -> None:
    save_identity(tmp_path, Identity(name="From File", email=PRIVATE))
    resolved = resolve_identity(
        tmp_path,
        environment={"MAILMAN_GIT_NAME": "From Env", "MAILMAN_GIT_EMAIL": PRIVATE},
    )
    assert resolved == Identity(name="From Env", email=PRIVATE)


def test_resolve_never_falls_back_to_the_machine(tmp_path: Path) -> None:
    # The whole point: an unconfigured identity is visible, not substituted.
    assert resolve_identity(tmp_path, environment={}) is None


def test_saved_identity_round_trips(tmp_path: Path) -> None:
    identity = Identity(name="wolfgang-aura", email=PRIVATE)
    save_identity(tmp_path, identity)
    assert load_identity(tmp_path) == identity


def test_a_half_configured_environment_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(IdentityError):
        resolve_identity(tmp_path, environment={"MAILMAN_GIT_NAME": "No Address"})


def test_a_malformed_address_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(IdentityError):
        resolve_identity(
            tmp_path,
            environment={"MAILMAN_GIT_NAME": "N", "MAILMAN_GIT_EMAIL": "not-an-email"},
        )


def test_applied_identity_beats_the_repository_default(repository: Path) -> None:
    apply_identity(repository, Identity(name="wolfgang-aura", email=PRIVATE))
    (repository / "file.txt").write_text("changed\n", encoding="utf-8")
    _git(repository, "commit", "-am", "change")
    assert _git(repository, "log", "-1", "--format=%ae") == PRIVATE


def test_a_commit_under_the_machine_identity_is_a_violation(repository: Path) -> None:
    base = _git(repository, "rev-parse", "HEAD")
    (repository / "file.txt").write_text("changed\n", encoding="utf-8")
    _git(repository, "commit", "-am", "change")
    commits = branch_commits(repository, base)
    assert len(commits) == 1
    assert commits[0]["author_email"] == PERSONAL

    identity = Identity(name="wolfgang-aura", email=PRIVATE)
    violations = author_violations(commits, identity)
    assert len(violations) == 1
    assert violations[0]["sha"] == commits[0]["sha"]
    roles = {entry["role"] for entry in violations[0]["emails"]}
    assert roles == {"author", "committer"}


def test_a_noreply_address_passes_whoever_owns_it(repository: Path) -> None:
    base = _git(repository, "rev-parse", "HEAD")
    apply_identity(repository, Identity(name="Someone Else", email="9+x" + "@users.noreply.github.com"))
    (repository / "file.txt").write_text("changed\n", encoding="utf-8")
    _git(repository, "commit", "-am", "change")
    commits = branch_commits(repository, base)
    identity = Identity(name="wolfgang-aura", email=PRIVATE)
    assert author_violations(commits, identity) == []


def test_without_a_configured_identity_every_personal_address_is_a_violation(
    repository: Path,
) -> None:
    base = _git(repository, "rev-parse", "HEAD")
    (repository / "file.txt").write_text("changed\n", encoding="utf-8")
    _git(repository, "commit", "-am", "change")
    commits = branch_commits(repository, base)
    assert author_violations(commits, None)


def test_an_unchanged_branch_has_nothing_to_check(repository: Path) -> None:
    base = _git(repository, "rev-parse", "HEAD")
    assert branch_commits(repository, base) == []
