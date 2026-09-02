from __future__ import annotations

import argparse
import json
import sys
import webbrowser
from pathlib import Path

from mailman.agents.codex_cli import REASONING_EFFORTS
from mailman.agents import (
    DEFAULT_MAX_TURNS,
    ClaudeCliAgent,
    CodexCliAgent,
    EngineeringAgent,
)
from mailman.agents.base import AgentRequest
from mailman.artifacts import (
    append_agent_execution,
    append_verification,
    create_run,
    default_data_root,
    load_run,
    write_run,
)
from mailman.doctor import run_checks
from mailman.environment import (
    environment_command,
    load_environment_record,
    load_plan,
    prepare_environment,
)
from mailman.executor import execute
from mailman.export import export_patch
from mailman.issue import (
    capture_issue_from_file,
    capture_issue_from_github,
    load_issue_record,
)
from mailman.targeting import assess_target
from mailman.submission import (
    TargetPolicy,
    prepare_submission,
    record_duplicate_search,
)
from mailman.knowledge.collect import collect_retrospective, write_retrospective
from mailman.knowledge.retrospective import RETROSPECTIVE_SECTIONS
from mailman.models import RunStatus
from mailman.orchestrator import orchestrate
from mailman.prior_art import collect_prior_art
from mailman.prompts import write_task_prompts
from mailman.toolchain import prepare_agent_prompt, probe_tool, toolchain_executable
from mailman.view import render_run, summarize_runs, write_transcript_logs
from mailman.review_page import write_run_page
from mailman.workspace import commit_is_ancestor, inspect_workspace, prepare_workspace


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
    # Required on purpose. A run that took a vendor default recorded `null` and
    # could not say afterwards what had actually done the work.
    init_run.add_argument(
        "--primary-model",
        required=True,
        help="model id for the primary engineer, recorded with the run",
    )
    init_run.add_argument(
        "--reviewer-model",
        required=True,
        help="model id for the reviewer, recorded with the run",
    )
    init_run.add_argument("--data-root", type=Path)

    fetch_issue = subparsers.add_parser(
        "fetch-issue", help="capture the run's issue text into the private run record"
    )
    fetch_issue.add_argument("run_id")
    fetch_issue.add_argument(
        "--from-file",
        type=Path,
        help="read the issue body from a local file instead of the GitHub CLI",
    )
    fetch_issue.add_argument("--title", help="issue title to record with --from-file")
    fetch_issue.add_argument("--executable", help="path to the GitHub CLI executable")
    fetch_issue.add_argument("--timeout", type=float, default=60)
    fetch_issue.add_argument("--data-root", type=Path)

    build_prompts = subparsers.add_parser(
        "build-prompts", help="turn the captured issue into primary and reviewer prompts"
    )
    build_prompts.add_argument("run_id")
    build_prompts.add_argument(
        "--verification",
        help="the verification command to quote in both prompts, as one string",
    )
    build_prompts.add_argument("--data-root", type=Path)

    prepare_environment_parser = subparsers.add_parser(
        "prepare-environment",
        help="install the target repository's dependencies outside its working tree",
    )
    prepare_environment_parser.add_argument("run_id")
    prepare_environment_parser.add_argument("--plan", required=True, type=Path)
    prepare_environment_parser.add_argument("--workspace", type=Path)
    prepare_environment_parser.add_argument("--timeout", type=float, default=1800)
    prepare_environment_parser.add_argument("--data-root", type=Path)

    export = subparsers.add_parser(
        "export-patch", help="write a reviewable patch package for a finished run"
    )
    export.add_argument("run_id")
    export.add_argument("--workspace", type=Path)
    export.add_argument("--output", type=Path)
    export.add_argument("--allow-unfinished", action="store_true")
    export.add_argument("--timeout", type=float, default=120)
    export.add_argument("--data-root", type=Path)

    submission = subparsers.add_parser(
        "prepare-submission",
        help="check a finished run against a target's contribution policy",
    )
    submission.add_argument("run_id")
    submission.add_argument(
        "--policy", type=Path, required=True, help="target policy JSON file"
    )
    submission.add_argument(
        "--diff",
        type=Path,
        help="unified diff to check, defaults to the run's exported changes.diff",
    )
    submission.add_argument("--output", type=Path)
    submission.add_argument("--branch")
    submission.add_argument("--title")
    submission.add_argument("--data-root", type=Path)

    prior_art = subparsers.add_parser(
        "prior-art",
        help="read earlier pull requests on this issue and put them in the prompts",
    )
    prior_art.add_argument("run_id")
    prior_art.add_argument(
        "--pull-request",
        type=int,
        action="append",
        dest="pull_requests",
        help="pull request number to read, repeatable; defaults to the "
        "duplicate search results",
    )
    prior_art.add_argument("--executable")
    prior_art.add_argument("--timeout", type=float, default=60)
    prior_art.add_argument("--data-root", type=Path)

    duplicate = subparsers.add_parser(
        "duplicate-search",
        help="search a target's pull requests and issues for the same change",
    )
    duplicate.add_argument("run_id")
    duplicate.add_argument("--query", required=True)
    duplicate.add_argument("--limit", type=int, default=30)
    duplicate.add_argument("--executable")
    duplicate.add_argument("--timeout", type=float, default=60)
    duplicate.add_argument("--data-root", type=Path)

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
    run_agent.add_argument(
        "--reasoning-effort",
        choices=REASONING_EFFORTS,
        help="how hard a Codex model is asked to think, recorded with the run",
    )
    run_agent.add_argument("--timeout", type=float, default=3600)
    run_agent.add_argument("--max-turns", type=int, default=DEFAULT_MAX_TURNS)
    run_agent.add_argument(
        "--verification",
        help="the verification command, as one string, so the agent is "
        "permitted to run it",
    )
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

    prepare = subparsers.add_parser(
        "prepare-workspace", help="clone a run repository at its exact base commit"
    )
    prepare.add_argument("run_id")
    prepare.add_argument("--timeout", type=float, default=600)
    prepare.add_argument("--data-root", type=Path)

    orchestrate_parser = subparsers.add_parser(
        "orchestrate",
        help="run the bounded primary, reviewer, and verification loop for a run",
    )
    orchestrate_parser.add_argument("run_id")
    orchestrate_parser.add_argument("--primary-prompt", type=Path)
    orchestrate_parser.add_argument("--reviewer-prompt", type=Path)
    orchestrate_parser.add_argument("--workspace", required=True, type=Path)
    orchestrate_parser.add_argument("--agent-timeout", type=float, default=3600)
    orchestrate_parser.add_argument("--verification-timeout", type=float, default=900)
    orchestrate_parser.add_argument("--max-turns", type=int, default=DEFAULT_MAX_TURNS)
    orchestrate_parser.add_argument("--max-revisions", type=int, default=1)
    orchestrate_parser.add_argument(
        "--acknowledge-prior-attempts",
        action="store_true",
        help="start even though closed pull requests already attempted this "
        "issue; an open one still refuses",
    )
    orchestrate_parser.add_argument("--data-root", type=Path)

    retrospective = subparsers.add_parser(
        "retrospective",
        help="draft a structured retrospective from a run's recorded evidence",
    )
    retrospective.add_argument("run_id")
    retrospective.add_argument(
        "--force",
        action="store_true",
        help="replace an existing retrospective, discarding hand-written answers",
    )
    retrospective.add_argument(
        "--skill", type=Path, help="skill file to read a version from"
    )
    retrospective.add_argument("--data-root", type=Path)

    check_target = subparsers.add_parser(
        "check-target",
        help="say whether an issue is already claimed, before a run starts",
    )
    check_target.add_argument("run_id")
    check_target.add_argument(
        "--acknowledge-prior-attempts",
        action="store_true",
        help="treat closed attempts as read rather than as a blocker",
    )
    check_target.add_argument("--data-root", type=Path)

    show = subparsers.add_parser(
        "show", help="render what a run's agents actually did, step by step"
    )
    show.add_argument("run_id", nargs="?", help="omit to list every recorded run")
    show.add_argument(
        "--full",
        action="store_true",
        help="include agent messages and command output in full, not one line each",
    )
    show.add_argument("--width", type=int, default=120)
    show.add_argument(
        "--no-reports", action="store_true", help="omit the agent report bodies"
    )
    show.add_argument(
        "--write-logs",
        action="store_true",
        help="write a .log transcript beside each stored agent execution",
    )
    show.add_argument("--data-root", type=Path)

    review = subparsers.add_parser(
        "review", help="write a single-page review of a run and open it"
    )
    review.add_argument("run_id")
    review.add_argument(
        "--output",
        type=Path,
        help="where to write the page, default review.html inside the run",
    )
    review.add_argument(
        "--no-open", action="store_true", help="write the page without opening it"
    )
    review.add_argument("--data-root", type=Path)

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


