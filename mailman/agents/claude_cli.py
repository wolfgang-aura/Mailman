from __future__ import annotations

from dataclasses import dataclass

from mailman.agents.base import (
    AgentRequest,
    AgentResult,
    EngineeringAgent,
    resolve_executable,
)
from mailman.executor import execute
from mailman.redaction import redact


_UPSTREAM_WRITE_DENYLIST = ",".join(
    (
        "Bash(git push:*)",
        "Bash(gh pr create:*)",
        "Bash(gh issue comment:*)",
        "Bash(gh issue close:*)",
    )
)


@dataclass(frozen=True)
class ClaudeCliAgent(EngineeringAgent):
    executable: str = "claude"
    model: str | None = None
    max_turns: int = 30

    @property
    def name(self) -> str:
        return "claude"

    def build_command(self, request: AgentRequest) -> list[str]:
        if self.max_turns <= 0:
            raise ValueError("max_turns must be positive")
        if request.role not in {"primary", "reviewer"}:
            raise ValueError("agent role must be primary or reviewer")
        permission_mode = "acceptEdits" if request.role == "primary" else "plan"
        command = [
            self.executable,
            "--print",
            "--input-format",
            "text",
            "--output-format",
            "text",
            "--permission-mode",
            permission_mode,
            "--max-turns",
            str(self.max_turns),
            "--disallowedTools",
            _UPSTREAM_WRITE_DENYLIST,
        ]
        if self.model:
            command.extend(["--model", self.model])
        return command

    def run(self, request: AgentRequest) -> AgentResult:
        prompt = request.prompt_path.read_text(encoding="utf-8")
        command = self.build_command(request)
        command[0] = resolve_executable(command[0])
        result = execute(
            command,
            working_directory=request.workspace,
            timeout_seconds=request.timeout_seconds,
            stdin_text=prompt,
        )
        report_present = result.exit_code == 0 and bool(result.stdout.strip())
        if report_present:
            request.report_path.write_text(redact(result.stdout), encoding="utf-8")
        return AgentResult(
            exit_code=result.exit_code,
            report_path=request.report_path,
            timed_out=result.timed_out,
            report_present=report_present,
            command_result=result,
        )
