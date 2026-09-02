from __future__ import annotations

import platform
from dataclasses import dataclass

from mailman.agents.base import (
    AgentRequest,
    AgentResult,
    EngineeringAgent,
    resolve_executable,
)
from mailman.executor import execute
from mailman.redaction import redact


@dataclass(frozen=True)
class CodexCliAgent(EngineeringAgent):
    executable: str = "codex"
    model: str | None = None
    windows_sandbox: str | None = (
        "elevated" if platform.system() == "Windows" else None
    )

    @property
    def name(self) -> str:
        return "codex"

    def build_command(self, request: AgentRequest) -> list[str]:
        if self.windows_sandbox not in {None, "elevated", "unelevated"}:
            raise ValueError("Windows sandbox mode must be elevated or unelevated")
        if request.role not in {"primary", "reviewer"}:
            raise ValueError("agent role must be primary or reviewer")
        sandbox_mode = "workspace-write" if request.role == "primary" else "read-only"
        command = [
            self.executable,
            "exec",
            "--ephemeral",
            "--ignore-user-config",
        ]
        if self.windows_sandbox:
            command.extend(
                ["--config", f"windows.sandbox='{self.windows_sandbox}'"]
            )
        command.extend(
            [
                "--color",
                "never",
                "--json",
                "--sandbox",
                sandbox_mode,
                "--cd",
                str(request.workspace.resolve()),
                "--output-last-message",
                str(request.report_path.resolve()),
            ]
        )
        if self.model:
            command.extend(["--model", self.model])
        command.append("-")
        return command

    def run(self, request: AgentRequest) -> AgentResult:
        prompt = request.prompt_path.read_text(encoding="utf-8")
        report_before = (
            (
                request.report_path.stat().st_mtime_ns,
                request.report_path.read_bytes(),
            )
            if request.report_path.is_file()
            else None
        )
        command = self.build_command(request)
        command[0] = resolve_executable(command[0])
        result = execute(
            command,
            working_directory=request.workspace,
            timeout_seconds=request.timeout_seconds,
            stdin_text=prompt,
            on_stdout_line=request.observe(self.name),
        )
        report_after = (
            (
                request.report_path.stat().st_mtime_ns,
                request.report_path.read_bytes(),
            )
            if request.report_path.is_file()
            else None
        )
        report_present = bool(
            report_after
            and report_after != report_before
            and report_after[1].strip()
        )
        if report_present and report_after:
            report = report_after[1].decode("utf-8", errors="replace")
            request.report_path.write_text(redact(report), encoding="utf-8")
        return AgentResult(
            exit_code=result.exit_code,
            report_path=request.report_path,
            timed_out=result.timed_out,
            report_present=report_present,
            command_result=result,
        )