def _fetch_issue(arguments: argparse.Namespace) -> int:
    run, run_directory = load_run(arguments.run_id, arguments.data_root)
    if arguments.from_file is not None:
        record = capture_issue_from_file(
            run_directory,
            issue_url=run.issue,
            source_file=arguments.from_file,
            title=arguments.title,
        )
    else:
        record = capture_issue_from_github(
            run_directory,
            issue_url=run.issue,
            executable=arguments.executable,
            timeout_seconds=arguments.timeout,
        )
    summary = {
        "run_id": run.run_id,
        "source": record["source"],
        "success": record["success"],
        "title": record.get("title"),
        "body_characters": record.get("body_characters"),
        "issue_markdown": str(run_directory / "issue.md"),
    }
    if not record["success"]:
        summary["detail"] = record.get("detail")
    print(json.dumps(summary, indent=2))
    return 0 if record["success"] else 1


def _build_prompts(arguments: argparse.Namespace) -> int:
    run, run_directory = load_run(arguments.run_id, arguments.data_root)
    verification = (
        environment_command(run_directory, arguments.verification.split())
        if arguments.verification
        else None
    )
    primary_path, reviewer_path = write_task_prompts(
        run, run_directory, verification_command=verification
    )
    print(
        json.dumps(
            {
                "run_id": run.run_id,
                "primary_prompt": str(primary_path),
                "reviewer_prompt": str(reviewer_path),
            },
            indent=2,
        )
    )
    return 0


