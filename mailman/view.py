"""Render a stored run as something a person can read.

A run leaves a lot of evidence on disk - orchestration steps, agent execution
records, verification output - and until now none of it was rendered anywhere.
This module turns that evidence into a timeline, so the owner can see what the
agents did instead of only the verdict they reached.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from mailman.transcript import parse_stream

_DIVIDER = "-" * 78


@dataclass(frozen=True)
class AgentExecution:
    number: int
    path: Path
    record: dict[str, Any]

    @property
    def agent(self) -> str:
        return str(self.record.get("agent", "?"))

    @property
    def role(self) -> str:
        return str(self.record.get("role", "?"))

    @property
    def stdout(self) -> str:
        process = self.record.get("process") or {}
        return str(process.get("stdout", ""))

    @property
    def log_path(self) -> Path:
        return self.path.with_suffix(".log")


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def agent_executions(run_directory: Path) -> list[AgentExecution]:
    directory = run_directory / "agent-executions"
    if not directory.is_dir():
        return []
    executions: list[AgentExecution] = []
    for index, path in enumerate(sorted(directory.glob("*.json")), start=1):
        executions.append(AgentExecution(index, path, _read_json(path)))
    return executions


def _duration(record: dict[str, Any]) -> str:
    seconds = (record.get("process") or {}).get("duration_seconds")
    if not isinstance(seconds, (int, float)):
        return ""
    if seconds < 90:
        return f"{seconds:.0f}s"
    return f"{seconds / 60:.1f}m"


def _header(run: dict[str, Any], orchestration: dict[str, Any]) -> list[str]:
    lines = [
        f"run       {run.get('run_id', '?')}",
        f"status    {run.get('status', orchestration.get('final_status', '?'))}",
    ]
    issue = run.get("issue")
    if isinstance(issue, dict):
        issue = issue.get("url") or issue.get("reference") or ""
    repository = run.get("repository") or ""
    if issue or repository:
        lines.append(f"issue     {repository} {issue}".strip())
    if orchestration:
        lines.append(
            f"outcome   {orchestration.get('final_status', '?')}, "
            f"{orchestration.get('revisions_used', 0)} revision(s), "
            f"{orchestration.get('review_cycles', 0)} review cycle(s)"
        )
    return [line for line in lines if line.strip()]


def _steps(orchestration: dict[str, Any]) -> list[str]:
    steps = orchestration.get("steps") or []
    if not steps:
        return []
    lines = ["", "timeline", _DIVIDER]
    for step in steps:
        if not isinstance(step, dict):
            continue
        mark = "ok  " if step.get("ok") else "stop"
        at = str(step.get("at", ""))[11:19]
        lines.append(f"{at}  {mark} {step.get('name', '?')}: {step.get('detail', '')}")
    return lines


def _transcript(execution: AgentExecution, *, width: int, full: bool) -> list[str]:
    events = parse_stream(execution.stdout.splitlines(), execution.agent)
    duration = _duration(execution.record)
    heading = (
        f"{execution.number:04d} {execution.role} - {execution.agent}"
        f"{f', {duration}' if duration else ''}, {len(events)} event(s)"
    )
    lines = ["", heading, _DIVIDER]
    if not events:
        lines.append(
            "no machine-readable output was captured for this agent. "
            "Runs before the transcript layer only stored the final report."
        )
        return lines
    for event in events:
        lines.append(event.line(width=width))
        if full and event.detail and event.kind in {"says", "result", "error"}:
            lines.extend(f"    {line}" for line in event.detail.splitlines())
    return lines


def _reports(run_directory: Path) -> list[str]:
    lines: list[str] = []
    for role in ("primary", "reviewer"):
        path = run_directory / f"{role}-report.md"
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace").strip()
        if not text:
            continue
        lines.extend(["", f"{role} report", _DIVIDER, text])
    return lines


def render_run(
    run_directory: Path,
    *,
    width: int = 120,
    full: bool = False,
    reports: bool = True,
) -> str:
    """Render one run's evidence as a readable report."""
    run = _read_json(run_directory / "run.json")
    orchestration = _read_json(run_directory / "orchestration.json")
    lines = _header(run, orchestration)
    lines.extend(_steps(orchestration))
    for execution in agent_executions(run_directory):
        lines.extend(_transcript(execution, width=width, full=full))
    if reports:
        lines.extend(_reports(run_directory))
    return "\n".join(lines)


def write_transcript_logs(run_directory: Path, *, width: int = 160) -> list[Path]:
    """Write a plain-text transcript beside each stored agent execution."""
    written: list[Path] = []
    for execution in agent_executions(run_directory):
        events = parse_stream(execution.stdout.splitlines(), execution.agent)
        if not events:
            continue
        body = "\n".join(event.line(width=width) for event in events)
        execution.log_path.write_text(body + "\n", encoding="utf-8")
        written.append(execution.log_path)
    return written


def summarize_runs(run_directories: Iterable[Path]) -> str:
    """One line per run, newest last."""
    lines: list[str] = []
    for directory in run_directories:
        run = _read_json(directory / "run.json")
        orchestration = _read_json(directory / "orchestration.json")
        executions = agent_executions(directory)
        agents = ", ".join(
            f"{execution.role}:{execution.agent}" for execution in executions
        )
        lines.append(
            f"{directory.name}  {str(run.get('status', '?')):<16} "
            f"{orchestration.get('final_status', '-'):<10} {agents}"
        )
    return "\n".join(lines)
