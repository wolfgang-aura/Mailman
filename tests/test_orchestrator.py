from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from mailman.agents.base import AgentRequest, AgentResult, EngineeringAgent
from mailman.artifacts import create_run, load_run
from mailman.cli import main
from mailman.executor import CommandResult
from mailman.models import RunStatus
from mailman.orchestrator import (
    VERDICT_APPROVE,
    VERDICT_REVISE,
    orchestrate,
    parse_verdict,
)
from mailman.toolchain import probe_tool


def git(workspace: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(workspace), *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        shell=False,
    )
    return completed.stdout.strip()


class ScriptedAgent(EngineeringAgent):
    """An agent that writes a queued report and reports a queued exit code."""

    def __init__(
        self,
        agent_name: str,
        script: list[dict[str, object]],
        turn_budget: int | None = None,
        token_budget: int | None = None,
    ) -> None:
        self._name = agent_name
        self.script = script
        self._turn_budget = turn_budget
        self._token_budget = token_budget
        self.calls: list[tuple[str, str]] = []
        self.session_ids: list[str | None] = []
        self.command_budgets: list[int | None] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def turn_budget(self) -> int | None:
        return self._turn_budget

    @property
    def token_budget(self) -> int | None:
        return self._token_budget

    def run(self, request: AgentRequest) -> AgentResult:
        if not self.script:
            raise AssertionError(f"unexpected {request.role} agent call")
        step = self.script.pop(0)
        self.calls.append(
            (request.role, request.prompt_path.read_text(encoding="utf-8"))
        )
        self.session_ids.append(request.session_id)
        self.command_budgets.append(request.command_budget)
        report = step.get("report")
        touch = step.get("touch")
        if isinstance(touch, tuple):
            name, content = touch
            (request.workspace / str(name)).write_text(str(content), encoding="utf-8")
        if isinstance(report, str):
            request.report_path.write_text(report, encoding="utf-8")
        exit_code = step.get("exit_code", 0)
        timed_out = bool(step.get("timed_out", False))
        command_result = CommandResult(
            command=[f"scripted-{self._name}"],
            working_directory=str(request.workspace),
            started_at="2026-09-02T00:00:00+00:00",
            duration_seconds=0.01,
            exit_code=None if timed_out else int(exit_code),  # type: ignore[arg-type]
            stdout=str(step.get("stdout", RAN_ONE_COMMAND)),
            stderr="",
            timed_out=timed_out,
            timeout_seconds=request.timeout_seconds,
            environment={"operating_system": "test"},
            stopped_reason=step.get("stopped_reason"),  # type: ignore[arg-type]
        )
        return AgentResult(
            exit_code=command_result.exit_code,
            report_path=request.report_path,
            timed_out=timed_out,
            report_present=isinstance(report, str) and bool(report.strip()),
            command_result=command_result,
            stop_reason=step.get("stop_reason"),  # type: ignore[arg-type]
            session_id=step.get("session_id"),  # type: ignore[arg-type]
        )


#: A Claude-format transcript showing one shell command. Scripted agents emit
#: this by default so they look like an agent that actually ran something; a
#: step passing `stdout=""` is an agent that ran nothing.
RAN_ONE_COMMAND = json.dumps(
    {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "name": "Bash",
                    "input": {"command": "python -m pytest tests/"},
                }
            ]
        },
    }
)

#: Reviewers judge the code. Mailman, not the reviewer, owns the repeated gate.
APPROVED = "no findings\nMAILMAN-VERDICT: APPROVE\n"

PASSING_CHECK = "import sys; sys.exit(0)"
FAILING_CHECK = "import sys; sys.exit(1)"
# Passes on the clean base tree, then fails after the scripted primary creates
# its candidate. This distinguishes a broken candidate from a broken baseline.
POST_EDIT_FAILING_CHECK = (
    "import pathlib, sys; sys.exit(1 if pathlib.Path('fix.txt').exists() else 0)"
)
# Passes on the clean base tree, fails on the first candidate, and passes once
# the agent leaves a repair marker.
REPAIRABLE_CHECK = (
    "import pathlib, sys; "
    "sys.exit(0 if not pathlib.Path('fix.txt').exists() or "
    "pathlib.Path('repaired.txt').exists() else 1)"
)


def record_clear_target(run_directory: Path, *, attempts: list | None = None) -> None:
    """Record the searches a run needs before it is allowed to start."""
    (run_directory / "duplicate-search.json").write_text(
        json.dumps({"success": True, "matches": []}), encoding="utf-8"
    )
    (run_directory / "prior-art.json").write_text(
        json.dumps({"success": True, "attempts": attempts or []}), encoding="utf-8"
    )
    (run_directory / "target-intel.json").write_text(
        json.dumps(
            {
                "success": True,
                "repository": "example/project",
                "window_days": 14,
                "freshness": {
                    "human_outside_merges": 3,
                    "outside_pull_requests_closed_unmerged": 1,
                },
                "assessment": {"passes_freshness_bar": True},
            }
        ),
        encoding="utf-8",
    )
    (run_directory / "reproduction.json").write_text(
        json.dumps(
            {
                "success": True,
                "machine_checked": True,
                "reproduced": True,
                "checks": [],
            }
        ),
        encoding="utf-8",
    )
    (run_directory / "claims.json").write_text(
        json.dumps(
            {
                "success": True,
                "repository": "example/project",
                "issue_number": 1,
                "comments_read": 0,
                "claims": [],
                "assignments": [],
                "assignees": [],
            }
        ),
        encoding="utf-8",
    )