def _prepare_environment(arguments: argparse.Namespace) -> int:
    _, run_directory = load_run(arguments.run_id, arguments.data_root)
    workspace = arguments.workspace or (run_directory / "workspace")
    plan = load_plan(arguments.plan.resolve(strict=True))
    record = prepare_environment(
        run_directory,
        workspace=workspace,
        plan=plan,
        timeout_seconds=arguments.timeout,
        announce=lambda message: print(message, flush=True),
    )
    summary = {
        "steps": len(record["steps"]),
        "failed_step": next(
            (step["name"] for step in record["steps"] if not step["ok"]), None
        ),
        "workspace_clean": record["workspace_clean"],
        "registered": [entry["name"] for entry in record["registered"]],
        "success": record["success"],
        "record": str(run_directory / "environment.json"),
    }
    if not record["success"]:
        summary["detail"] = record.get("detail")
    print(json.dumps(summary, indent=2))
    return 0 if record["success"] else 1


def _prior_art(arguments: argparse.Namespace) -> int:
    run, run_directory = load_run(arguments.run_id, arguments.data_root)
    numbers = list(arguments.pull_requests or [])
    if not numbers:
        search_path = run_directory / "duplicate-search.json"
        if not search_path.is_file():
            raise ValueError(
                "no pull requests given and no duplicate search recorded. Run "
                "`mailman duplicate-search` first, or pass --pull-request."
            )
        search = json.loads(search_path.read_text(encoding="utf-8"))
        numbers = [
            match["number"]
            for match in search.get("matches", [])
            if match.get("pull_request") and isinstance(match.get("number"), int)
        ]
    if not numbers:
        raise ValueError("no earlier pull requests to read")
    record = collect_prior_art(
        run_directory,
        repository=run.repository,
        numbers=numbers,
        executable=arguments.executable,
        timeout_seconds=arguments.timeout,
    )
    print(
        json.dumps(
            {
                "run_id": run.run_id,
                "repository": record["repository"],
                "success": record["success"],
                "attempts": record.get("attempt_count", 0),
                "closed_unmerged": record.get("closed_unmerged", 0),
                "open": record.get("open", 0),
                "detail": record.get("detail"),
            },
            indent=2,
        )
    )
    if record["success"]:
        print(
            "Prior art is now part of both prompts. Rebuild them with "
            "`mailman build-prompts` if they already exist."
        )
    return 0 if record["success"] else 1


