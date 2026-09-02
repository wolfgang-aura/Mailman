from __future__ import annotations

import shutil
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from mailman.executor import CommandResult
from mailman.transcript import TranscriptEvent, parse_line


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
    on_event: Callable[[TranscriptEvent], None] | None = None
    verification_command: tuple[str, ...] = ()
    """The command the run verifies with. An agent that cannot run it cannot
    check its own work, so the adapter has to permit it explicitly."""

    def observe(self, agent: str) -> Callable[[str], None] | None:
        """Turn one line of agent output into events for whoever is watching."""
        if self.on_event is None:
            return None
        sink = self.on_event

        def handle(line: str) -> None:
            for event in parse_line(line, agent):
                sink(event)

        return handle


@dataclass(frozen=True)
class AgentResult:
    exit_code: int | None
    report_path: Path
    timed_out: bool
    report_present: bool
    command_result: CommandResult
    stop_reason: str | None = None
    """Why the agent stopped, when its CLI says so. ``error_max_turns`` and a
    bare exit code are very different failures and used to look identical."""


class EngineeringAgent(ABC):
    """Interchangeable boundary for a primary engineer or reviewer."""

    @property
    @abstractmethod
    def name(self) -> str:
        raise NotImplementedError

    @property
    def turn_budget(self) -> int | None:
        """How many turns the agent is allowed, when its CLI bounds them.

        Recorded with every execution so a run that stopped at its limit can
        be read against the limit it was given. ``None`` when the CLI has no
        such bound.
        """
        return None

    @abstractmethod
    def run(self, request: AgentRequest) -> AgentResult:
        raise NotImplementedError
