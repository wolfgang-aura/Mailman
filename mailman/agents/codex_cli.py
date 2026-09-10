from __future__ import annotations

import json
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
from mailman.transcript import CODEX, observed_model


# The CLI's own catalog for the 5.6 family. `ultra` is not offered on every
# model, so an unsupported pairing has to fail here rather than mid-run.
REASONING_EFFORTS = ("low", "medium", "high", "xhigh", "max", "ultra")
DEFAULT_TOKEN_BUDGET = 2_000_000
DEFAULT_TOOL_OUTPUT_TOKEN_LIMIT = 20_000


def _thread_id(stdout: str) -> str | None:
    for line in stdout.splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if payload.get("type") == "thread.started" and payload.get("thread_id"):
            return str(payload["thread_id"])
    return None


@dataclass(frozen=True)
class CodexCliAgent(EngineeringAgent):
    executable: str = "codex"
    model: str | None = None
    reasoning_effort: str | None = None
    """How hard the model is asked to think. Recorded with the run, because a
    review at `max` and one at `low` are not the same evidence."""
    windows_sandbox: str | None = (
        "elevated" if platform.system() == "Windows" else None
    )
    token_budget_limit: int = DEFAULT_TOKEN_BUDGET
    tool_output_token_limit: int = DEFAULT_TOOL_OUTPUT_TOKEN_LIMIT

    @property
    def name(self) -> str:
        return "codex"

    @property
    def token_budget(self) -> int | None:
        return self.token_budget_limit

    def build_command(self, request: AgentRequest) -> list[str]:
        if self.windows_sandbox not in {None, "elevated", "unelevated"}:
            raise ValueError("Windows sandbox mode must be elevated or unelevated")
        if request.role not in {"primary", "reviewer"}:
            raise ValueError("agent role must be primary or reviewer")
        if self.reasoning_effort and self.reasoning_effort not in REASONING_EFFORTS:
            raise ValueError(
                f"unsupported reasoning effort: {self.reasoning_effort!r}. "
                f"Supported: {', '.join(REASONING_EFFORTS)}."
            )
        if self.token_budget_limit <= 0:
            raise ValueError("Codex token budget must be positive")
        if self.tool_output_token_limit <= 0:
            raise ValueError("Codex tool output token limit must be positive")
        sandbox_mode = "workspace-write" if request.role == "primary" else "read-only"
        if request.role == "reviewer" and request.scratch_directory is not None:
            # A read-only sandbox blocks temp writes everywhere, so a suite
            # that needs a temp directory failed inside the review for reasons
            # that had nothing to do with the candidate: run
            # 20260902T144544Z-5dbf69 saw two FileNotFoundError from
            # tempfile.py and a Permission denied on .pytest_cache. The
            # workspace the reviewer may now write is watched by the
            # orchestrator's workspace-change:reviewer step, so an edit ships
            # nowhere. See https://github.com/wolfgang-aura/Mailman/issues/29.
            sandbox_mode = "workspace-write"
        command = [self.executable, "exec"]
        if request.session_id:
            command.append("resume")
        command.append("--ignore-user-config")
        if self.windows_sandbox:
            command.extend(
                ["--config", f"windows.sandbox='{self.windows_sandbox}'"]
            )
        if request.scratch_directory is not None:
            # Single-quoted TOML literal strings: double quotes would make the
            # backslashes in a Windows path escape sequences.
            scratch = request.scratch_directory.resolve().as_posix()
            command.extend(
                ["--config", f"sandbox_workspace_write.writable_roots=['{scratch}']"]
            )
        command.extend(["--json", "--output-last-message", str(request.report_path.resolve())])
        if not request.session_id:
            command.extend(
                [
                    "--color",
                    "never",
                    "--sandbox",
                    sandbox_mode,
                    "--cd",
                    str(request.workspace.resolve()),
                ]
            )
        if self.model:
            command.extend(["--model", self.model])
        if self.reasoning_effort:
            command.extend(
                ["--config", f"model_reasoning_effort={self.reasoning_effort!r}"]
            )
        command.extend(
            [
                "--enable",
                "token_budget",
                "--config",
                f"token_budget.limit_tokens={self.token_budget_limit}",
                "--config",
                f"tool_output_token_limit={self.tool_output_token_limit}",
            ]
        )
        if request.session_id:
            command.append(request.session_id)
        command.append("-")
        return command

    def run(self, request: AgentRequest) -> AgentResult:
        prompt = request.prompt_path.read_text(encoding="utf-8")
        environment = None
        if request.scratch_directory is not None:
            scratch = str(request.scratch_directory.resolve())
            # The scratch directory is where temp writes belong. pytest's own
            # cache writes into the workspace root, so it is disabled for the
            # agent's runs: repeated focused checks must not dirty the candidate.
            environment = {
                "TMP": scratch,
                "TEMP": scratch,
                "TMPDIR": scratch,
                "PYTEST_ADDOPTS": "-p no:cacheprovider",
            }
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
            environment=environment,
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
            stop_reason=result.stopped_reason,
            observed_model=observed_model(result.stdout, CODEX),
            session_id=_thread_id(result.stdout),
        )
