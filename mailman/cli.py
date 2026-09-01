from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from mailman.agents import ClaudeCliAgent, CodexCliAgent, EngineeringAgent
from mailman.agents.base import AgentRequest
from mailman.artifacts import (
    append_agent_execution,
    append_verification,
    create_run,
    load_run,
    write_run,
)
from mailman.doctor import run_checks
from mailman.executor import execute
from mailman.models import RunStatus
from mailman.toolchain import prepare_agent_prompt, probe_tool
from mailman.workspace import commit_is_ancestor, inspect_workspace


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mailman",
        description="Evidence-first software engineering agent harness",
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    subparsers.add_parser("doctor", help="check required and optional local tools")

    init_run = subparsers.add_parser("init-run", help="create a private local run record")
    init_run.add_argument("--repository", required=True)
    init_run.add_argument("--issue", required=True)
    init_run.add_argument("--base-commit", required=True)
    init_run.add_argument("--primary", required=True)
    init_run.add_argument("--reviewer", required=True)
    init_run.add_argument("--primary-model")
    init_run.add_argument("--reviewer-model")
    init_run.add_argument("--data-root", type=Path)

    transition = subparsers.add_parser("transition", help="change a run workflow state")
    transition.add_argument("run_id")
    transition.add_argument("target", choices=[str(status) for status in RunStatus])
    transition.add_argument("--reason", required=True)
    transition.add_argument("--data-root", type=Path)

    run_agent = subparsers.add_parser(
        "run-agent", help="run one configured agent without changing workflow status"
    )
    run_agent.add_argument("run_id")
    run_agent.add_argument("--role", required=True, choices=("primary", "reviewer"))
    run_agent.add_argument("--prompt", required=True, type=Path)
    run_agent.add_argument("--workspace", required=True, type=Path)
    run_agent.add_argument("--model")
    run_agent.add_argument("--timeout", type=float, default=3600)
    run_agent.add_argument("--max-turns", type=int, default=30)
    run_agent.add_argument("--data-root", type=Path)

    probe = subparsers.add_parser(
        "probe-tool", help="verify and register one executable for a run"
    )
    probe.add_argument("run_id")
    probe.add_argument("--name", required=True)
    probe.add_argument("--executable", required=True, type=Path)
    probe.add_argument("--probe-arg", action="append")
    probe.add_argument("--timeout", type=float, default=30)
    probe.add_argument("--data-root", type=Path)

    verify = subparsers.add_parser("verify", help="run and record a verification command")
    verify.add_argument("run_id")
    verify.add_argument("--data-root", type=Path)
    verify.add_argument("--working-directory", type=Path, default=Path.cwd())
    verify.add_argument("--timeout", type=float, default=900)
    return parser


def _doctor() -> int:
    checks = run_checks()
    for check in checks:
        requirement = "required" if check.required else "optional"
        state = "OK" if check.ok else "MISSING"
        print(f"{state:7} {check.name:8} {check.detail} ({requirement})")
    return 1 if any(check.required and not check.ok for check in checks) else 0


def _init_run(arguments: argparse.Namespace) -> int:
    run, run_directory = create_run(
        repository=arguments.repository,
        issue=arguments.issue,
        base_commit=arguments.base_commit,
        primary=arguments.primary,
        reviewer=arguments.reviewer,
        primary_model=arguments.primary_model,
        reviewer_model=arguments.reviewer_model,
        data_root=arguments.data_root,
    )
    print(json.dumps({"run_id": run.run_id, "path": str(run_directory)}, indent=2))
    return 0


def _transition(arguments: argparse.Namespace) -> int:
    run, run_directory = load_run(arguments.run_id, arguments.data_root)
    run.transition(RunStatus(arguments.target), arguments.reason)
    write_run(run, run_directory)
    print(json.dumps({"run_id": run.run_id, "status": str(run.status)}, indent=2))
    return 0


def _make_agent(
    name: str, *, model: str | None, max_turns: int
) -> EngineeringAgent:
    normalized = name.strip().lower()
    if normalized == "codex":
        return CodexCliAgent(model=model)
    if normalized == "claude":
        return ClaudeCliAgent(model=model, max_turns=max_turns)
    raise ValueError(f"unsupported engineering agent: {name}")