def _duplicate_search(arguments: argparse.Namespace) -> int:
    run, run_directory = load_run(arguments.run_id, arguments.data_root)
    record = record_duplicate_search(
        run_directory,
        repository=run.repository,
        query=arguments.query,
        executable=arguments.executable,
        timeout_seconds=arguments.timeout,
        limit=arguments.limit,
    )
    print(
        json.dumps(
            {
                "run_id": run.run_id,
                "repository": record["repository"],
                "query": record["query"],
                "success": record["success"],
                "matches": record.get("match_count", 0),
                "detail": record.get("detail"),
            },
            indent=2,
        )
    )
    if record["success"] and record["matches"]:
        for match in record["matches"]:
            kind = "PR" if match["pull_request"] else "issue"
            print(f"  {kind} #{match['number']} {match['state']}: {match['title']}")
    return 0 if record["success"] else 1


def _prepare_submission(arguments: argparse.Namespace) -> int:
    run, run_directory = load_run(arguments.run_id, arguments.data_root)
    policy = TargetPolicy.load(arguments.policy)
    diff_path = arguments.diff or (run_directory / "export" / "changes.diff")
    if not diff_path.is_file():
        raise ValueError(
            f"no diff at {diff_path}. Run export-patch first, or pass --diff."
        )
    issue_record = load_issue_record(run_directory)
    reference = (issue_record or {}).get("reference")
    number = reference.get("number") if isinstance(reference, dict) else None
    branch = arguments.branch or (
        f"mailman/issue-{number}" if isinstance(number, int) else f"mailman/run-{run.run_id}"
    )
    title = arguments.title or (issue_record or {}).get("title") or f"Address {run.issue}"
    record = prepare_submission(
        run,
        run_directory,
        diff=diff_path.read_text(encoding="utf-8", errors="replace"),
        policy=policy,
        destination=arguments.output or (run_directory / "submission"),
        branch=branch,
        title=title,
    )
    print(
        json.dumps(
            {
                "run_id": record["run_id"],
                "target": record["target"],
                "policy_stance": record["policy_stance"],
                "ready": record["ready"],
                "blocking_codes": record["blocking_codes"],
                "branch": record["branch"],
            },
            indent=2,
        )
    )
    return 0 if record["ready"] else 1


def _export_patch(arguments: argparse.Namespace) -> int:
    run, run_directory = load_run(arguments.run_id, arguments.data_root)
    workspace = arguments.workspace or (run_directory / "workspace")
    destination = arguments.output or (run_directory / "export")
    record = export_patch(
        run,
        run_directory,
        workspace=workspace,
        destination=destination,
        timeout_seconds=arguments.timeout,
        require_ready=not arguments.allow_unfinished,
    )
    print(
        json.dumps(
            {
                "run_id": record["run_id"],
                "status_at_export": record["status_at_export"],
                "destination": record["destination"],
                "branch": record["branch"],
                "changed_files": record["changed_files"],
            },
            indent=2,
        )
    )
    return 0