class OrchestratorHarness(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        git(self.workspace, "init", "--initial-branch=main")
        git(self.workspace, "config", "user.name", "Fixture")
        git(self.workspace, "config", "user.email", "fixture@example.invalid")
        (self.workspace / "code.txt").write_text("baseline\n", encoding="utf-8")
        git(self.workspace, "add", "--", "code.txt")
        git(self.workspace, "commit", "-m", "baseline")
        self.base_commit = git(self.workspace, "rev-parse", "HEAD")
        self.data_root = self.root / "runs"
        self.primary_prompt = self.root / "primary.md"
        self.primary_prompt.write_text("fix the fixture", encoding="utf-8")
        self.reviewer_prompt = self.root / "reviewer.md"
        self.reviewer_prompt.write_text("review the fixture", encoding="utf-8")

    def make_run(self):
        run, run_directory = create_run(
            repository="https://github.com/example/project.git",
            issue="https://github.com/example/project/issues/1",
            base_commit=self.base_commit,
            primary="codex",
            reviewer="claude",
            data_root=self.data_root,
        )
        record_clear_target(run_directory)
        return run, run_directory

    def orchestrate(
        self,
        *,
        primary_script: list[dict[str, object]],
        reviewer_script: list[dict[str, object]],
        check: str = PASSING_CHECK,
        max_revisions: int = 1,
        max_review_cycles: int = 3,
        primary_turn_budget: int | None = None,
    ):
        run, run_directory = self.make_run()
        primary = ScriptedAgent("codex", list(primary_script), primary_turn_budget)
        reviewer = ScriptedAgent("claude", list(reviewer_script))
        agents = {"codex": primary, "claude": reviewer}
        outcome = orchestrate(
            run=run,
            run_directory=run_directory,
            workspace=self.workspace,
            primary_prompt=self.primary_prompt,
            reviewer_prompt=self.reviewer_prompt,
            verification_command=[sys.executable, "-c", check],
            agent_factory=lambda name, model: agents[name],
            max_revisions=max_revisions,
            max_review_cycles=max_review_cycles,
        )
        return outcome, run_directory, primary, reviewer


class VerdictParsingTests(unittest.TestCase):
    def test_single_verdict_is_read(self) -> None:
        self.assertEqual(
            parse_verdict("findings\n\nMAILMAN-VERDICT: APPROVE\n"), VERDICT_APPROVE
        )
        self.assertEqual(
            parse_verdict("- fix the guard\nMAILMAN-VERDICT: REVISE"), VERDICT_REVISE
        )

    def test_repeated_agreeing_verdicts_are_read(self) -> None:
        report = "MAILMAN-VERDICT: REVISE\n\nsummary\n\nMAILMAN-VERDICT: REVISE\n"
        self.assertEqual(parse_verdict(report), VERDICT_REVISE)

    def test_missing_or_contradictory_verdicts_are_rejected(self) -> None:
        self.assertIsNone(parse_verdict(None))
        self.assertIsNone(parse_verdict("looks good to me"))
        self.assertIsNone(parse_verdict("MAILMAN-VERDICT: MAYBE"))
        self.assertIsNone(
            parse_verdict("MAILMAN-VERDICT: APPROVE\nMAILMAN-VERDICT: REVISE\n")
        )
        self.assertIsNone(parse_verdict("MAILMAN-VERDICT: APPROVE now"))


class WorkspaceChangeRecordingTests(OrchestratorHarness):
    def test_an_unchanged_workspace_is_recorded_as_no_work(self) -> None:
        # A stage where the agent changed nothing passes verification exactly as
        # the base commit does. The evidence has to say which one happened.
        outcome, _, _, _ = self.orchestrate(
            primary_script=[{"report": "contradictory issue, I changed nothing\n"}],
            reviewer_script=[{"report": "no candidate\nMAILMAN-VERDICT: APPROVE\n"}],
        )

        steps = {step.name: step for step in outcome.steps}
        self.assertIn("workspace-change:primary", steps)
        recorded = steps["workspace-change:primary"]
        self.assertFalse(recorded.ok)
        self.assertFalse(recorded.data["changed"])
        self.assertIn("identical to the base commit", recorded.detail)

    def test_a_large_candidate_stops_before_review(self) -> None:
        outcome, _, _, reviewer = self.orchestrate(
            primary_script=[
                {"report": "broad rewrite", "touch": ("fix.txt", "line\n" * 501)}
            ],
            reviewer_script=[],
        )
        self.assertEqual(str(outcome.status), "BLOCKED")
        self.assertEqual(reviewer.calls, [])
        blocked = [step for step in outcome.steps if not step.ok][-1]
        self.assertIn("small-patch budget", blocked.detail)

    def test_a_changed_workspace_names_the_paths(self) -> None:
        outcome, _, _, _ = self.orchestrate(
            primary_script=[
                {"report": "candidate ready\n", "touch": ("fix.txt", "fixed\n")}
            ],
            reviewer_script=[{"report": APPROVED}],
        )

        recorded = {step.name: step for step in outcome.steps}[
            "workspace-change:primary"
        ]
        self.assertTrue(recorded.ok)
        self.assertTrue(recorded.data["changed"])
        self.assertIn("fix.txt", recorded.detail)


class EmptyCandidateTests(OrchestratorHarness):
    """What happens to a stage that changed nothing.

    On run 20260902T054015Z-0e51db the issue's acceptance criteria contradicted
    an existing test, the primary agent correctly changed nothing, and three
    `ok` steps recorded it. The reviewer caught it, and its diagnosis was
    better than any status line, so the candidate still reaches the reviewer.
    It just cannot leave as a submission. See
    https://github.com/wolfgang-aura/Mailman/issues/10.
    """

    def test_the_reviewer_is_told_the_workspace_is_unchanged(self) -> None:
        _, _, _, reviewer = self.orchestrate(
            primary_script=[{"report": "the criteria contradict test_clamp\n"}],
            reviewer_script=[{"report": APPROVED}],
        )

        prompt = reviewer.calls[0][1]
        self.assertIn("The workspace is unchanged", prompt)
        self.assertIn("MAILMAN-VERDICT", prompt)

    def test_the_reviewer_prompt_names_the_writable_scratch_directory(self) -> None:
        # A reviewer that discovers its sandbox by hitting it spends its turns
        # collecting permission errors. See
        # https://github.com/wolfgang-aura/Mailman/issues/29.
        outcome, run_directory, _, reviewer = self.orchestrate(
            primary_script=[
                {"report": "candidate ready\n", "touch": ("fix.txt", "fixed\n")}
            ],
            reviewer_script=[{"report": APPROVED}],
        )

        prompt = reviewer.calls[0][1]
        self.assertIn("Where you can write", prompt)
        self.assertIn(str(run_directory / "scratch"), prompt)
        self.assertIs(outcome.status, RunStatus.ENGINEERING_COMPLETE)

    def test_a_reviewer_that_edits_the_workspace_stops_the_run(self) -> None:
        outcome, _, _, _ = self.orchestrate(
            primary_script=[
                {"report": "candidate ready\n", "touch": ("fix.txt", "fixed\n")}
            ],
            reviewer_script=[
                {
                    "report": "found a typo, fixed it\n"
                    "MAILMAN-VERIFICATION: RAN\nMAILMAN-VERDICT: APPROVE\n",
                    "touch": ("reviewer-fix.txt", "edited by the reviewer\n"),
                }
            ],
        )

        self.assertIs(outcome.status, RunStatus.BLOCKED)
        step = next(
            step for step in outcome.steps if step.name == "workspace-change:reviewer"
        )
        self.assertFalse(step.ok)
        self.assertIn("reviewer-fix.txt", step.detail)
        blocked = [step for step in outcome.steps if step.name == "blocked"]
        self.assertIn("reviewer changed the workspace", blocked[-1].detail)

    def test_the_reviewer_stage_records_no_change_when_only_reading(self) -> None:
        outcome, _, _, _ = self.orchestrate(
            primary_script=[
                {"report": "candidate ready\n", "touch": ("fix.txt", "fixed\n")}
            ],
            reviewer_script=[{"report": APPROVED}],
        )

        step = next(
            step for step in outcome.steps if step.name == "workspace-change:reviewer"
        )
        self.assertTrue(step.ok)
        self.assertEqual(step.data["changed"], False)

    def test_an_approved_empty_candidate_stops_for_a_human(self) -> None:
        outcome, _, _, _ = self.orchestrate(
            primary_script=[{"report": "nothing to change\n"}],
            reviewer_script=[{"report": APPROVED}],
        )

        self.assertIs(outcome.status, RunStatus.BLOCKED)
        self.assertFalse(outcome.ready)
        blocked = [step for step in outcome.steps if step.name == "blocked"]
        self.assertIn("changes nothing", blocked[-1].detail)

    def test_a_revision_that_produces_work_clears_the_notice(self) -> None:
        outcome, _, _, reviewer = self.orchestrate(
            primary_script=[
                {"report": "nothing to change\n"},
                {"report": "done\n", "touch": ("fix.txt", "fixed\n")},
            ],
            reviewer_script=[
                {"report": "the issue is real, do the work\nMAILMAN-VERDICT: REVISE\n"},
                {"report": APPROVED},
            ],
        )

        self.assertIn("The workspace is unchanged", reviewer.calls[0][1])
        self.assertNotIn("The workspace is unchanged", reviewer.calls[1][1])
        self.assertIs(outcome.status, RunStatus.ENGINEERING_COMPLETE)

    def test_a_candidate_with_work_is_never_told_it_is_empty(self) -> None:
        _, _, _, reviewer = self.orchestrate(
            primary_script=[
                {"report": "candidate ready\n", "touch": ("fix.txt", "fixed\n")}
            ],
            reviewer_script=[{"report": APPROVED}],
        )

        self.assertNotIn("The workspace is unchanged", reviewer.calls[0][1])


class OrchestrationTests(OrchestratorHarness):
    def test_command_budget_stop_is_recorded_without_a_false_usage_failure(self) -> None:
        run, directory = self.make_run()
        reason = "command budget exceeded: 21 commands attempted, budget 20"
        primary = ScriptedAgent(
            "codex",
            [{"exit_code": 1, "stdout": "", "stopped_reason": reason}],
            token_budget=100,
        )
        outcome = orchestrate(
            run=run,
            run_directory=directory,
            workspace=self.workspace,
            primary_prompt=self.primary_prompt,
            reviewer_prompt=self.reviewer_prompt,
            verification_command=[sys.executable, "-c", PASSING_CHECK],
            agent_factory=lambda name, model: primary,
        )

        self.assertEqual(outcome.status, RunStatus.BLOCKED)
        agent_step = next(
            step for step in outcome.steps if step.name == "agent:primary"
        )
        self.assertEqual(primary.command_budgets, [20])
        self.assertEqual(agent_step.data["command_budget"], 20)
        self.assertTrue(agent_step.data["command_budget_exceeded"])
        self.assertFalse(agent_step.data["usage_accounting_missing"])
        self.assertEqual(agent_step.detail, reason)

    def test_codex_input_overrun_blocks_before_review(self) -> None:
        run, directory = self.make_run()
        usage = json.dumps(
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 120,
                    "cached_input_tokens": 90,
                    "output_tokens": 10,
                },
            }
        )
        primary = ScriptedAgent(
            "codex",
            [{"report": "candidate", "touch": ("fix.txt", "fixed"), "stdout": usage}],
            token_budget=100,
        )
        outcome = orchestrate(
            run=run,
            run_directory=directory,
            workspace=self.workspace,
            primary_prompt=self.primary_prompt,
            reviewer_prompt=self.reviewer_prompt,
            verification_command=[sys.executable, "-c", PASSING_CHECK],
            agent_factory=lambda name, model: primary,
        )

        self.assertEqual(outcome.status, RunStatus.BLOCKED)
        agent_step = next(
            step for step in outcome.steps if step.name == "agent:primary"
        )
        self.assertTrue(agent_step.data["usage_budget_exceeded"])
        self.assertEqual(agent_step.data["role_usage"]["input_tokens"], 120)
        execution = json.loads(
            next((directory / "agent-executions").glob("*.json")).read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(execution["usage"]["cached_input_tokens"], 90)

        with self.assertRaisesRegex(ValueError, "usage budget already spent"):
            orchestrate(
                run=run,
                run_directory=directory,
                workspace=self.workspace,
                primary_prompt=self.primary_prompt,
                reviewer_prompt=self.reviewer_prompt,
                verification_command=[sys.executable, "-c", PASSING_CHECK],
                agent_factory=lambda name, model: self.fail("agent resumed"),
                resume_review=True,
            )

    def test_bounded_codex_without_usage_blocks(self) -> None:
        run, directory = self.make_run()
        primary = ScriptedAgent(
            "codex",
            [{"report": "candidate", "touch": ("fix.txt", "fixed"), "stdout": ""}],
            token_budget=100,
        )
        outcome = orchestrate(
            run=run,
            run_directory=directory,
            workspace=self.workspace,
            primary_prompt=self.primary_prompt,
            reviewer_prompt=self.reviewer_prompt,
            verification_command=[sys.executable, "-c", PASSING_CHECK],
            agent_factory=lambda name, model: primary,
        )

        self.assertEqual(outcome.status, RunStatus.BLOCKED)
        agent_step = next(
            step for step in outcome.steps if step.name == "agent:primary"
        )
        self.assertTrue(agent_step.data["usage_accounting_missing"])
        self.assertIn("cannot enforce", agent_step.detail)

    def test_failing_baseline_verification_stops_before_primary(self) -> None:
        run, directory = self.make_run()
        outcome = orchestrate(
            run=run,
            run_directory=directory,
            workspace=self.workspace,
            primary_prompt=self.primary_prompt,
            reviewer_prompt=self.reviewer_prompt,
            verification_command=[sys.executable, "-c", "raise SystemExit(1)"],
            agent_factory=lambda name, model: self.fail("agent started"),
        )

        self.assertEqual(str(outcome.status), "BLOCKED")
        self.assertFalse(any(step.name == "agent:primary" for step in outcome.steps))
        self.assertIn("baseline verification failed", outcome.steps[-1].detail)

    def test_baseline_verification_may_not_dirty_the_workspace(self) -> None:
        run, directory = self.make_run()
        command = "from pathlib import Path; Path('generated.txt').write_text('x')"
        outcome = orchestrate(
            run=run,
            run_directory=directory,
            workspace=self.workspace,
            primary_prompt=self.primary_prompt,
            reviewer_prompt=self.reviewer_prompt,
            verification_command=[sys.executable, "-c", command],
            agent_factory=lambda name, model: self.fail("agent started"),
        )

        self.assertEqual(str(outcome.status), "BLOCKED")
        baseline = next(
            step for step in outcome.steps if step.name == "verification:baseline"
        )
        self.assertFalse(baseline.ok)
        self.assertFalse(baseline.data["candidate_unchanged"])

    def test_a_hunt_deadline_stops_before_primary(self) -> None:
        run, directory = self.make_run()
        expired = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        outcome = orchestrate(
            run=run,
            run_directory=directory,
            workspace=self.workspace,
            primary_prompt=self.primary_prompt,
            reviewer_prompt=self.reviewer_prompt,
            verification_command=[sys.executable, "-c", PASSING_CHECK],
            agent_factory=lambda name, model: self.fail("agent started"),
            deadline_at=expired,
            time_budget_name="hunt",
        )

        self.assertEqual(str(outcome.status), "BLOCKED")
        self.assertFalse(any(step.name == "agent:primary" for step in outcome.steps))
        self.assertIn("hunt time budget spent", outcome.steps[-1].detail)

    def test_a_read_only_review_can_approve_before_the_harness_gate(self) -> None:
        outcome, run_directory, _, _ = self.orchestrate(
            primary_script=[
                {"report": "candidate ready\n", "touch": ("fix.txt", "fixed\n")}
            ],
            reviewer_script=[
                {
                    "report": (
                        "I could not run the requested tests, so I make no "
                        "test-pass claim.\n"
                        "MAILMAN-VERIFICATION: BLOCKED\n"
                        "MAILMAN-VERDICT: APPROVE\n"
                    ),
                    "stdout": "",
                }
            ],
        )

        self.assertIs(outcome.status, RunStatus.ENGINEERING_COMPLETE)
        record = json.loads(
            (run_directory / "orchestration.json").read_text(encoding="utf-8")
        )
        execution = [
            step for step in record["steps"] if step["name"] == "reviewer-execution"
        ]
        self.assertTrue(execution[-1]["ok"])
        self.assertEqual(execution[-1]["data"]["commands_run"], 0)
        self.assertEqual(execution[-1]["data"]["verification_claim"], "BLOCKED")

    def test_a_reviewer_that_ran_the_gate_still_clears_the_run(self) -> None:
        outcome, _, _, _ = self.orchestrate(
            primary_script=[
                {"report": "candidate ready\n", "touch": ("fix.txt", "fixed\n")}
            ],
            reviewer_script=[{"report": APPROVED}],
        )

        self.assertIs(outcome.status, RunStatus.ENGINEERING_COMPLETE)

    def test_a_legacy_blocked_verification_claim_does_not_replace_final_gate(
        self,
    ) -> None:
        outcome, _, _, _ = self.orchestrate(
            primary_script=[
                {"report": "candidate ready\n", "touch": ("fix.txt", "fixed\n")}
            ],
            reviewer_script=[
                {
                    "report": (
                        "I could not run the requested tests, so I make no "
                        "test-pass claim.\n"
                        "MAILMAN-VERIFICATION: BLOCKED\n"
                        "MAILMAN-VERDICT: APPROVE\n"
                    )
                }
            ],
        )
        self.assertIs(outcome.status, RunStatus.ENGINEERING_COMPLETE)

    def test_a_reviewer_needs_only_a_clear_verdict(self) -> None:
        outcome, _, _, _ = self.orchestrate(
            primary_script=[
                {"report": "candidate ready\n", "touch": ("fix.txt", "fixed\n")}
            ],
            reviewer_script=[
                {"report": "read it, looks fine\nMAILMAN-VERDICT: APPROVE\n"}
            ],
        )
        self.assertIs(outcome.status, RunStatus.ENGINEERING_COMPLETE)

    def test_a_reviewer_does_not_need_to_duplicate_the_harness_command(
        self,
    ) -> None:
        outcome, _, _, _ = self.orchestrate(
            primary_script=[
                {"report": "candidate ready\n", "touch": ("fix.txt", "fixed\n")}
            ],
            reviewer_script=[{"report": APPROVED, "stdout": ""}],
        )
        self.assertIs(outcome.status, RunStatus.ENGINEERING_COMPLETE)

    def test_a_blocked_verification_still_allows_a_revision(self) -> None:
        # A reviewer that could not run the gate can still read the code and
        # find something. Only its approval is worthless.
        outcome, _, primary, _ = self.orchestrate(
            primary_script=[
                {"report": "candidate ready\n", "touch": ("fix.txt", "fixed\n")},
                {"report": "revised\n"},
            ],
            reviewer_script=[
                {
                    "report": (
                        "cannot run the suite, but this name is wrong\n"
                        "MAILMAN-VERIFICATION: BLOCKED\n"
                        "MAILMAN-VERDICT: REVISE\n"
                    )
                },
                {"report": APPROVED},
            ],
        )

        self.assertIs(outcome.status, RunStatus.ENGINEERING_COMPLETE)
        self.assertEqual(outcome.revisions_used, 1)
        self.assertEqual(len(primary.calls), 2)

    def test_the_reviewer_prompt_assigns_review_not_reverification(self) -> None:
        _, _, _, reviewer = self.orchestrate(
            primary_script=[
                {"report": "candidate ready\n", "touch": ("fix.txt", "fixed\n")}
            ],
            reviewer_script=[{"report": APPROVED}],
        )

        self.assertIn("Review boundary", reviewer.calls[0][1])
        self.assertIn("Do not rerun that full gate", reviewer.calls[0][1])

    def test_the_reviewer_receives_changed_paths_and_the_primary_report(self) -> None:
        _, _, _, reviewer = self.orchestrate(
            primary_script=[
                {
                    "report": "changed the parser after a focused check\n",
                    "touch": ("fix.txt", "fixed\n"),
                }
            ],
            reviewer_script=[{"report": APPROVED}],
        )

        prompt = reviewer.calls[0][1]
        self.assertIn("Candidate briefing", prompt)
        self.assertIn("fix.txt", prompt)
        self.assertIn("changed the parser", prompt)

    def test_a_reviewer_that_executed_nothing_may_still_ask_for_a_revision(
        self,
    ) -> None:
        # REVISE from a reviewer that only read the code is still worth acting
        # on. Only an APPROVE claims a check that did not happen.
        outcome, _, primary, _ = self.orchestrate(
            primary_script=[
                {"report": "candidate ready\n", "touch": ("fix.txt", "fixed\n")},
                {"report": "revised\n"},
            ],
            reviewer_script=[
                {
                    "report": "rename the helper\nMAILMAN-VERDICT: REVISE\n",
                    "stdout": "",
                },
                {"report": APPROVED},
            ],
        )

        self.assertIs(outcome.status, RunStatus.ENGINEERING_COMPLETE)
        self.assertEqual(outcome.revisions_used, 1)
        self.assertEqual(len(primary.calls), 2)

    def test_the_reviewer_execution_count_is_on_the_record(self) -> None:
        _, run_directory, _, _ = self.orchestrate(
            primary_script=[
                {"report": "candidate ready\n", "touch": ("fix.txt", "fixed\n")}
            ],
            reviewer_script=[
                {"report": "no findings\nMAILMAN-VERDICT: APPROVE\n", "stdout": ""}
            ],
        )
        reviewer_rows = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(
                (run_directory / "agent-executions").glob("*-reviewer.json")
            )
        ]

        self.assertEqual(reviewer_rows[-1]["commands_run"], 0)

    def test_approved_candidate_reaches_human_review(self) -> None:
        outcome, run_directory, primary, reviewer = self.orchestrate(
            primary_script=[
                {"report": "candidate ready\n", "touch": ("fix.txt", "fixed\n")}
            ],
            reviewer_script=[{"report": APPROVED}],
        )

        self.assertEqual(outcome.status, RunStatus.ENGINEERING_COMPLETE)
        self.assertFalse(outcome.ready)
        self.assertEqual(outcome.revisions_used, 0)
        self.assertEqual(outcome.review_cycles, 1)
        self.assertEqual(primary.calls[0][0], "primary")
        self.assertEqual(len(reviewer.calls), 1)
        self.assertIn("MAILMAN-VERDICT", reviewer.calls[0][1])

        stored, _ = load_run(outcome.run_id, self.data_root)
        self.assertEqual(stored.status, RunStatus.ENGINEERING_COMPLETE)
        record = json.loads(
            (run_directory / "orchestration.json").read_text(encoding="utf-8")
        )
        self.assertFalse(record["ready_for_human_review"])
        self.assertEqual(record["final_status"], "ENGINEERING_COMPLETE")
        verifications = json.loads(
            (run_directory / "verification.json").read_text(encoding="utf-8")
        )
        self.assertEqual(len(verifications), 3)

    def test_one_revision_is_applied_and_then_approved(self) -> None:
        outcome, _, primary, reviewer = self.orchestrate(
            primary_script=[
                {"report": "first attempt\n"},
                {"report": "revised\n", "touch": ("fix.txt", "fixed\n")},
            ],
            reviewer_script=[
                {"report": "- add fix.txt\nMAILMAN-VERDICT: REVISE\n"},
                {"report": APPROVED},
            ],
        )

        self.assertEqual(outcome.status, RunStatus.ENGINEERING_COMPLETE)
        self.assertTrue((self.workspace / "fix.txt").is_file())
        self.assertEqual(outcome.revisions_used, 1)
        self.assertEqual(outcome.review_cycles, 2)
        self.assertIn("Reviewer findings to address", primary.calls[1][1])
        self.assertIn("add fix.txt", primary.calls[1][1])
        self.assertEqual(len(reviewer.calls), 2)

    def test_second_revision_request_stops_the_run(self) -> None:
        outcome, _, primary, reviewer = self.orchestrate(
            primary_script=[
                {"report": "first attempt\n"},
                {"report": "second attempt\n"},
            ],
            reviewer_script=[
                {"report": "MAILMAN-VERDICT: REVISE\n"},
                {"report": "MAILMAN-VERDICT: REVISE\n"},
            ],
        )

        self.assertEqual(outcome.status, RunStatus.BLOCKED)
        self.assertFalse(outcome.ready)
        self.assertEqual(outcome.revisions_used, 1)
        self.assertEqual(len(primary.calls), 2)
        self.assertEqual(len(reviewer.calls), 2)
        self.assertEqual(
            outcome.steps[-1].detail,
            "reviewer requested changes beyond the revision budget",
        )

    def test_missing_verdict_blocks_instead_of_assuming_approval(self) -> None:
        outcome, _, _, _ = self.orchestrate(
            primary_script=[
                {"report": "candidate ready\n", "touch": ("fix.txt", "fixed\n")}
            ],
            reviewer_script=[{"report": "this all looks fine to me\n"}],
        )

        self.assertEqual(outcome.status, RunStatus.BLOCKED)
        self.assertEqual(
            outcome.steps[-1].detail, "reviewer verdict was missing or contradictory"
        )

    def test_failed_verification_spends_one_revision_before_blocking(self) -> None:
        outcome, run_directory, primary, reviewer = self.orchestrate(
            primary_script=[
                {"report": "candidate ready\n", "touch": ("fix.txt", "fixed\n")},
                {"report": "second attempt\n"},
            ],
            reviewer_script=[],
            check=POST_EDIT_FAILING_CHECK,
        )

        self.assertEqual(outcome.status, RunStatus.BLOCKED)
        self.assertEqual(reviewer.calls, [])
        self.assertEqual([role for role, _ in primary.calls], ["primary", "primary"])
        self.assertEqual(outcome.revisions_used, 1)
        self.assertEqual(
            outcome.steps[-1].detail,
            "independent verification failed again after one revision",
        )
        repair_prompt = (run_directory / "repair-input.md").read_text(encoding="utf-8")
        self.assertIn("The verification you have to pass", repair_prompt)
        self.assertIn("exited with code 1", repair_prompt)

    def test_a_repaired_candidate_reaches_the_reviewer(self) -> None:
        outcome, _, primary, reviewer = self.orchestrate(
            primary_script=[
                {"report": "candidate ready\n", "touch": ("fix.txt", "fixed\n")},
                {"report": "fixed it\n", "touch": ("repaired.txt", "done")},
            ],
            reviewer_script=[{"report": APPROVED}],
            check=REPAIRABLE_CHECK,
        )

        self.assertEqual(outcome.status, RunStatus.ENGINEERING_COMPLETE)
        self.assertEqual(len(primary.calls), 2)
        self.assertEqual(len(reviewer.calls), 1)
        self.assertEqual(outcome.revisions_used, 1)

    def test_no_revision_budget_blocks_the_stage_as_before(self) -> None:
        outcome, _, primary, reviewer = self.orchestrate(
            primary_script=[
                {"report": "candidate ready\n", "touch": ("fix.txt", "fixed\n")}
            ],
            reviewer_script=[],
            check=POST_EDIT_FAILING_CHECK,
            max_revisions=0,
        )

        self.assertEqual(outcome.status, RunStatus.BLOCKED)
        self.assertEqual(len(primary.calls), 1)
        self.assertEqual(reviewer.calls, [])
        self.assertEqual(
            outcome.steps[-1].detail,
            "independent verification failed after the primary stage",
        )

    def test_a_revision_spent_on_verification_is_not_available_to_the_reviewer(
        self,
    ) -> None:
        outcome, _, primary, reviewer = self.orchestrate(
            primary_script=[
                {"report": "candidate ready\n", "touch": ("fix.txt", "fixed\n")},
                {"report": "fixed it\n", "touch": ("repaired.txt", "done")},
            ],
            reviewer_script=[{"report": "MAILMAN-VERDICT: REVISE\n"}],
            check=REPAIRABLE_CHECK,
        )

        self.assertEqual(outcome.status, RunStatus.BLOCKED)
        self.assertEqual(len(primary.calls), 2)
        self.assertEqual(outcome.revisions_used, 1)
        self.assertEqual(
            outcome.steps[-1].detail,
            "reviewer requested changes beyond the revision budget",
        )

    def test_successful_agent_exit_without_a_report_blocks(self) -> None:
        outcome, _, _, reviewer = self.orchestrate(
            primary_script=[{"exit_code": 0}],
            reviewer_script=[],
        )

        self.assertEqual(outcome.status, RunStatus.BLOCKED)
        self.assertEqual(reviewer.calls, [])
        self.assertEqual(
            outcome.steps[-1].detail,
            "primary agent did not complete the primary stage",
        )

    def test_a_turn_limit_is_named_with_the_budget_it_hit(self) -> None:
        outcome, run_directory, _, _ = self.orchestrate(
            primary_script=[{"exit_code": 1, "stop_reason": "error_max_turns"}],
            reviewer_script=[],
            primary_turn_budget=120,
        )

        self.assertEqual(outcome.status, RunStatus.BLOCKED)
        agent_step = next(
            step for step in outcome.steps if step.name == "agent:primary"
        )
        self.assertIn("it ran out of turns", agent_step.detail)
        self.assertIn("budget: 120 turns", agent_step.detail)
        self.assertEqual(agent_step.data["turn_budget"], 120)
        record = json.loads(
            (run_directory / "agent-executions" / "0001-primary.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(record["turn_budget"], 120)

    def test_an_agent_without_a_turn_bound_records_none(self) -> None:
        outcome, _, _, _ = self.orchestrate(
            primary_script=[{"exit_code": 0}],
            reviewer_script=[],
        )

        agent_step = next(
            step for step in outcome.steps if step.name == "agent:primary"
        )
        self.assertIsNone(agent_step.data["turn_budget"])
        self.assertEqual(agent_step.detail, "codex produced no report")

    def test_a_missing_agent_executable_blocks_the_run(self) -> None:
        class MissingExecutableAgent(EngineeringAgent):
            @property
            def name(self) -> str:
                return "codex"

            def run(self, request: AgentRequest) -> AgentResult:
                raise FileNotFoundError("agent executable 'codex' was not found")

        run, run_directory = self.make_run()
        outcome = orchestrate(
            run=run,
            run_directory=run_directory,
            workspace=self.workspace,
            primary_prompt=self.primary_prompt,
            reviewer_prompt=self.reviewer_prompt,
            verification_command=[sys.executable, "-c", PASSING_CHECK],
            agent_factory=lambda name, model: MissingExecutableAgent(),
        )

        self.assertEqual(outcome.status, RunStatus.BLOCKED)
        stored, _ = load_run(outcome.run_id, self.data_root)
        self.assertEqual(stored.status, RunStatus.BLOCKED)
        self.assertIn("was not found", outcome.steps[-1].detail)
        self.assertTrue((run_directory / "orchestration.json").is_file())

    def test_dirty_workspace_stops_before_any_agent_runs(self) -> None:
        (self.workspace / "stray.txt").write_text("dirty\n", encoding="utf-8")
        primary = ScriptedAgent("codex", [])
        run, run_directory = self.make_run()
        with self.assertRaises(ValueError) as caught:
            orchestrate(
                run=run,
                run_directory=run_directory,
                workspace=self.workspace,
                primary_prompt=self.primary_prompt,
                reviewer_prompt=self.reviewer_prompt,
                verification_command=[sys.executable, "-c", PASSING_CHECK],
                agent_factory=lambda name, model: primary,
            )
        self.assertIn("must be clean", str(caught.exception))
        self.assertEqual(primary.calls, [])

    def test_wrong_base_commit_stops_before_any_agent_runs(self) -> None:
        run, run_directory = create_run(
            repository="https://github.com/example/project.git",
            issue="https://github.com/example/project/issues/1",
            base_commit="a" * 40,
            primary="codex",
            reviewer="claude",
            data_root=self.data_root,
        )
        record_clear_target(run_directory)
        primary = ScriptedAgent("codex", [])
        with self.assertRaises(ValueError) as caught:
            orchestrate(
                run=run,
                run_directory=run_directory,
                workspace=self.workspace,
                primary_prompt=self.primary_prompt,
                reviewer_prompt=self.reviewer_prompt,
                verification_command=[sys.executable, "-c", PASSING_CHECK],
                agent_factory=lambda name, model: primary,
            )
        self.assertIn("does not match base commit", str(caught.exception))
        self.assertEqual(primary.calls, [])

    def test_a_run_that_already_started_cannot_be_orchestrated(self) -> None:
        run, run_directory = self.make_run()
        run.transition(RunStatus.PRIMARY_RUNNING, "already started")
        with self.assertRaises(ValueError) as caught:
            orchestrate(
                run=run,
                run_directory=run_directory,
                workspace=self.workspace,
                primary_prompt=self.primary_prompt,
                reviewer_prompt=self.reviewer_prompt,
                verification_command=[sys.executable, "-c", PASSING_CHECK],
                agent_factory=lambda name, model: ScriptedAgent("codex", []),
            )
        self.assertIn("requires an INITIALIZED or BLOCKED run", str(caught.exception))

    def test_a_blocked_run_can_be_orchestrated_again(self) -> None:
        """A precondition refusal must not cost a fresh clone.

        Run 20260906T104815Z-29582c was blocked before the primary started,
        for a missing `target-intel` record and a missing claim check. Both
        were recorded a minute later, and the run was then unreachable: the
        state table allows BLOCKED to PRIMARY_RUNNING, but orchestration
        demanded INITIALIZED and nothing transitions back to it.
        """
        run, run_directory = self.make_run()
        run.transition(RunStatus.BLOCKED, "no target intel")
        primary = ScriptedAgent(
            "codex", [{"report": "candidate ready\n", "touch": ("fix.txt", "fixed\n")}]
        )
        reviewer = ScriptedAgent("claude", [{"report": APPROVED}])
        agents = {"codex": primary, "claude": reviewer}
        outcome = orchestrate(
            run=run,
            run_directory=run_directory,
            workspace=self.workspace,
            primary_prompt=self.primary_prompt,
            reviewer_prompt=self.reviewer_prompt,
            verification_command=[sys.executable, "-c", PASSING_CHECK],
            agent_factory=lambda name, model: agents[name],
        )
        self.assertEqual(outcome.status, RunStatus.ENGINEERING_COMPLETE)
        self.assertFalse(outcome.ready)


class VerificationExecutableTests(OrchestratorHarness):
    """The gate runs the run's own interpreter, not whatever PATH offers.

    Run 20260903T050831Z-bed67e recorded ["python", "-m", "pytest", ...] and
    executed the host interpreter, which had none of the target's dependencies,
    so the gate failed on ModuleNotFoundError rather than on the candidate.
    """

    def test_a_bare_executable_is_resolved_through_the_run_toolchain(self) -> None:
        run, run_directory = self.make_run()
        probe_tool(
            run_directory,
            name="python",
            executable=Path(sys.executable),
            probe_arguments=["--version"],
            timeout_seconds=30,
        )
        agents = {
            "codex": ScriptedAgent(
                "codex",
                [{"report": "candidate ready\n", "touch": ("fix.txt", "fixed\n")}],
                None,
            ),
            "claude": ScriptedAgent("claude", [{"report": APPROVED}], None),
        }
        outcome = orchestrate(
            run=run,
            run_directory=run_directory,
            workspace=self.workspace,
            primary_prompt=self.primary_prompt,
            reviewer_prompt=self.reviewer_prompt,
            verification_command=["python", "-c", PASSING_CHECK],
            agent_factory=lambda name, model: agents[name],
        )

        self.assertEqual(outcome.status, RunStatus.ENGINEERING_COMPLETE)
        verifications = json.loads(
            (run_directory / "verification.json").read_text(encoding="utf-8")
        )
        for verification in verifications:
            self.assertEqual(
                verification["command"][0], str(Path(sys.executable).resolve())
            )


class VerificationAgreementTests(OrchestratorHarness):
    """The prompts and the gate must name the same verification program.

    On run 20260903T194455Z-140c59 the free-text verification passed to
    `build-prompts` was a parenthetical the agents could not execute as
    written, and the harness's own gate ran something else again. Three ideas
    of one command in a single run. See
    https://github.com/wolfgang-aura/Mailman/issues/58.
    """

    def _record_prompt_verification(self, run_directory, command: list[str]) -> None:
        (run_directory / "prompts.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "verification_command": command,
                }
            ),
            encoding="utf-8",
        )

    def test_a_gate_from_another_program_is_refused(self) -> None:
        run, run_directory = self.make_run()
        self._record_prompt_verification(run_directory, ["node", "--test"])
        primary = ScriptedAgent(
            "codex", [{"report": "candidate ready\n", "touch": ("fix.txt", "fixed\n")}]
        )
        reviewer = ScriptedAgent("claude", [{"report": APPROVED}])
        agents = {"codex": primary, "claude": reviewer}

        with self.assertRaisesRegex(ValueError, "different verification programs"):
            orchestrate(
                run=run,
                run_directory=run_directory,
                workspace=self.workspace,
                primary_prompt=self.primary_prompt,
                reviewer_prompt=self.reviewer_prompt,
                verification_command=[sys.executable, "-c", PASSING_CHECK],
                agent_factory=lambda name, model: agents[name],
            )

    def test_an_equivalent_spelling_of_the_same_program_passes(self) -> None:
        run, run_directory = self.make_run()
        # The prompts quote a bare name; the gate carries the resolved
        # interpreter. Same program, so the run is not refused.
        self._record_prompt_verification(run_directory, ["python", "-m", "pytest"])

        outcome, run_directory, _, _ = self.orchestrate(
            primary_script=[
                {"report": "candidate ready\n", "touch": ("fix.txt", "fixed\n")}
            ],
            reviewer_script=[{"report": APPROVED}],
        )

        self.assertEqual(outcome.status, RunStatus.ENGINEERING_COMPLETE)

    def test_a_run_whose_prompts_record_no_command_is_not_refused(self) -> None:
        run, run_directory = self.make_run()
        (run_directory / "prompts.json").write_text(
            json.dumps({"schema_version": 1, "verification_command": None}),
            encoding="utf-8",
        )

        outcome, run_directory, _, _ = self.orchestrate(
            primary_script=[
                {"report": "candidate ready\n", "touch": ("fix.txt", "fixed\n")}
            ],
            reviewer_script=[{"report": APPROVED}],
        )

        self.assertEqual(outcome.status, RunStatus.ENGINEERING_COMPLETE)