def _run_agent(arguments: argparse.Namespace) -> int:
    run, run_directory = load_run(arguments.run_id, arguments.data_root)
    workspace_state = inspect_workspace(arguments.workspace)
    if arguments.role == "primary" and workspace_state.head != run.base_commit:
        raise ValueError(
            f"workspace HEAD {workspace_state.head} does not match base commit "
            f"{run.base_commit}"
        )
    if arguments.role == "reviewer" and not commit_is_ancestor(
        workspace_state.path, run.base_commit
    ):
        raise ValueError(
            f"base commit {run.base_commit} is not an ancestor of reviewer "
            f"workspace HEAD {workspace_state.head}"
        )
    if arguments.role == "primary" and not workspace_state.clean:
        raise ValueError("primary workspace must be clean before agent execution")
    source_prompt = arguments.prompt.resolve(strict=True)
    if not source_prompt.is_file():
        raise ValueError("prompt must be a file")
    prompt_path = prepare_agent_prompt(
        run_directory, role=arguments.role, source_prompt=source_prompt
    )

    configured = run.primary if arguments.role == "primary" else run.reviewer
    model = arguments.model or configured.model
    agent = _make_agent(configured.agent, model=model, max_turns=arguments.max_turns)
    report_path = run_directory / f"{arguments.role}-report.md"
    request = AgentRequest(
        run_id=run.run_id,
        role=arguments.role,
        prompt_path=prompt_path,
        workspace=workspace_state.path,
        report_path=report_path,
        timeout_seconds=arguments.timeout,
    )
    print(
        f"Starting {agent.name} as {arguments.role} with a "
        f"{arguments.timeout:g} second timeout.",
        flush=True,
    )
    result = agent.run(request)
    report_text = (
        result.report_path.read_text(encoding="utf-8", errors="replace")
        if result.report_present
        else None
    )
    execution_record = {
        "agent": agent.name,
        "model": model,
        "role": arguments.role,
        "report_path": str(result.report_path),
        "report_present": result.report_present,
        "report": report_text,
        "prompt_path": str(prompt_path),
        "process": result.command_result.to_dict(),
        "workflow_status_after_run": str(run.status),
    }
    record_path = append_agent_execution(
        run_directory, arguments.role, execution_record
    )
    summary = {
        "agent": agent.name,
        "model": model,
        "role": arguments.role,
        "process_exit_code": result.exit_code,
        "timed_out": result.timed_out,
        "report_present": result.report_present,
        "execution_record": str(record_path),
        "workflow_status": str(run.status),
        "status_changed": False,
    }
    print(json.dumps(summary, indent=2))
    if result.timed_out:
        return 124
    return result.exit_code or 0


def _probe_tool(arguments: argparse.Namespace) -> int:
    _, run_directory = load_run(arguments.run_id, arguments.data_root)
    probe_arguments = arguments.probe_arg or ["--version"]
    result = probe_tool(
        run_directory,
        name=arguments.name,
        executable=arguments.executable,
        probe_arguments=probe_arguments,
        timeout_seconds=arguments.timeout,
    )
    summary = {
        "name": arguments.name,
        "executable": str(arguments.executable.resolve()),
        "exit_code": result.exit_code,
        "timed_out": result.timed_out,
        "registered": result.exit_code == 0 and not result.timed_out,
    }
    print(json.dumps(summary, indent=2))
    if result.timed_out:
        return 124
    return result.exit_code or 0


def _verify(arguments: argparse.Namespace) -> int:
    command = list(arguments.command)
    if not command:
        raise ValueError("a command is required after --")
    _, run_directory = load_run(arguments.run_id, arguments.data_root)
    result = execute(
        command,
        working_directory=arguments.working_directory,
        timeout_seconds=arguments.timeout,
    )
    command_number = append_verification(run_directory, result.to_dict())
    summary = {
        "record": command_number,
        "exit_code": result.exit_code,
        "timed_out": result.timed_out,
        "duration_seconds": result.duration_seconds,
    }
    print(json.dumps(summary, indent=2))
    if result.timed_out:
        return 124
    return result.exit_code or 0


def main(arguments: list[str] | None = None) -> int:
    raw_arguments = list(arguments if arguments is not None else sys.argv[1:])
    verification_command: list[str] | None = None
    if raw_arguments[:1] == ["verify"] and "--" in raw_arguments:
        delimiter = raw_arguments.index("--")
        verification_command = raw_arguments[delimiter + 1 :]
        raw_arguments = raw_arguments[:delimiter]

    parser = _build_parser()
    parsed = parser.parse_args(raw_arguments)
    if parsed.subcommand == "verify":
        parsed.command = verification_command or []
    try:
        if parsed.subcommand == "doctor":
            return _doctor()
        if parsed.subcommand == "init-run":
            return _init_run(parsed)
        if parsed.subcommand == "transition":
            return _transition(parsed)
        if parsed.subcommand == "run-agent":
            return _run_agent(parsed)
        if parsed.subcommand == "probe-tool":
            return _probe_tool(parsed)
        if parsed.subcommand == "verify":
            return _verify(parsed)
    except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    parser.error(f"unknown subcommand: {parsed.subcommand}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