def _transition(arguments: argparse.Namespace) -> int:
    run, run_directory = load_run(arguments.run_id, arguments.data_root)
    run.transition(RunStatus(arguments.target), arguments.reason)
    write_run(run, run_directory)
    print(json.dumps({"run_id": run.run_id, "status": str(run.status)}, indent=2))
    return 0


def _make_agent(
    name: str,
    *,
    model: str | None,
    max_turns: int,
    executable: str | None = None,
    reasoning_effort: str | None = None,
) -> EngineeringAgent:
    normalized = name.strip().lower()
    if normalized == "codex":
        return CodexCliAgent(
            model=model,
            executable=executable or "codex",
            reasoning_effort=reasoning_effort,
        )
    if normalized == "claude":
        return ClaudeCliAgent(
            model=model, max_turns=max_turns, executable=executable or "claude"
        )
    raise ValueError(f"unsupported engineering agent: {name}")


def _pinned_agent_factory(run_directory: Path, max_turns: int):
    """Prefer a probed executable for an agent so a run cannot drift mid-flight."""

    def factory(name: str, model: str | None) -> EngineeringAgent:
        return _make_agent(
            name,
            model=model,
            max_turns=max_turns,
            executable=toolchain_executable(run_directory, name.strip().lower()),
        )

    return factory


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
        raise ValueError(
            "primary workspace must be clean before agent execution: "
            f"{workspace_state.describe_changes()}"
        )
    source_prompt = arguments.prompt.resolve(strict=True)
    if not source_prompt.is_file():
        raise ValueError("prompt must be a file")
    prompt_path = prepare_agent_prompt(
        run_directory, role=arguments.role, source_prompt=source_prompt
    )

    configured = run.primary if arguments.role == "primary" else run.reviewer
    model = arguments.model or configured.model
    agent = _make_agent(
        configured.agent,
        model=model,
        max_turns=arguments.max_turns,
        executable=toolchain_executable(run_directory, configured.agent.strip().lower()),
        reasoning_effort=arguments.reasoning_effort,
    )
    report_path = run_directory / f"{arguments.role}-report.md"
    verification = (
        tuple(environment_command(run_directory, arguments.verification.split()))
        if arguments.verification
        else ()
    )
    request = AgentRequest(
        run_id=run.run_id,
        role=arguments.role,
        prompt_path=prompt_path,
        workspace=workspace_state.path,
        report_path=report_path,
        timeout_seconds=arguments.timeout,
        on_event=lambda event: _emit(f"     {event.line()}"),
        verification_command=verification,
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
        "turn_budget": agent.turn_budget,
        "reasoning_effort": arguments.reasoning_effort,
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
        "stop_reason": result.stop_reason,
        "turn_budget": agent.turn_budget,
        "execution_record": str(record_path),
        "workflow_status": str(run.status),
        "status_changed": False,
    }
    print(json.dumps(summary, indent=2))
    if result.timed_out:
        return 124
    return result.exit_code or 0


def _default_prompt(run_directory: Path, given: Path | None, name: str) -> Path:
    """Fall back to the prompts `build-prompts` wrote for this run."""
    if given is not None:
        return given.resolve(strict=True)
    generated = run_directory / name
    if not generated.is_file():
        raise ValueError(
            f"no {name} for this run. Pass the prompt explicitly, or run "
            "`mailman build-prompts` first."
        )
    return generated.resolve(strict=True)


