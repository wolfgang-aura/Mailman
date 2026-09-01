from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from mailman.executor import CommandResult


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
