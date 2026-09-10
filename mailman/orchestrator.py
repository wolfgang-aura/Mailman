from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from mailman import health
from mailman.agents.base import AgentRequest, EngineeringAgent
from mailman.artifacts import append_agent_execution, append_verification, write_run
from mailman.completion import candidate_digest, git_bytes
from mailman.environment import environment_command
from mailman.executor import CommandResult, execute
from mailman.instructions import describe_instruction_sources
from mailman.limits import offload
from mailman.models import RunRecord, RunStatus, utc_now
from mailman.prompts import load_recorded_verification
from mailman.redaction import redact
from mailman.targeting import assess_target
from mailman.toolchain import prepare_agent_prompt, resolve_command
from mailman.transcript import (
    TranscriptEvent,
    count_commands,
    parse_stream,
    token_usage,
)
from mailman.workspace import inspect_workspace

VERDICT_APPROVE = "APPROVE"
VERDICT_REVISE = "REVISE"
DEFAULT_RUN_TIME_BUDGET_SECONDS = 7200
DEFAULT_AGENT_TIMEOUT_SECONDS = 600
DEFAULT_PRIMARY_COMMAND_BUDGET = 20
DEFAULT_REVIEWER_COMMAND_BUDGET = 10
DEFAULT_MAX_CHANGED_FILES = 8
DEFAULT_MAX_CHANGED_LINES = 500


class RunTimeBudgetExpired(RuntimeError):
    pass


_STOP_REASONS = {
    "error_max_turns": "it ran out of turns, so the work was cut off mid-task",
    "error_during_execution": "the CLI failed part-way through",
    "success": "the CLI reported success but wrote nothing",
}


def _describe_stop(reason: str | None, turn_budget: int | None) -> str:
    """Say why the agent stopped, and against what budget it stopped.

    A turn limit is only readable next to the limit, so the budget is named
    here rather than left for whoever opens the execution record.
    """
    if not reason:
        return ""
    described = _STOP_REASONS.get(reason, reason)
    if reason == "error_max_turns" and turn_budget:
        return f"{described} (budget: {turn_budget} turns)"
    return described


_VERDICT_PATTERN = re.compile(
    r"^[ \t>*-]*MAILMAN-VERDICT:[ \t]*(APPROVE|REVISE)[ \t]*$", re.MULTILINE
)

VERIFICATION_RAN = "RAN"
VERIFICATION_BLOCKED = "BLOCKED"

_VERIFICATION_PATTERN = re.compile(
    r"^[ \t>*-]*MAILMAN-VERIFICATION:[ \t]*(RAN|BLOCKED)[ \t]*$", re.MULTILINE
)

_VERDICT_CONTRACT = """
## Review boundary

Mailman ran the recorded verification after the primary stage and will run it
again after your review. Do not rerun that full gate merely to duplicate the
harness. Your job is to inspect the candidate's logic, scope, tests, and risks.
Run a smaller focused check only when it resolves a review question.

## Required verdict

Finish your reply with exactly one line of the form `MAILMAN-VERDICT: APPROVE`
or `MAILMAN-VERDICT: REVISE`, written on its own line and nowhere else.

Choose REVISE when the candidate needs a change, and list every required change
above the verdict line. Choose APPROVE only when no change is required. A
missing, repeated, or contradictory verdict stops the run for a human.

An APPROVE means no code-review change is required. The final independent
verification, not your process, decides whether the candidate advances.
"""

_EMPTY_CANDIDATE_NOTICE = """
## The workspace is unchanged

The primary agent finished its stage and left the workspace identical to the
base commit: no tracked file differs and nothing untracked was added. The
verification you are asked to run passes here for the same reason it passes on
the base commit, so it says nothing about this candidate.

Deciding whether changing nothing was right is this review. It is the correct
answer when the issue's acceptance criteria contradict an existing test, or
when the behaviour it asks for is already present; it is a failed stage when
the work was simply not done. Read the primary agent's report against the issue
and say which of the two happened.

Either way this run stops for a human rather than producing a submission, since
an empty candidate is nothing to publish. Your verdict decides what the record
says: APPROVE means changing nothing was correct and the issue needs an answer
rather than a patch, REVISE means the work is still owed, and the findings you
list are what the next attempt has to address.
"""

_REVIEWER_SANDBOX_NOTICE = """
## Where you can write

Your sandbox permits writes inside the workspace and inside the run's scratch
directory `{scratch}`, and the temporary-directory environment variables point
at the scratch directory, so a test suite that needs temp files has them.
Everything else is read-only.

You are still a reviewer. Do not edit, create, or delete anything in the
workspace: any change you leave behind stops the run, because the workspace
diff is the candidate and a reviewer's edit would enter the submission
silently.
"""

_REVISION_CONTRACT = """
## Reviewer findings to address

The reviewer read the candidate change and asked for revisions. Address every
finding below in the same workspace. This is the only revision in this run.

"""

_REPAIR_CONTRACT = """
## The verification you have to pass

Your change was checked with the run's own verification command and it failed.
The command and its output are below. Fix the failure in the same workspace.
This is the only revision in this run, and the same command decides the run.

"""

# Enough of a failure to act on, without pasting a whole suite into a prompt.
_REPAIR_OUTPUT_LINES = 120