def _orchestrate(arguments: argparse.Namespace) -> int:
    run, run_directory = load_run(arguments.run_id, arguments.data_root)
    command = environment_command(run_directory, arguments.command)
    if not command:
        raise ValueError("a verification command is required after --")
    environment = load_environment_record(run_directory)
    if environment is not None and not environment.get("success"):
        raise ValueError(
            "environment preparation for this run did not succeed. Fix the plan "
            "and re-run `mailman prepare-environment` before orchestrating."
        )
    outcome = orchestrate(
        run=run,
        run_directory=run_directory,
        workspace=arguments.workspace,
        primary_prompt=_default_prompt(
            run_directory, arguments.primary_prompt, "primary-task.md"
        ),
        reviewer_prompt=_default_prompt(
            run_directory, arguments.reviewer_prompt, "reviewer-task.md"
        ),
        verification_command=command,
        agent_factory=_pinned_agent_factory(run_directory, arguments.max_turns),
        agent_timeout_seconds=arguments.agent_timeout,
        verification_timeout_seconds=arguments.verification_timeout,
        max_revisions=arguments.max_revisions,
        announce=_emit,
        acknowledge_prior_attempts=arguments.acknowledge_prior_attempts,
    )
    summary = {
        "run_id": outcome.run_id,
        "final_status": str(outcome.status),
        "ready_for_human_review": outcome.ready,
        "revisions_used": outcome.revisions_used,
        "review_cycles": outcome.review_cycles,
        "record": str(outcome.record_path),
    }
    print(json.dumps(summary, indent=2))
    return 0 if outcome.ready else 1


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


def _prepare_workspace(arguments: argparse.Namespace) -> int:
    run, run_directory = load_run(arguments.run_id, arguments.data_root)
    print(
        f"Preparing {run.repository} at {run.base_commit} with a "
        f"{arguments.timeout:g} second timeout per Git command.",
        flush=True,
    )
    record = prepare_workspace(
        repository=run.repository,
        base_commit=run.base_commit,
        run_directory=run_directory,
        timeout_seconds=arguments.timeout,
    )
    summary = {
        "path": record["path"],
        "head": record.get("head"),
        "clean": record.get("clean"),
        "reused": record["reused"],
        "success": record["success"],
        "record": str(run_directory / "workspace.json"),
    }
    if record.get("detail"):
        summary["detail"] = record["detail"]
    print(json.dumps(summary, indent=2))
    if record["success"]:
        return 0
    checkout = record.get("checkout")
    if isinstance(checkout, dict) and checkout.get("timed_out"):
        return 124
    clone = record.get("clone")
    if isinstance(clone, dict) and clone.get("timed_out"):
        return 124
    return 1


def _retrospective(arguments: argparse.Namespace) -> int:
    run, run_directory = load_run(arguments.run_id, arguments.data_root)
    retrospective = collect_retrospective(
        run, run_directory, skill_path=arguments.skill
    )
    json_path, markdown_path = write_retrospective(
        retrospective, run_directory, force=arguments.force
    )
    summary = {
        "run_id": run.run_id,
        "run_status": str(run.status),
        "record": str(json_path),
        "template": str(markdown_path),
        "seeded_observations": len(retrospective.observations),
        "channels": retrospective.channel_counts(),
        "categories": retrospective.category_counts(),
        "open_questions": len(RETROSPECTIVE_SECTIONS),
        "completed": False,
    }
    print(json.dumps(summary, indent=2))
    print(
        f"Answer the questions in {markdown_path}. Mailman seeded only what it "
        "observed.",
        flush=True,
    )
    return 0


def _emit(text: str) -> None:
    """Print a transcript without letting the console break the command.

    Agents write curly quotes and dashes that a legacy code page cannot
    encode, and `show | head` closes the pipe early. Neither is a reason to
    end with a traceback.

    The flush is the whole point of a live stream. Python only line-buffers a
    terminal, so without it a redirected or piped run says nothing for 8 KB.
    """
    try:
        print(text, flush=True)
    except UnicodeEncodeError:
        stream = getattr(sys.stdout, "buffer", None)
        if stream is None:
            return
        encoding = sys.stdout.encoding or "utf-8"
        stream.write(text.encode(encoding, errors="replace") + b"\n")
        stream.flush()
    except (BrokenPipeError, OSError):
        pass


