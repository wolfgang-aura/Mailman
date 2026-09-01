from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from mailman.agents.base import AgentRequest, EngineeringAgent
from mailman.artifacts import append_agent_execution, append_verification, write_run
from mailman.executor import execute
from mailman.models import RunRecord, RunStatus, utc_now
from mailman.toolchain import prepare_agent_prompt
from mailman.workspace import inspect_workspace


VERDICT_APPROVE = "APPROVE"
VERDICT_REVISE = "REVISE"

_VERDICT_PATTERN = re.compile(
    r"^[ \t>*-]*MAILMAN-VERDICT:[ \t]*(APPROVE|REVISE)[ \t]*$", re.MULTILINE
)

_VERDICT_CONTRACT = """
## Required verdict

Finish your reply with exactly one line of the form `MAILMAN-VERDICT: APPROVE`
or `MAILMAN-VERDICT: REVISE`, written on its own line and nowhere else.

Choose REVISE when the candidate needs a change, and list every required change
above the verdict line. Choose APPROVE only when no change is required. A
missing, repeated, or contradictory verdict stops the run for a human.
"""

_REVISION_CONTRACT = """
## Reviewer findings to address

The reviewer read the candidate change and asked for revisions. Address every
finding below in the same workspace. This is the only revision in this run.

"""


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
        verification_timeout_seconds: float,
        max_revisions: int,
        announce: Callable[[str], None],
    ) -> None:
        self.run = run
        self.run_directory = run_directory
        self.workspace = workspace
        self.primary_prompt = primary_prompt
        self.reviewer_prompt = reviewer_prompt
        self.verification_command = list(verification_command)
        self.agent_factory = agent_factory
        self.agent_timeout_seconds = agent_timeout_seconds
        self.verification_timeout_seconds = verification_timeout_seconds
        self.max_revisions = max_revisions
        self.announce = announce
        self.steps: list[OrchestrationStep] = []
        self.revisions_used = 0

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

    def _transition(self, target: RunStatus, reason: str) -> None:
        self.run.transition(target, reason)
        write_run(self.run, self.run_directory)
        self._step("transition", ok=True, detail=f"{target}: {reason}")

    def _block(self, reason: str) -> None:
        self.run.transition(RunStatus.BLOCKED, reason)
        write_run(self.run, self.run_directory)
        self._step("blocked", ok=False, detail=reason)

    # Bounded stages ----------------------------------------------------

    def _run_agent(self, role: str, source_prompt: Path) -> tuple[bool, str | None]:
        configured = self.run.primary if role == "primary" else self.run.reviewer
        agent = self.agent_factory(configured.agent, configured.model)
        prompt_path = prepare_agent_prompt(
            self.run_directory, role=role, source_prompt=source_prompt
        )
        report_path = self.run_directory / f"{role}-report.md"
        self.announce(
            f"run  {role}: {agent.name} with a "
            f"{self.agent_timeout_seconds:g} second timeout."
        )
        result = agent.run(
            AgentRequest(
                run_id=self.run.run_id,
                role=role,
                prompt_path=prompt_path,
                workspace=self.workspace,
                report_path=report_path,
                timeout_seconds=self.agent_timeout_seconds,
            )
        )
        report_text = (
            result.report_path.read_text(encoding="utf-8", errors="replace")
            if result.report_present
            else None
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
                "process": result.command_result.to_dict(),
                "workflow_status_after_run": str(self.run.status),
            },
        )
        ok = not result.timed_out and result.exit_code == 0 and result.report_present
        if result.timed_out:
            detail = f"{agent.name} timed out"
        elif not result.report_present:
            detail = f"{agent.name} produced no report"
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
                "execution_record": str(record_path),
            },
        )
        return ok, report_text

    def _verify(self, stage: str) -> bool:
        self.announce(
            f"run  verification ({stage}): {' '.join(self.verification_command)}"
        )
        result = execute(
            self.verification_command,
            working_directory=self.workspace,
            timeout_seconds=self.verification_timeout_seconds,
        )
        command_number = append_verification(self.run_directory, result.to_dict())
        ok = not result.timed_out and result.exit_code == 0
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
            },
        )
        return ok

    def _write_derived_prompt(self, name: str, text: str) -> Path:
        destination = self.run_directory / name
        destination.write_text(text, encoding="utf-8")
        return destination

    def _review_prompt(self) -> Path:
        source = self.reviewer_prompt.read_text(encoding="utf-8").rstrip()
        return self._write_derived_prompt(
            "review-input.md", f"{source}\n{_VERDICT_CONTRACT}"
        )

    def _revision_prompt(self, findings: str) -> Path:
        source = self.primary_prompt.read_text(encoding="utf-8").rstrip()
        return self._write_derived_prompt(
            "revision-input.md",
            f"{source}\n{_REVISION_CONTRACT}{findings.strip()}\n",
        )

    # Entry point -------------------------------------------------------

    def execute(self) -> OrchestrationOutcome:
        if self.run.status is not RunStatus.INITIALIZED:
            raise ValueError(
                f"orchestration requires an INITIALIZED run, found {self.run.status}"
            )
        if self.max_revisions < 0:
            raise ValueError("max_revisions cannot be negative")
        if not self.verification_command:
            raise ValueError("a verification command is required")

        state = inspect_workspace(self.workspace)
        if state.head != self.run.base_commit:
            raise ValueError(
                f"workspace HEAD {state.head} does not match base commit "
                f"{self.run.base_commit}"
            )
        if not state.clean:
            raise ValueError("primary workspace must be clean before orchestration")
        self.workspace = state.path
        self._step(
            "workspace",
            ok=True,
            detail=f"clean workspace at {state.head}",
            data={"path": str(state.path), "head": state.head},
        )

        self._transition(RunStatus.PRIMARY_RUNNING, "primary agent starting")
        try:
            return self._loop()
        except (OSError, ValueError) as error:
            # A started run must never be left claiming it is still in flight.
            self._block(f"orchestration stopped on an unexpected error: {error}")
            return self._outcome()

    def _loop(self) -> OrchestrationOutcome:
        if not self._finish_primary_stage(self.primary_prompt, "primary"):
            return self._outcome()

        review_prompt = self._review_prompt()
        while True:
            self._transition(RunStatus.REVIEW_PENDING, "reviewer reading the candidate")
            reviewer_ok, review_report = self._run_agent("reviewer", review_prompt)
            self.run.review_cycles += 1
            write_run(self.run, self.run_directory)
            if not reviewer_ok:
                self._block("reviewer did not complete a readable review")
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

        self._transition(
            RunStatus.VERIFICATION_PENDING, "final independent verification"
        )
        if not self._verify("final"):
            self._block("final independent verification failed")
            return self._outcome()
        self._transition(
            RunStatus.READY_FOR_HUMAN_REVIEW,
            "approved by the reviewer and verified independently",
        )
        return self._outcome()

    def _finish_primary_stage(self, prompt: Path, stage: str) -> bool:
        primary_ok, _ = self._run_agent("primary", prompt)
        if not primary_ok:
            self._block(f"primary agent did not complete the {stage} stage")
            return False
        if not self._verify(stage):
            self._block(f"independent verification failed after the {stage} stage")
            return False
        return True

    def _outcome(self) -> OrchestrationOutcome:
        outcome = OrchestrationOutcome(
            run_id=self.run.run_id,
            status=self.run.status,
            steps=self.steps,
            revisions_used=self.revisions_used,
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
    agent_timeout_seconds: float = 3600,
    verification_timeout_seconds: float = 900,
    max_revisions: int = 1,
    announce: Callable[[str], None] = lambda message: None,
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
        verification_timeout_seconds=verification_timeout_seconds,
        max_revisions=max_revisions,
        announce=announce,
    ).execute()