def _describe_failure(result: CommandResult) -> str:
    """Render a failed verification for an agent that never saw it run."""
    tail = []
    for name in ("stdout", "stderr"):
        text = (getattr(result, name) or "").strip()
        if not text:
            continue
        lines = text.splitlines()
        clipped = lines[-_REPAIR_OUTPUT_LINES:]
        if len(clipped) < len(lines):
            clipped.insert(0, f"[earlier {len(lines) - len(clipped)} line(s) omitted]")
        tail.append(f"### {name}" + chr(10) + chr(10) + chr(10).join(clipped))
    outcome = (
        "timed out" if result.timed_out else f"exited with code {result.exit_code}"
    )
    header = "```" + chr(10) + " ".join(result.command) + chr(10) + "```"
    body = chr(10) + chr(10) + (chr(10) + chr(10)).join(tail) if tail else ""
    return f"The command {outcome}." + chr(10) + chr(10) + header + body


def parse_verification_claim(report_text: str | None) -> str | None:
    """Return the reviewer's own answer to whether it ran the gate.

    Counting commands cannot answer this. In run 20260902T144544Z-5dbf69 the
    Codex reviewer ran three: two file reads that succeeded and one pytest that
    exited 1. Pairing a command to its result does not answer it either, since
    Claude issues tool calls in parallel and the results arrive out of order.
    The reviewer knows, and on the run that started
    https://github.com/wolfgang-aura/Mailman/issues/20 it said so in prose that
    nothing could act on. This makes the same sentence machine-readable, and the
    command tally still catches a reviewer that claims RAN having run nothing.
    """
    if not report_text:
        return None
    claims = set(_VERIFICATION_PATTERN.findall(report_text))
    if len(claims) != 1:
        return None
    return claims.pop()


def parse_verdict(report_text: str | None) -> str | None:
    """Return the reviewer verdict, or None when it is missing or contradictory."""
    if not report_text:
        return None
    verdicts = set(_VERDICT_PATTERN.findall(report_text))
    if len(verdicts) != 1:
        return None
    return verdicts.pop()


AgentFactory = Callable[[str, str | None], EngineeringAgent]


@dataclass(frozen=True)
class OrchestrationStep:
    name: str
    at: str
    ok: bool
    detail: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "at": self.at,
            "ok": self.ok,
            "detail": self.detail,
            "data": self.data,
        }


@dataclass
class OrchestrationOutcome:
    run_id: str
    status: RunStatus
    steps: list[OrchestrationStep]
    revisions_used: int
    review_cycles: int
    max_review_cycles: int
    time_budget_seconds: float
    deadline_at: str
    budget_override_reason: str | None
    record_path: Path

    @property
    def ready(self) -> bool:
        return self.status is RunStatus.READY_FOR_HUMAN_REVIEW

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "run_id": self.run_id,
            "final_status": str(self.status),
            "ready_for_human_review": self.ready,
            "revisions_used": self.revisions_used,
            "review_cycles": self.review_cycles,
            "max_review_cycles": self.max_review_cycles,
            "time_budget_seconds": self.time_budget_seconds,
            "deadline_at": self.deadline_at,
            "budget_override_reason": self.budget_override_reason,
            "steps": [step.to_dict() for step in self.steps],
        }


