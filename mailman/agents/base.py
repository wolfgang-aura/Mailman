from __future__ import annotations

import shutil
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from mailman.executor import CommandResult


def resolve_executable(name: str) -> str:
    """Return an absolute path for an agent executable, or explain what is missing.

    Commands run without a shell, so a bare name has to be resolved here. On
    Windows that also picks up the ``.cmd`` and ``.exe`` launchers that a bare
    name would otherwise miss.
    """
    candidate = Path(name)
    if candidate.is_absolute() or len(candidate.parts) > 1:
        if not candidate.is_file():
            raise FileNotFoundError(f"agent executable does not exist: {name}")
        return str(candidate.resolve())
    found = shutil.which(name)
    if found is None:
        raise FileNotFoundError(
            f"agent executable {name!r} was not found on PATH. Install it, or "
            f"register it for the run with `mailman probe-tool --name {name}`."
        )
    return found


@dataclass(frozen=True)
class AgentRequest:
    run_id: str
    role: str
    prompt_path: Path
    workspace: Path
    report_path: Path
    timeout_seconds: float = 3600


@dataclass(frozen=True)
class AgentResult:
    exit_code: int | None
    report_path: Path
    timed_out: bool
    report_present: bool
    command_result: CommandResult


class EngineeringAgent(ABC):
    """Interchangeable boundary for a primary engineer or reviewer."""

    @property
    @abstractmethod
    def name(self) -> str:
        raise NotImplementedError

    @abstractmethod
    def run(self, request: AgentRequest) -> AgentResult:
        raise NotImplementedError
