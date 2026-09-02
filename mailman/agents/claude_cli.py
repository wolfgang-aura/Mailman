from __future__ import annotations

import json
from dataclasses import dataclass

from mailman.agents.base import (
    AgentRequest,
    AgentResult,
    EngineeringAgent,
    resolve_executable,
)
from mailman.executor import execute
from mailman.redaction import redact
from mailman.transcript import CLAUDE, final_message, parse_stream


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
            # The stream carries the whole thread. Plain text carries only the
            # last message, which left every Claude run unobservable.
            "--output-format",
            "stream-json",
            "--verbose",
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
            on_stdout_line=request.observe(self.name),
        )
        report, stop_reason = _final_result(result.stdout)
        report_present = result.exit_code == 0 and bool(report and report.strip())
        if report_present and report:
            request.report_path.write_text(redact(report), encoding="utf-8")
        return AgentResult(
            exit_code=result.exit_code,
            report_path=request.report_path,
            timed_out=result.timed_out,
            report_present=report_present,
            command_result=result,
            stop_reason=stop_reason,
        )


def _final_result(stdout: str) -> tuple[str | None, str | None]:
    """Pull the report and the stop reason out of a stream-json transcript.

    The CLI ends with a single ``result`` event. Its ``subtype`` says whether
    the agent finished or ran out of turns, which is the difference between a
    considered stop and a truncated one.
    """
    report: str | None = None
    stop_reason: str | None = None
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped.startswith("{"):
            if stripped:
                stop_reason = stop_reason or stripped
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict) or payload.get("type") != "result":
            continue
        stop_reason = str(payload.get("subtype") or "")
        text = payload.get("result")
        if isinstance(text, str) and text.strip():
            report = text
    if report is None:
        report = final_message(parse_stream(stdout.splitlines(), CLAUDE))
    return report, stop_reason or None
