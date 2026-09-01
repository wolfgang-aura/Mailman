from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass


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


def run_checks() -> list[Check]:
    python_ok = sys.version_info >= (3, 12)
    git_version = _command_version("git", ["--version"])
    codex_version = _command_version("codex", ["--version"])
    claude_version = _command_version("claude", ["--version"])
    return [
        Check(
            "python",
            python_ok,
            f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
            True,
        ),
        Check("git", git_version is not None, git_version or "not found", True),
        Check("codex", codex_version is not None, codex_version or "not found", False),
        Check("claude", claude_version is not None, claude_version or "not found", False),
    ]