class _Orchestration:
    def __init__(
        self,
        *,
        run: RunRecord,
        run_directory: Path,
        workspace: Path,
        primary_prompt: Path,
        reviewer_prompt: Path,
        verification_command: Sequence[str],
        agent_factory: AgentFactory,
        agent_timeout_seconds: float,
        primary_command_budget: int,
        reviewer_command_budget: int,
        verification_timeout_seconds: float,
        max_revisions: int,
        max_review_cycles: int,
        run_time_budget_seconds: float,
        deadline_at: str | None,
        time_budget_name: str,
        budget_override_reason: str | None,
        max_changed_files: int,
        max_changed_lines: int,
        announce: Callable[[str], None],
        check_target: bool = True,
        acknowledge_prior_attempts: bool = False,
        acknowledge_claims: bool = False,
    ) -> None:
        self.run = run
        self.run_directory = run_directory
        self.workspace = workspace
        self.primary_prompt = primary_prompt
        self.reviewer_prompt = reviewer_prompt
        self.verification_command = resolve_command(run_directory, verification_command)
        self.agent_factory = agent_factory
        self.agent_timeout_seconds = agent_timeout_seconds
        self.command_budgets = {
            "primary": primary_command_budget,
            "reviewer": reviewer_command_budget,
        }
        self.verification_timeout_seconds = verification_timeout_seconds
        self.max_revisions = max_revisions
        self.max_review_cycles = max_review_cycles
        self.run_time_budget_seconds = run_time_budget_seconds
        self.time_budget_name = time_budget_name
        self.budget_override_reason = (
            budget_override_reason.strip() if budget_override_reason else None
        )
        created = datetime.fromisoformat(run.created_at)
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        if deadline_at:
            self.deadline = datetime.fromisoformat(deadline_at)
            if self.deadline.tzinfo is None:
                self.deadline = self.deadline.replace(tzinfo=UTC)
        else:
            self.deadline = created + timedelta(seconds=run_time_budget_seconds)
        self.max_changed_files = max_changed_files
        self.max_changed_lines = max_changed_lines
        self.announce = announce
        self.check_target = check_target
        self.acknowledge_prior_attempts = acknowledge_prior_attempts
        self.acknowledge_claims = acknowledge_claims
        #: How many commands each role ran in its most recent stage. A reviewer
        #: that ran none has read the candidate, not checked it.
        self.commands_run: dict[str, int] = {}
        self.steps: list[OrchestrationStep] = []
        self.revisions_used = 0
        #: Whether the most recent primary stage changed anything at all.
        #: A stage that changed nothing passes verification exactly as the
        #: base commit does, so the flag, not the green check, is what says
        #: whether there is a candidate. See
        #: https://github.com/wolfgang-aura/Mailman/issues/10.
        self.workspace_changed = True

    # Recording helpers -------------------------------------------------

    def _step(
        self, name: str, *, ok: bool, detail: str, data: dict[str, Any] | None = None
    ) -> None:
        self.announce(f"{'ok  ' if ok else 'stop'} {name}: {detail}")
        self.steps.append(
            OrchestrationStep(
                name=name, at=utc_now(), ok=ok, detail=detail, data=data or {}
            )
        )

    def _target_data(self, assessment: Any) -> dict[str, Any]:
        """Keep the target verdict inline and the intel in its own file.

        A full assessment reached 21,606,896 bytes, and `resume-review` copies
        every prior step forward, so four resumes carried four copies. See
        https://github.com/wolfgang-aura/Mailman/issues/66.
        """
        return offload(
            assessment.to_dict(),
            self.run_directory / "target-assessment.json",
            keep=(
                "searched",
                "open_attempts",
                "merged_attempts",
                "superseded_attempts",
            ),
        )

    def _transition(self, target: RunStatus, reason: str) -> None:
        self.run.transition(target, reason)
        write_run(self.run, self.run_directory)
        self._step("transition", ok=True, detail=f"{target}: {reason}")

    def _block(self, reason: str) -> None:
        # A resume blocks the run before the loop starts, so a budget refusal
        # inside the loop would otherwise be a BLOCKED -> BLOCKED transition.
        # The reason is what a reader needs either way; record it and keep the
        # run where it already is.
        if self.run.status is not RunStatus.BLOCKED:
            self.run.transition(RunStatus.BLOCKED, reason)
            write_run(self.run, self.run_directory)
        else:
            self.run.history.append(
                {
                    "at": utc_now(),
                    "from": str(RunStatus.BLOCKED),
                    "to": str(RunStatus.BLOCKED),
                    "reason": reason,
                }
            )
            write_run(self.run, self.run_directory)
        self._step("blocked", ok=False, detail=reason)

    # Bounded stages ----------------------------------------------------

    def _remaining_timeout(self, requested: float, stage: str) -> float:
        remaining = (self.deadline - datetime.now(UTC)).total_seconds()
        if remaining <= 0:
            raise RunTimeBudgetExpired(
                f"{self.time_budget_name} time budget spent before {stage}: "
                f"deadline {self.deadline.isoformat()}"
            )
        return min(requested, remaining)

    def _previous_session(self, role: str) -> str | None:
        directory = self.run_directory / "agent-executions"
        if not directory.is_dir():
            return None
        for path in reversed(sorted(directory.glob("*.json"))):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if record.get("role") == role and record.get("session_id"):
                return str(record["session_id"])
        return None

    def _role_usage(self, role: str) -> dict[str, int]:
        totals = {
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "output_tokens": 0,
        }
        directory = self.run_directory / "agent-executions"
        if not directory.is_dir():
            return totals
        for path in sorted(directory.glob("*.json")):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if record.get("role") != role:
                continue
            usage = record.get("usage") or {}
            for key in totals:
                value = usage.get(key, 0)
                if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                    totals[key] += value
        return totals

    def _run_agent(self, role: str, source_prompt: Path) -> tuple[bool, str | None]:
        configured = self.run.primary if role == "primary" else self.run.reviewer
        command_budget = self.command_budgets[role]
        agent = self.agent_factory(configured.agent, configured.model)
        prior_usage = self._role_usage(role)
        usage_budget = agent.token_budget
        if usage_budget is not None and prior_usage["input_tokens"] >= usage_budget:
            detail = (
                f"{agent.name} {role} input budget spent: "
                f"{prior_usage['input_tokens']} of {usage_budget} tokens recorded"
            )
            self._step(
                f"agent:{role}",
                ok=False,
                detail=detail,
                data={
                    "agent": agent.name,
                    "token_budget": usage_budget,
                    "role_usage": prior_usage,
                    "usage_budget_exceeded": True,
                },
            )
            return False, None
        prompt_path = prepare_agent_prompt(
            self.run_directory, role=role, source_prompt=source_prompt
        )
        report_path = self.run_directory / f"{role}-report.md"
        # The reviewer needs a writable temp directory or every suite that
        # needs one fails inside the review for reasons that have nothing to
        # do with the candidate (#29). The scratch lives beside the run
        # record, outside the workspace, so using it cannot dirty the
        # candidate; the adapter makes it writable to the agent's sandbox.
        scratch_directory: Path | None = None
        if role == "reviewer":
            scratch_directory = self.run_directory / "scratch"
            scratch_directory.mkdir(parents=True, exist_ok=True)
        timeout = self._remaining_timeout(self.agent_timeout_seconds, f"agent:{role}")
        session_id = self._previous_session(role)
        self.announce(
            f"run  {role}: {agent.name} with a "
            f"{timeout:g} second timeout and {command_budget} command budget"
            + (f", resuming session {session_id}." if session_id else ".")
        )
        log_path = self.run_directory / "agent-executions" / f"{role}-live.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as log:

            def watch(event: TranscriptEvent) -> None:
                line = redact(event.line())
                log.write(line + "\n")
                log.flush()
                self.announce(f"     {line}")

            result = agent.run(
                AgentRequest(
                    run_id=self.run.run_id,
                    role=role,
                    prompt_path=prompt_path,
                    workspace=self.workspace,
                    report_path=report_path,
                    timeout_seconds=timeout,
                    on_event=watch,
                    verification_command=tuple(self.verification_command),
                    scratch_directory=scratch_directory,
                    session_id=session_id,
                    command_budget=command_budget,
                )
            )
        report_text = (
            result.report_path.read_text(encoding="utf-8", errors="replace")
            if result.report_present
            else None
        )
        # A reviewer that executed nothing has read the code, not checked it.
        # Codex under a read-only sandbox has done exactly that on a live run,
        # so the count belongs in the record, not only on the review page.
        tally = count_commands(
            parse_stream(result.command_result.stdout.splitlines(), agent.name)
        )
        reported_usage = token_usage(result.command_result.stdout, agent.name)
        usage_accounting_missing = bool(
            agent.name == "codex"
            and usage_budget is not None
            and reported_usage is None
            and result.command_result.stopped_reason is None
        )
        command_budget_exceeded = bool(
            result.command_result.stopped_reason
            and result.command_result.stopped_reason.startswith(
                "command budget exceeded:"
            )
        )
        execution_usage = reported_usage or {
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "output_tokens": 0,
        }
        role_usage = {
            key: prior_usage[key] + execution_usage[key] for key in prior_usage
        }
        usage_budget_exceeded = bool(
            usage_budget is not None and role_usage["input_tokens"] > usage_budget
        )
        record_path = append_agent_execution(
            self.run_directory,
            role,
            {
                "agent": agent.name,
                "model": configured.model,
                "role": role,
                "report_path": str(result.report_path),
                "report_present": result.report_present,
                "report": report_text,
                "prompt_path": str(prompt_path),
                "turn_budget": agent.turn_budget,
                "token_budget": agent.token_budget,
                "command_budget": command_budget,
                "command_budget_exceeded": command_budget_exceeded,
                "usage": execution_usage,
                "role_usage": role_usage,
                "usage_budget_exceeded": usage_budget_exceeded,
                "usage_accounting_missing": usage_accounting_missing,
                "session_id": result.session_id,
                "commands_run": tally["commands"],
                "commands_refused_or_failed": tally["refused_or_failed"],
                "model_reported_by_cli": result.observed_model or "not reported",
                "instruction_sources": describe_instruction_sources(agent.name),
                "process": result.command_result.to_dict(),
                "workflow_status_after_run": str(self.run.status),
            },
        )
        ok = (
            not result.timed_out
            and result.exit_code == 0
            and result.report_present
            and not usage_budget_exceeded
            and not usage_accounting_missing
        )
        stop_reason = _describe_stop(result.stop_reason, agent.turn_budget)
        if not ok and not command_budget_exceeded:
            state = health.classify(
                result.stop_reason,
                stop_reason,
                result.command_result.stderr,
                result.command_result.stdout[-20_000:],
            )
            if state:
                health.record(
                    self.run_directory,
                    state=state,
                    stage=f"agent:{role}",
                    resume_command=f"mailman resume-review {self.run.run_id}",
                    detail=f"{agent.name} stopped for a {state} reason, not a candidate defect",
                )
        else:
            health.clear(self.run_directory)
        if command_budget_exceeded:
            detail = result.command_result.stopped_reason or "command budget exceeded"
        elif usage_accounting_missing:
            detail = (
                f"{agent.name} returned no turn usage, so Mailman cannot enforce "
                f"the {role} input budget"
            )
        elif usage_budget_exceeded:
            detail = (
                f"{agent.name} exceeded the {role} input budget: "
                f"{role_usage['input_tokens']} of {usage_budget} tokens recorded"
            )
        elif result.timed_out:
            detail = f"{agent.name} timed out"
        elif not result.report_present:
            detail = f"{agent.name} produced no report"
            if stop_reason:
                detail = f"{detail}: {stop_reason}"
        else:
            detail = f"{agent.name} finished with exit code {result.exit_code}"
        self._step(
            f"agent:{role}",
            ok=ok,
            detail=detail,
            data={
                "agent": agent.name,
                "exit_code": result.exit_code,
                "timed_out": result.timed_out,
                "report_present": result.report_present,
                "stop_reason": result.stop_reason,
                "turn_budget": agent.turn_budget,
                "command_budget": command_budget,
                "command_budget_exceeded": command_budget_exceeded,
                "token_budget": usage_budget,
                "usage": execution_usage,
                "role_usage": role_usage,
                "usage_budget_exceeded": usage_budget_exceeded,
                "usage_accounting_missing": usage_accounting_missing,
                "commands_run": tally["commands"],
                "commands_refused_or_failed": tally["refused_or_failed"],
                "execution_record": str(record_path),
                "live_log": str(log_path),
            },
        )
        self.commands_run[role] = tally["commands"]
        return ok, report_text

    def _verify(self, stage: str) -> tuple[bool, CommandResult]:
        guard_workspace = stage in ("baseline", "final")
        before = (
            candidate_digest(self.workspace, self.run.base_commit)
            if guard_workspace
            else None
        )
        self.announce(
            f"run  verification ({stage}): {' '.join(self.verification_command)}"
        )
        result = execute(
            self.verification_command,
            working_directory=self.workspace,
            timeout_seconds=self._remaining_timeout(
                self.verification_timeout_seconds, f"verification:{stage}"
            ),
        )
        command_number = append_verification(self.run_directory, result.to_dict())
        ok = not result.timed_out and result.exit_code == 0
        after = (
            candidate_digest(self.workspace, self.run.base_commit)
            if guard_workspace
            else None
        )
        if guard_workspace and before != after:
            ok = False
        detail = (
            "verification timed out"
            if result.timed_out
            else f"verification exited with code {result.exit_code}"
        )
        self._step(
            f"verification:{stage}",
            ok=ok,
            detail=detail,
            data={
                "record": command_number,
                "exit_code": result.exit_code,
                "timed_out": result.timed_out,
                "candidate_digest": after,
                "workspace": str(self.workspace),
                "candidate_unchanged": before == after,
            },
        )
        return ok, result

    def _record_workspace_change(self, stage: str) -> bool:
        """Record whether the primary stage actually changed anything.

        A verification pass proves the tests pass, not that work happened: a
        stage where the agent changed nothing passes verification exactly as a
        base commit does. Recording it keeps that distinction in the evidence
        instead of leaving a reader to infer progress from a green check.
        """
        state = inspect_workspace(self.workspace)
        changed = bool(
            git_bytes(
                self.workspace, "diff", self.run.base_commit, "--name-only"
            ).strip()
            or git_bytes(
                self.workspace, "ls-files", "--others", "--exclude-standard"
            ).strip()
        )
        self.workspace_changed = changed
        self._step(
            f"workspace-change:{stage}",
            ok=changed,
            detail=(
                f"{len(state.changes)} changed path(s): {state.describe_changes()}"
                if changed
                else "the agent left the workspace identical to the base commit"
            ),
            data={"changed": changed, "changes": list(state.changes)},
        )
        return changed

    def _candidate_size(self) -> tuple[int, int]:
        numstat = git_bytes(
            self.workspace, "diff", "--numstat", self.run.base_commit, "--"
        ).decode("utf-8", errors="replace")
        paths: set[str] = set()
        changed_lines = 0
        for line in numstat.splitlines():
            parts = line.split("\t", 2)
            if len(parts) != 3:
                continue
            added, removed, path = parts
            paths.add(path)
            if added.isdigit():
                changed_lines += int(added)
            if removed.isdigit():
                changed_lines += int(removed)
        untracked = git_bytes(
            self.workspace, "ls-files", "--others", "--exclude-standard"
        ).decode("utf-8", errors="replace")
        for path in filter(None, untracked.splitlines()):
            paths.add(path)
            try:
                changed_lines += len((self.workspace / path).read_bytes().splitlines())
            except OSError:
                changed_lines += self.max_changed_lines + 1
        return len(paths), changed_lines

    def _check_candidate_scope(self, stage: str) -> bool:
        files, lines = self._candidate_size()
        ok = files <= self.max_changed_files and lines <= self.max_changed_lines
        self._step(
            f"candidate-scope:{stage}",
            ok=ok,
            detail=(
                f"{files} changed file(s), {lines} changed line(s); limits are "
                f"{self.max_changed_files} files and {self.max_changed_lines} lines"
            ),
            data={"changed_files": files, "changed_lines": lines},
        )
        if not ok:
            self._block(
                "candidate exceeded the small-patch budget; replace it instead "
                "of spending more agent cycles"
            )
        return ok

    def _record_reviewer_change(
        self, before: tuple[str, ...], before_digest: str
    ) -> bool:
        """Record whether the reviewer changed the workspace it was only reading.

        Compare candidate content, including paths already changed by the primary.
        A reviewer edit returns the run to coordinator repair.
        """
        state = inspect_workspace(self.workspace)
        introduced = tuple(line for line in state.changes if line not in before)
        changed = (
            candidate_digest(self.workspace, self.run.base_commit) != before_digest
        )
        self._step(
            "workspace-change:reviewer",
            ok=not changed,
            detail=(
                f"the reviewer changed candidate bytes; {len(introduced)} new status path(s): "
                + ", ".join(introduced[:10])
                if changed
                else "the reviewer left the workspace as the primary stage left it"
            ),
            data={"changed": changed, "changes": list(introduced)},
        )
        return changed

    def _write_derived_prompt(self, name: str, text: str) -> Path:
        destination = self.run_directory / name
        destination.write_text(text, encoding="utf-8")
        return destination

    def _review_prompt(self) -> Path:
        """Build the reviewer's prompt for the stage it is about to read.

        An empty candidate still reaches the reviewer, because on the run
        that opened issue #10 the reviewer's diagnosis of why nothing had
        changed was worth more than a status line. What it gets now is the
        fact up front, so it spends its turn on that question instead of
        rediscovering it. The prompt is rebuilt for every review cycle,
        because a revision stage can leave a different state behind.
        """
        resumed = (
            self.run.reviewer.agent == "codex"
            and self._previous_session("reviewer") is not None
        )
        source = (
            "# Continued review\n\nThe primary changed the candidate in the "
            "workspace. Review the new diff against the same issue and base commit."
            if resumed
            else self.reviewer_prompt.read_text(encoding="utf-8").rstrip()
        )
        notice = "" if self.workspace_changed else _EMPTY_CANDIDATE_NOTICE
        sandbox = _REVIEWER_SANDBOX_NOTICE.format(
            scratch=self.run_directory / "scratch"
        )
        changes = inspect_workspace(self.workspace).changes
        stat = (
            git_bytes(self.workspace, "diff", "--stat", self.run.base_commit, "--")
            .decode("utf-8", errors="replace")
            .strip()
        )
        primary_report = self.run_directory / "primary-report.md"
        report = (
            primary_report.read_text(encoding="utf-8", errors="replace")[-4_000:]
            if primary_report.is_file()
            else "No primary report was recorded."
        )
        candidate = (
            "\n\n## Candidate briefing\n\n"
            f"Changed paths: {', '.join(changes) or 'none'}\n\n"
            f"Diff stat:\n```text\n{stat or 'empty'}\n```\n\n"
            f"Primary report (tail):\n\n{report.strip()}\n"
        )
        return self._write_derived_prompt(
            "review-input.md",
            f"{source}{candidate}\n{notice}{sandbox}{_VERDICT_CONTRACT}",
        )

    def _revision_prompt(self, findings: str) -> Path:
        resumed = (
            self.run.primary.agent == "codex"
            and self._previous_session("primary") is not None
        )
        source = (
            "# Continued primary task\n\nContinue in the same workspace and "
            "session. Do not repeat repository discovery or the baseline."
            if resumed
            else self.primary_prompt.read_text(encoding="utf-8").rstrip()
        )
        return self._write_derived_prompt(
            "revision-input.md",
            f"{source}\n{_REVISION_CONTRACT}{findings.strip()}\n",
        )

    def _repair_prompt(self, result: CommandResult) -> Path:
        """Hand the primary agent the verification output it has to satisfy.

        A failing gate is a more objective finding than a reviewer opinion, so
        it earns the same one revision. The agent never saw this output before.
        """
        resumed = (
            self.run.primary.agent == "codex"
            and self._previous_session("primary") is not None
        )
        source = (
            "# Continued primary task\n\nThe harness checked your current "
            "workspace. Fix only the failure below; do not repeat discovery."
            if resumed
            else self.primary_prompt.read_text(encoding="utf-8").rstrip()
        )
        body = _describe_failure(result)
        return self._write_derived_prompt(
            "repair-input.md",
            f"{source}\n{_REPAIR_CONTRACT}{body}\n",
        )

    # Entry point -------------------------------------------------------

    def execute(self, *, resume_review: bool = False) -> OrchestrationOutcome:
        # BLOCKED is resumable because the target gate below is what blocks a
        # run before the primary ever starts, and its preconditions are
        # satisfied by commands the operator runs afterwards. Refusing to
        # restart would make a missing `target-intel` record cost a fresh
        # clone. The gate runs again a few lines down, so a precondition that
        # is still unsatisfied blocks the run a second time rather than
        # slipping past.
        if resume_review:
            if self.run.status not in (
                RunStatus.BLOCKED,
                RunStatus.ENGINEERING_COMPLETE,
                RunStatus.READY_FOR_HUMAN_REVIEW,
                RunStatus.MAINTAINER_CHANGES_REQUESTED,
            ):
                raise ValueError(
                    "resume-review requires a blocked or completed engineering run"
                )
            previous = self.run_directory / "orchestration.json"
            if not previous.is_file():
                raise ValueError("no prior orchestration to resume")
            old = json.loads(previous.read_text(encoding="utf-8"))
            if any(
                (step.get("data") or {}).get("usage_budget_exceeded")
                or (step.get("data") or {}).get("usage_accounting_missing")
                or (step.get("data") or {}).get("command_budget_exceeded")
                for step in old.get("steps", [])
            ):
                raise ValueError(
                    "agent usage budget already spent or command budget spent; "
                    "this candidate cannot resume"
                )
            previous_budget = float(
                old.get("time_budget_seconds", DEFAULT_RUN_TIME_BUDGET_SECONDS)
            )
            if (
                self.run_time_budget_seconds > previous_budget
                and not self.budget_override_reason
            ):
                raise ValueError(
                    "increasing a run time budget requires --time-budget-override-reason"
                )
            if not any(
                step.get("name") == "agent:primary" for step in old.get("steps", [])
            ):
                raise ValueError(
                    "primary never ran; use orchestrate after fixing preconditions"
                )
            self.revisions_used = int(old.get("revisions_used", 0))
            self.steps = [OrchestrationStep(**step) for step in old.get("steps", [])]
            archive = self.run_directory / "orchestration-history"
            archive.mkdir(exist_ok=True)
            (archive / f"{len(list(archive.glob('*.json'))) + 1:04d}.json").write_bytes(
                previous.read_bytes()
            )
            if self.run.status is not RunStatus.BLOCKED:
                self._block("candidate requires a fresh independent review")
        if self.run.status not in (RunStatus.INITIALIZED, RunStatus.BLOCKED):
            raise ValueError(
                "orchestration requires an INITIALIZED or BLOCKED run, "
                f"found {self.run.status}"
            )
        if self.max_revisions < 0:
            raise ValueError("max_revisions cannot be negative")
        if self.max_review_cycles < 1:
            raise ValueError("max_review_cycles must be at least 1")
        if self.run_time_budget_seconds <= 0:
            raise ValueError("run_time_budget_seconds must be positive")
        if any(budget <= 0 for budget in self.command_budgets.values()):
            raise ValueError("agent command budgets must be positive")
        if (
            self.run_time_budget_seconds > DEFAULT_RUN_TIME_BUDGET_SECONDS
            and not self.budget_override_reason
        ):
            raise ValueError(
                "a run time budget above two hours requires an override reason"
            )
        if self.max_changed_files < 1 or self.max_changed_lines < 1:
            raise ValueError("candidate scope limits must be positive")
        if not self.verification_command:
            raise ValueError("a verification command is required")
        self._check_verification_agreement()

        try:
            self._remaining_timeout(1, "target checks")
        except RunTimeBudgetExpired as error:
            self._block(str(error))
            return self._outcome()

        if self.budget_override_reason:
            self._step(
                "time-budget-override",
                ok=True,
                detail=self.budget_override_reason,
                data={"time_budget_seconds": self.run_time_budget_seconds},
            )

        if self.check_target:
            assessment = assess_target(
                self.run_directory,
                acknowledged=self.acknowledge_prior_attempts,
                acknowledged_claims=self.acknowledge_claims,
            )
            self.announce(assessment.summary())
            if not assessment.may_start:
                self._step(
                    "target",
                    ok=False,
                    detail="; ".join(assessment.blocking),
                    data=self._target_data(assessment),
                )
                self._block("refused to start: " + "; ".join(assessment.blocking))
                return self._outcome()
            self._step(
                "target",
                ok=True,
                detail=(
                    f"{len(assessment.closed_attempts)} closed attempt(s), "
                    "no open or merged pull request"
                ),
                data=self._target_data(assessment),
            )

        state = inspect_workspace(self.workspace)
        if not resume_review and state.head != self.run.base_commit:
            raise ValueError(
                f"workspace HEAD {state.head} does not match base commit "
                f"{self.run.base_commit}"
            )
        if not resume_review and not state.clean:
            raise ValueError(
                "primary workspace must be clean before orchestration: "
                f"{state.describe_changes()}"
            )
        self.workspace = state.path
        self._step(
            "workspace",
            ok=True,
            detail=f"clean workspace at {state.head}",
            data={"path": str(state.path), "head": state.head},
        )

        if resume_review:
            from mailman.workspace import commit_is_ancestor

            if not commit_is_ancestor(self.workspace, self.run.base_commit):
                raise ValueError("candidate does not descend from the run base")
            self._record_workspace_change("resume")
        else:
            baseline_ok, _ = self._verify("baseline")
            if not baseline_ok:
                self._block(
                    "baseline verification failed before the primary agent started"
                )
                return self._outcome()
            self._transition(RunStatus.PRIMARY_RUNNING, "primary agent starting")
        try:
            return self._loop(start_primary=not resume_review)
        except RunTimeBudgetExpired as error:
            self._block(str(error))
            return self._outcome()
        except (OSError, ValueError) as error:
            # A started run must never be left claiming it is still in flight.
            self._block(f"orchestration stopped on an unexpected error: {error}")
            return self._outcome()

    def _check_verification_agreement(self) -> None:
        """Require identical resolved verification executables and arguments.

        Legacy prompts without a recorded command retain their existing behavior.
        New PRHunt runs require a recorded argv before engineering starts.
        """
        recorded = load_recorded_verification(self.run_directory)
        if not recorded:
            return
        recorded = resolve_command(
            self.run_directory, environment_command(self.run_directory, recorded)
        )
        if recorded == list(self.verification_command):
            return
        raise ValueError(
            "the prompts and the gate name different verification programs or arguments: "
            f"the prompts quote `{' '.join(recorded)}` and orchestrate was "
            f"given `{' '.join(self.verification_command)}`. Re-run "
            "`mailman build-prompts --verification` with the command the gate "
            "runs, or pass the command the prompts quote."
        )

    def _loop(self, *, start_primary: bool = True) -> OrchestrationOutcome:
        if start_primary and not self._finish_primary_stage(
            self.primary_prompt, "primary"
        ):
            return self._outcome()

        while True:
            # `review_cycles` lives on the run record, so it survives every
            # `resume-review`. A per-call limit does not: one recovered run
            # reached five reviewer passes against a one-revision budget, each
            # a full reviewer stage on the shared model allowance. See
            # https://github.com/wolfgang-aura/Mailman/issues/65.
            if self.run.review_cycles >= self.max_review_cycles:
                self._block(
                    f"review budget spent: {self.run.review_cycles} of "
                    f"{self.max_review_cycles} cycles used across this run. "
                    "Raise --max-review-cycles deliberately, or replace the "
                    "candidate."
                )
                return self._outcome()
            review_prompt = self._review_prompt()
            self._transition(RunStatus.REVIEW_PENDING, "reviewer reading the candidate")
            reviewer_before = inspect_workspace(self.workspace).changes
            reviewer_digest = candidate_digest(self.workspace, self.run.base_commit)
            reviewer_ok, review_report = self._run_agent("reviewer", review_prompt)
            self.run.review_cycles += 1
            write_run(self.run, self.run_directory)
            if not reviewer_ok:
                self._block("reviewer did not complete a readable review")
                return self._outcome()
            if self._record_reviewer_change(reviewer_before, reviewer_digest):
                self._block(
                    "the reviewer changed the workspace it was only supposed "
                    "to read. The workspace diff is the candidate, so a "
                    "reviewer's edit would enter the submission silently; the "
                    "changed paths are in the record and a human decides what "
                    "to keep."
                )
                return self._outcome()

            verdict = parse_verdict(review_report)
            self._step(
                "verdict",
                ok=verdict is not None,
                detail=f"reviewer verdict {verdict or 'missing or contradictory'}",
                data={"verdict": verdict},
            )
            if verdict is None:
                self._block("reviewer verdict was missing or contradictory")
                return self._outcome()
            if verdict == VERDICT_APPROVE:
                claim = parse_verification_claim(review_report)
                reviewer_commands = self.commands_run.get("reviewer", 0)
                self._step(
                    "reviewer-execution",
                    ok=True,
                    detail=(
                        f"reviewer completed the code review and ran "
                        f"{reviewer_commands} optional command(s)"
                    ),
                    data={
                        "verification_claim": claim,
                        "commands_run": reviewer_commands,
                    },
                )
                break
            if self.revisions_used >= self.max_revisions:
                self._block("reviewer requested changes beyond the revision budget")
                return self._outcome()

            self._transition(
                RunStatus.REVISION_REQUIRED, "reviewer requested one revision"
            )
            self.revisions_used += 1
            revision_prompt = self._revision_prompt(review_report or "")
            if not self._finish_primary_stage(revision_prompt, "revision"):
                return self._outcome()

        if not self.workspace_changed:
            self._block(
                "the reviewer approved a candidate that changes nothing. There "
                "is no patch to submit, so the run stops here for a human: read "
                "the review, and answer the issue in its thread if changing "
                "nothing was the right call."
            )
            return self._outcome()

        self._transition(
            RunStatus.VERIFICATION_PENDING, "final independent verification"
        )
        final_ok, _ = self._verify("final")
        if not final_ok:
            self._block("final independent verification failed")
            return self._outcome()
        self._transition(
            RunStatus.ENGINEERING_COMPLETE,
            "approved by the reviewer and verified independently",
        )
        return self._outcome()

    def _finish_primary_stage(self, prompt: Path, stage: str) -> bool:
        primary_ok, _ = self._run_agent("primary", prompt)
        if not primary_ok:
            self._block(f"primary agent did not complete the {stage} stage")
            return False
        self._record_workspace_change(stage)
        if not self._check_candidate_scope(stage):
            return False
        verified, result = self._verify(stage)
        if verified:
            return True
        if self.revisions_used >= self.max_revisions:
            self._block(f"independent verification failed after the {stage} stage")
            return False
        return self._repair(result)

    def _repair(self, failure: CommandResult) -> bool:
        """Spend one revision on a verification the primary stage failed.

        The budget is the run's, not the stage's: a revision spent here is one
        the reviewer cannot also ask for, so the loop stays as bounded as it
        was when only a REVISE verdict could trigger one.
        """
        self._transition(
            RunStatus.REVISION_REQUIRED,
            "verification failed after the primary stage; one revision granted",
        )
        self.revisions_used += 1
        primary_ok, _ = self._run_agent("primary", self._repair_prompt(failure))
        if not primary_ok:
            self._block("primary agent did not complete the repair stage")
            return False
        self._record_workspace_change("repair")
        repaired, _ = self._verify("repair")
        if not repaired:
            self._block("independent verification failed again after one revision")
            return False
        return True

    def _outcome(self) -> OrchestrationOutcome:
        outcome = OrchestrationOutcome(
            run_id=self.run.run_id,
            status=self.run.status,
            steps=self.steps,
            revisions_used=self.revisions_used,
            max_review_cycles=self.max_review_cycles,
            time_budget_seconds=self.run_time_budget_seconds,
            deadline_at=self.deadline.isoformat(),
            budget_override_reason=self.budget_override_reason,
            review_cycles=self.run.review_cycles,
            record_path=self.run_directory / "orchestration.json",
        )
        destination = outcome.record_path
        temporary = destination.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(outcome.to_dict(), indent=2) + "\n", encoding="utf-8"
        )
        temporary.replace(destination)
        return outcome


