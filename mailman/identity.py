"""Whose name and address end up on a commit that leaves this machine.

A run's workspace is a clone like any other, so `git commit` inside it uses
whatever identity the machine happens to carry. On the machine this was written
for, that was a personal address containing the owner's real name, and it
reached a third party's repository, where it cannot be taken back.

Two defences, because either alone leaks. The clone gets an explicit local
identity, which beats the global one. And nothing is handed over for pushing
until every commit on the branch has been read and found acceptable.

See https://github.com/wolfgang-aura/Mailman/issues/52.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


IDENTITY_FILENAME = "identity.json"

NOREPLY_SUFFIX = "@users.noreply.github.com"

NAME_VARIABLE = "MAILMAN_GIT_NAME"
EMAIL_VARIABLE = "MAILMAN_GIT_EMAIL"


@dataclass(frozen=True)
class Identity:
    """The author a run's commits are made under."""

    name: str
    email: str

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "email": self.email}


class IdentityError(ValueError):
    """The configured identity is unusable, and a run must not proceed."""


def is_private_email(email: str) -> bool:
    """True for an address GitHub issues instead of a personal mailbox."""
    return email.strip().lower().endswith(NOREPLY_SUFFIX)


def _validate(name: str, email: str, *, source: str) -> Identity:
    name = name.strip()
    email = email.strip()
    if not name:
        raise IdentityError(f"the identity from {source} has an empty name")
    if not email:
        raise IdentityError(f"the identity from {source} has an empty email")
    if "@" not in email or email.startswith("@") or email.endswith("@"):
        raise IdentityError(f"{email!r} from {source} is not an email address")
    return Identity(name=name, email=email)


def identity_path(data_root: Path) -> Path:
    return data_root / IDENTITY_FILENAME


def load_identity(data_root: Path) -> Identity | None:
    """The identity recorded under the data root, if one was ever saved."""
    path = identity_path(data_root)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise IdentityError(f"{path} is not valid JSON ({error})") from error
    if not isinstance(payload, dict):
        raise IdentityError(f"{path} does not hold an object")
    return _validate(
        str(payload.get("name", "")), str(payload.get("email", "")), source=str(path)
    )


def save_identity(data_root: Path, identity: Identity) -> Path:
    path = identity_path(data_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(identity.to_dict(), indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    return path


def resolve_identity(
    data_root: Path | None = None, environment: dict[str, str] | None = None
) -> Identity | None:
    """The identity to commit under: environment first, then the data root.

    Deliberately never falls back to `git config --global`. Falling back is
    what put a personal address on a public commit; an unset identity has to
    be visible, not quietly substituted.
    """
    source = os.environ if environment is None else environment
    name = source.get(NAME_VARIABLE, "")
    email = source.get(EMAIL_VARIABLE, "")
    if name or email:
        return _validate(name, email, source="the environment")
    if data_root is None:
        return None
    return load_identity(data_root)


def machine_identity() -> dict[str, str | None]:
    """What plain `git commit` would use here, for reporting only."""
    values: dict[str, str | None] = {}
    for key in ("user.name", "user.email"):
        completed = subprocess.run(
            ["git", "config", "--global", "--get", key],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
            shell=False,
        )
        value = completed.stdout.strip()
        values[key] = value or None
    return values


def apply_identity(workspace: Path, identity: Identity) -> None:
    """Write the identity into the clone's own config, where it wins."""
    for key, value in (("user.name", identity.name), ("user.email", identity.email)):
        completed = subprocess.run(
            ["git", "-C", str(workspace), "config", "--local", key, value],
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
            raise IdentityError(f"could not set {key} on {workspace}: {detail}")


def branch_commits(workspace: Path, base_commit: str) -> list[dict[str, str]]:
    """Every commit the run added on top of the base, newest first."""
    separator = "\x1f"
    completed = subprocess.run(
        [
            "git",
            "-C",
            str(workspace),
            "log",
            f"--format=%H{separator}%an{separator}%ae{separator}%cn{separator}%ce",
            f"{base_commit}..HEAD",
        ],
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
        raise IdentityError(f"could not read commits on {workspace}: {detail}")
    commits: list[dict[str, str]] = []
    for line in completed.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split(separator)
        if len(parts) != 5:
            continue
        commits.append(
            {
                "sha": parts[0],
                "author_name": parts[1],
                "author_email": parts[2],
                "committer_name": parts[3],
                "committer_email": parts[4],
            }
        )
    return commits


def author_violations(
    commits: list[dict[str, str]], identity: Identity | None
) -> list[dict[str, Any]]:
    """Commits carrying an address that must not be published.

    Anything ending in GitHub's noreply suffix is fine whoever it belongs to:
    it is already the address a person chose to be seen under. Everything else
    has to match the configured identity exactly.
    """
    allowed = {identity.email.strip().lower()} if identity else set()
    violations: list[dict[str, Any]] = []
    for commit in commits:
        offending = []
        for role in ("author", "committer"):
            email = commit.get(f"{role}_email", "").strip()
            if not email:
                continue
            if is_private_email(email) or email.lower() in allowed:
                continue
            offending.append({"role": role, "email": email})
        if offending:
            violations.append({"sha": commit["sha"], "emails": offending})
    return violations
