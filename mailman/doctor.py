from __future__ import annotations

import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from mailman.artifacts import default_data_root
from mailman.identity import (
    IdentityError,
    is_private_email,
    machine_identity,
    resolve_identity,
)


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str
    required: bool


def _command_version(command: str, arguments: list[str]) -> str | None:
    executable = shutil.which(command)
    if not executable:
        return None
    completed = subprocess.run(
        [executable, *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
        check=False,
        shell=False,
    )
    output = (completed.stdout or completed.stderr).strip().splitlines()
    return output[0] if output else executable


def _base_interpreter_reachable_by_codex() -> tuple[bool, str]:
    """Say whether a sandboxed agent could execute this interpreter.

    Codex's Windows sandbox refuses to create a process from an executable
    under the user profile, and a virtual environment is only a trampoline to
    its base, so a base interpreter in `AppData` makes every prepared
    environment unrunnable for the reviewer. Verified on 2026-09-02: the same
    command fails from `AppData` and succeeds from `C:\\ProgramData`.
    """
    if platform.system() != "Windows":
        return True, "not a Windows host, so the sandbox rule does not apply"
    return describe_interpreter_reach(
        Path(getattr(sys, "base_prefix", sys.prefix)).resolve(), Path.home().resolve()
    )


def describe_interpreter_reach(base: Path, home: Path) -> tuple[bool, str]:
    """Judge one base interpreter against one user profile."""
    if home in base.parents or base == home:
        return False, (
            f"base interpreter is inside the user profile ({base}). A sandboxed "
            "Codex agent cannot execute it, so it will review without running "
            "anything. Copy an interpreter outside the profile, for example to "
            r"C:\ProgramData\mailman-python, and build run environments from it."
        )
    return True, f"base interpreter is outside the user profile ({base})"


def describe_commit_identity() -> tuple[bool, str]:
    """Whether a run's commits would carry a configured address or the machine's.

    Reported because the failure is silent and permanent: a clone with no
    identity of its own commits under the global one, and a personal address
    that reaches a public repository cannot be withdrawn.
    """
    try:
        identity = resolve_identity(default_data_root().resolve())
    except IdentityError as error:
        return False, f"configured identity is unusable: {error}"
    machine = machine_identity()
    if identity is None:
        global_email = machine.get("user.email") or "unset"
        return False, (
            "no identity configured, so runs would commit as "
            f"{global_email}. Set one with `mailman identity`."
        )
    detail = f"{identity.name} <{identity.email}>"
    if not is_private_email(identity.email):
        return False, f"{detail} is not a GitHub noreply address"
    return True, detail


def run_checks() -> list[Check]:
    python_ok = sys.version_info >= (3, 12)
    git_version = _command_version("git", ["--version"])
    gh_version = _command_version("gh", ["--version"])
    codex_version = _command_version("codex", ["--version"])
    claude_version = _command_version("claude", ["--version"])
    return [
        Check("commit identity", *describe_commit_identity(), True),
        Check(
            "python",
            python_ok,
            f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
            True,
        ),
        Check("agent-runnable python", *_base_interpreter_reachable_by_codex(), False),
        Check("git", git_version is not None, git_version or "not found", True),
        Check("gh", gh_version is not None, gh_version or "not found", False),
        Check("codex", codex_version is not None, codex_version or "not found", False),
        Check("claude", claude_version is not None, claude_version or "not found", False),
    ]