def orchestrate(
    *,
    run: RunRecord,
    run_directory: Path,
    workspace: Path,
    primary_prompt: Path,
    reviewer_prompt: Path,
    verification_command: Sequence[str],
    agent_factory: AgentFactory,
    agent_timeout_seconds: float = DEFAULT_AGENT_TIMEOUT_SECONDS,
    primary_command_budget: int = DEFAULT_PRIMARY_COMMAND_BUDGET,
    reviewer_command_budget: int = DEFAULT_REVIEWER_COMMAND_BUDGET,
    verification_timeout_seconds: float = 900,
    max_revisions: int = 1,
    max_review_cycles: int = 3,
    run_time_budget_seconds: float = DEFAULT_RUN_TIME_BUDGET_SECONDS,
    deadline_at: str | None = None,
    time_budget_name: str = "run",
    budget_override_reason: str | None = None,
    max_changed_files: int = DEFAULT_MAX_CHANGED_FILES,
    max_changed_lines: int = DEFAULT_MAX_CHANGED_LINES,
    announce: Callable[[str], None] = lambda message: None,
    check_target: bool = True,
    acknowledge_prior_attempts: bool = False,
    acknowledge_claims: bool = False,
    resume_review: bool = False,
) -> OrchestrationOutcome:
    """Run one bounded primary, reviewer, and verification loop for a run."""
    return _Orchestration(
        run=run,
        run_directory=run_directory,
        workspace=workspace,
        primary_prompt=primary_prompt,
        reviewer_prompt=reviewer_prompt,
        verification_command=verification_command,
        agent_factory=agent_factory,
        agent_timeout_seconds=agent_timeout_seconds,
        primary_command_budget=primary_command_budget,
        reviewer_command_budget=reviewer_command_budget,
        verification_timeout_seconds=verification_timeout_seconds,
        max_revisions=max_revisions,
        max_review_cycles=max_review_cycles,
        run_time_budget_seconds=run_time_budget_seconds,
        deadline_at=deadline_at,
        time_budget_name=time_budget_name,
        budget_override_reason=budget_override_reason,
        max_changed_files=max_changed_files,
        max_changed_lines=max_changed_lines,
        announce=announce,
        check_target=check_target,
        acknowledge_prior_attempts=acknowledge_prior_attempts,
        acknowledge_claims=acknowledge_claims,
    ).execute(resume_review=resume_review)
