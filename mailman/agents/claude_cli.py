from __future__ import annotations

import json
import re
from dataclasses import dataclass

from mailman.agents.base import (
    AgentRequest,
    AgentResult,
    EngineeringAgent,
    resolve_executable,
)
from mailman.executor import execute
from mailman.redaction import redact
from mailman.transcript import CLAUDE, final_message, observed_model, parse_stream


_UPSTREAM_WRITE_DENYLIST = ",".join(
    (
        "Bash(git push:*)",
        "Bash(gh pr create:*)",
        "Bash(gh issue comment:*)",
        "Bash(gh issue close:*)",
    )
)

# Under acceptEdits every Bash call is referred to a human, and in --print
# there is no human, so the agent spends its turns collecting refusals. These
# are the commands it needs to reproduce a bug and check a fix. The denylist
# above still wins, so nothing here reaches the upstream repository.
_READ_COMMANDS = (
    "git status",
    "git diff",
    "git log",
    "git show",
    "git branch",
    "git rev-parse",
    "git ls-files",
)

_TEST_COMMANDS = ("python", "python3", "py", "pytest", "tox", "make")

# Thirty turns was the old default and it never once finished an external
# target: run 20260902T111426Z-3fc769 spent all thirty on pytest and wrote no
# report at all. The run that reached a verdict on the same issue was given
# 120, so that is the budget an unfamiliar codebase actually costs.
DEFAULT_MAX_TURNS = 120


_DRIVE_PATH = re.compile(r"([A-Za-z]):[\\\\/](.*)")


def command_spellings(command: str) -> list[str]:
    """Every spelling of one executable a shell command might use.

    Claude Code matches an allow rule as text. A rule built from a single
    spelling of the interpreter denies the same interpreter written with the
    other separator, and the agent can only find the permitted form by
    collecting refusals: run 20260902T144544Z-5dbf69 spent five of its first
    fourteen events doing exactly that.
    """
    spellings = [command]
    candidates = [
        command.replace('/', '\\'),
        command.replace('\\', '/'),
    ]
    match = _DRIVE_PATH.fullmatch(command)
    if match:
        drive, rest = match.groups()
        candidates.append(f"/{drive.lower()}/" + rest.replace('\\', '/'))
    for candidate in candidates:
        if candidate and candidate not in spellings:
            spellings.append(candidate)
    return spellings


def _allowed_tools(role: str, verification_command: tuple[str, ...]) -> str:
    """Permit the commands the role needs and nothing else.

    The reviewer only has to read the candidate change. The primary agent also
    has to run the suite, which is the whole point of giving it a workspace.
    """
    commands = list(_READ_COMMANDS)
    if role == "primary":
        commands.extend(_TEST_COMMANDS)
    rules = [f"Bash({command}:*)" for command in commands]
    if role == "primary" and verification_command:
        # The verification command is usually an absolute path into the run's
        # own interpreter, which no prefix above covers. Permit every spelling
        # of that path, not only the one Mailman happened to write.
        rules.extend(
            f"Bash({spelling}:*)"
            for spelling in command_spellings(verification_command[0])
        )
    return ",".join(rules)


@dataclass(frozen=True)
class ClaudeCliAgent(EngineeringAgent):
    executable: str = "claude"
    model: str | None = None
    max_turns: int = DEFAULT_MAX_TURNS

    @property
    def name(self) -> str:
        return "claude"

    @property
    def turn_budget(self) -> int | None:
        return self.max_turns

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
        command.extend(
            [
                "--allowedTools",
                _allowed_tools(request.role, request.verification_command),
            ]
        )
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
            observed_model=observed_model(result.stdout, CLAUDE),
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