class OrchestrateCliTests(OrchestratorHarness):
    def test_cli_uses_the_attached_hunt_deadline(self) -> None:
        from mailman.artifacts import write_run
        from mailman.hunt import add_run, create_hunt, hunt_path, save
        from mailman.models import AgentConfig

        run, run_directory = self.make_run()
        run.primary = AgentConfig("codex", "fixture-primary")
        run.reviewer = AgentConfig("claude", "fixture-reviewer")
        write_run(run, run_directory)
        (run_directory / "environment.json").write_text(
            json.dumps({"success": True}), encoding="utf-8"
        )
        hunt = create_hunt(
            self.data_root,
            1,
            primary="codex",
            primary_model="fixture-primary",
            reviewer="claude",
            reviewer_model="fixture-reviewer",
        )
        add_run(self.data_root, hunt, run.run_id)
        hunt["deadline_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        save(hunt_path(self.data_root, hunt["hunt_id"]), hunt)
        stdout = StringIO()
        stderr = StringIO()
        with patch(
            "mailman.cli._make_agent", side_effect=AssertionError("agent started")
        ):
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(
                    [
                        "orchestrate",
                        run.run_id,
                        "--primary-prompt",
                        str(self.primary_prompt),
                        "--reviewer-prompt",
                        str(self.reviewer_prompt),
                        "--workspace",
                        str(self.workspace),
                        "--data-root",
                        str(self.data_root),
                        "--",
                        sys.executable,
                        "-c",
                        PASSING_CHECK,
                    ]
                )

        self.assertEqual(exit_code, 2, stderr.getvalue())
        self.assertIn("deadline expired", stderr.getvalue())

    def _invoke(self, agents: dict[str, ScriptedAgent], check: str) -> tuple[int, str]:
        run, run_directory = self.make_run()
        (run_directory / "environment.json").write_text(
            json.dumps({"success": True}), encoding="utf-8"
        )
        stdout = StringIO()
        stderr = StringIO()
        with (
            patch(
                "mailman.cli._make_agent",
                side_effect=lambda name, *, model, max_turns, executable=None, reasoning_effort=None, token_budget=2_000_000: (
                    agents[name]
                ),
            ),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            exit_code = main(
                [
                    "orchestrate",
                    run.run_id,
                    "--primary-prompt",
                    str(self.primary_prompt),
                    "--reviewer-prompt",
                    str(self.reviewer_prompt),
                    "--workspace",
                    str(self.workspace),
                    "--data-root",
                    str(self.data_root),
                    "--",
                    sys.executable,
                    "-c",
                    check,
                ]
            )
        self.assertEqual(stderr.getvalue(), "")
        return exit_code, stdout.getvalue()

    def test_cli_reports_a_ready_run_with_exit_code_zero(self) -> None:
        agents = {
            "codex": ScriptedAgent(
                "codex",
                [{"report": "candidate ready\n", "touch": ("fix.txt", "fixed\n")}],
            ),
            "claude": ScriptedAgent("claude", [{"report": APPROVED}]),
        }
        exit_code, output = self._invoke(agents, PASSING_CHECK)

        self.assertEqual(exit_code, 0, output)
        self.assertIn('"final_status": "ENGINEERING_COMPLETE"', output)
        self.assertIn('"ready_for_human_review": false', output)

    def test_cli_reports_a_blocked_run_with_exit_code_one(self) -> None:
        agents = {
            "codex": ScriptedAgent(
                "codex",
                [
                    {"report": "candidate ready\n", "touch": ("fix.txt", "fixed\n")},
                    {"report": "second attempt\n"},
                ],
            ),
            "claude": ScriptedAgent("claude", []),
        }
        exit_code, output = self._invoke(agents, FAILING_CHECK)

        self.assertEqual(exit_code, 1, output)
        self.assertIn('"final_status": "BLOCKED"', output)

    def test_cli_requires_a_verification_command(self) -> None:
        run, _ = self.make_run()
        stderr = StringIO()
        with redirect_stdout(StringIO()), redirect_stderr(stderr):
            exit_code = main(
                [
                    "orchestrate",
                    run.run_id,
                    "--primary-prompt",
                    str(self.primary_prompt),
                    "--reviewer-prompt",
                    str(self.reviewer_prompt),
                    "--workspace",
                    str(self.workspace),
                    "--data-root",
                    str(self.data_root),
                ]
            )
        self.assertEqual(exit_code, 2)
        self.assertIn("verification command is required", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