def _check_target(arguments: argparse.Namespace) -> int:
    _, run_directory = load_run(arguments.run_id, arguments.data_root)
    assessment = assess_target(
        run_directory, acknowledged=arguments.acknowledge_prior_attempts
    )
    _emit(assessment.summary())
    if assessment.blocking:
        _emit("")
        _emit("refusing to start: " + "; ".join(assessment.blocking))
    return 0 if assessment.may_start else 1


def _show(arguments: argparse.Namespace) -> int:
    data_root = (arguments.data_root or default_data_root()).resolve()
    if not arguments.run_id:
        directories = sorted(path for path in data_root.glob("*") if path.is_dir())
        if not directories:
            print(f"no runs recorded under {data_root}")
            return 1
        _emit(summarize_runs(directories))
        return 0
    _, run_directory = load_run(arguments.run_id, arguments.data_root)
    if arguments.write_logs:
        written = write_transcript_logs(run_directory)
        if not written:
            print("no agent output was captured for this run", file=sys.stderr)
            return 1
        for path in written:
            print(f"wrote {path}")
        return 0
    _emit(
        render_run(
            run_directory,
            width=arguments.width,
            full=arguments.full,
            reports=not arguments.no_reports,
        )
    )
    return 0


def _review(arguments: argparse.Namespace) -> int:
    _, run_directory = load_run(arguments.run_id, arguments.data_root)
    destination = write_run_page(run_directory, arguments.output)
    print(json.dumps({"run_id": arguments.run_id, "page": str(destination)}, indent=2))
    if not arguments.no_open:
        # A page nobody opens is a file nobody reads. Failing to open one is not
        # a reason to fail the command, since the path is already printed.
        try:
            webbrowser.open(destination.resolve().as_uri())
        except (OSError, ValueError) as error:
            print(f"could not open a browser: {error}", file=sys.stderr)
    return 0


def _verify(arguments: argparse.Namespace) -> int:
    _, run_directory = load_run(arguments.run_id, arguments.data_root)
    command = environment_command(run_directory, arguments.command)
    if not command:
        raise ValueError("a command is required after --")
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
    if raw_arguments[:1] in (["verify"], ["orchestrate"]) and "--" in raw_arguments:
        delimiter = raw_arguments.index("--")
        verification_command = raw_arguments[delimiter + 1 :]
        raw_arguments = raw_arguments[:delimiter]

    parser = _build_parser()
    parsed = parser.parse_args(raw_arguments)
    if parsed.subcommand in ("verify", "orchestrate"):
        parsed.command = verification_command or []
    try:
        if parsed.subcommand == "doctor":
            return _doctor()
        if parsed.subcommand == "init-run":
            return _init_run(parsed)
        if parsed.subcommand == "fetch-issue":
            return _fetch_issue(parsed)
        if parsed.subcommand == "build-prompts":
            return _build_prompts(parsed)
        if parsed.subcommand == "prepare-environment":
            return _prepare_environment(parsed)
        if parsed.subcommand == "export-patch":
            return _export_patch(parsed)
        if parsed.subcommand == "prepare-submission":
            return _prepare_submission(parsed)
        if parsed.subcommand == "duplicate-search":
            return _duplicate_search(parsed)
        if parsed.subcommand == "prior-art":
            return _prior_art(parsed)
        if parsed.subcommand == "transition":
            return _transition(parsed)
        if parsed.subcommand == "run-agent":
            return _run_agent(parsed)
        if parsed.subcommand == "orchestrate":
            return _orchestrate(parsed)
        if parsed.subcommand == "probe-tool":
            return _probe_tool(parsed)
        if parsed.subcommand == "prepare-workspace":
            return _prepare_workspace(parsed)
        if parsed.subcommand == "retrospective":
            return _retrospective(parsed)
        if parsed.subcommand == "check-target":
            return _check_target(parsed)
        if parsed.subcommand == "show":
            return _show(parsed)
        if parsed.subcommand == "review":
            return _review(parsed)
        if parsed.subcommand == "verify":
            return _verify(parsed)
    except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    parser.error(f"unknown subcommand: {parsed.subcommand}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
