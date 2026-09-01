from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
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

    def __init__(self, agent_name: str, script: list[dict[str, object]]) -> None:
        self._name = agent_name
        self.script = script
        self.calls: list[tuple[str, str]] = []

    @property
    def name(self) -> str:
        return self._name

    def run(self, request: AgentRequest) -> AgentResult:
        if not self.script:
            raise AssertionError(f"unexpected {request.role} agent call")
        step = self.script.pop(0)
        self.calls.append(
            (request.role, request.prompt_path.read_text(encoding="utf-8"))
        )
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
            stdout="",
            stderr="",
            timed_out=timed_out,
            timeout_seconds=request.timeout_seconds,
            environment={"operating_system": "test"},
        )
        return AgentResult(
            exit_code=command_result.exit_code,
            report_path=request.report_path,
            timed_out=timed_out,
            report_present=isinstance(report, str) and bool(report.strip()),
            command_result=command_result,
        )


PASSING_CHECK = "import sys; sys.exit(0)"
FAILING_CHECK = "import sys; sys.exit(1)"


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
        return create_run(
            repository="https://github.com/example/project.git",
            issue="https://github.com/example/project/issues/1",
            base_commit=self.base_commit,
            primary="codex",
            reviewer="claude",
            data_root=self.data_root,
        )

    def orchestrate(
        self,
        *,
        primary_script: list[dict[str, object]],
        reviewer_script: list[dict[str, object]],
        check: str = PASSING_CHECK,
        max_revisions: int = 1,
    ):
        run, run_directory = self.make_run()
        primary = ScriptedAgent("codex", list(primary_script))
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


class OrchestrationTests(OrchestratorHarness):
    def test_approved_candidate_reaches_human_review(self) -> None:
        outcome, run_directory, primary, reviewer = self.orchestrate(
            primary_script=[{"report": "candidate ready\n"}],
            reviewer_script=[{"report": "no findings\nMAILMAN-VERDICT: APPROVE\n"}],
        )

        self.assertEqual(outcome.status, RunStatus.READY_FOR_HUMAN_REVIEW)
        self.assertTrue(outcome.ready)
        self.assertEqual(outcome.revisions_used, 0)
        self.assertEqual(outcome.review_cycles, 1)
        self.assertEqual(primary.calls[0][0], "primary")
        self.assertEqual(len(reviewer.calls), 1)
        self.assertIn("MAILMAN-VERDICT", reviewer.calls[0][1])

        stored, _ = load_run(outcome.run_id, self.data_root)
        self.assertEqual(stored.status, RunStatus.READY_FOR_HUMAN_REVIEW)
        record = json.loads(
            (run_directory / "orchestration.json").read_text(encoding="utf-8")
        )
        self.assertTrue(record["ready_for_human_review"])
        self.assertEqual(record["final_status"], "READY_FOR_HUMAN_REVIEW")
        verifications = json.loads(
            (run_directory / "verification.json").read_text(encoding="utf-8")
        )
        self.assertEqual(len(verifications), 2)

    def test_one_revision_is_applied_and_then_approved(self) -> None:
        outcome, _, primary, reviewer = self.orchestrate(
            primary_script=[
                {"report": "first attempt\n"},
                {"report": "revised\n", "touch": ("fix.txt", "fixed\n")},
            ],
            reviewer_script=[
                {"report": "- add fix.txt\nMAILMAN-VERDICT: REVISE\n"},
                {"report": "resolved\nMAILMAN-VERDICT: APPROVE\n"},
            ],
        )

        self.assertEqual(outcome.status, RunStatus.READY_FOR_HUMAN_REVIEW)
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
            primary_script=[{"report": "candidate ready\n"}],
            reviewer_script=[{"report": "this all looks fine to me\n"}],
        )

        self.assertEqual(outcome.status, RunStatus.BLOCKED)
        self.assertEqual(
            outcome.steps[-1].detail, "reviewer verdict was missing or contradictory"
        )

    def test_failed_verification_stops_before_the_reviewer(self) -> None:
        outcome, _, _, reviewer = self.orchestrate(
            primary_script=[{"report": "candidate ready\n"}],
            reviewer_script=[],
            check=FAILING_CHECK,
        )

        self.assertEqual(outcome.status, RunStatus.BLOCKED)
        self.assertEqual(reviewer.calls, [])
        self.assertEqual(
            outcome.steps[-1].detail,
            "independent verification failed after the primary stage",
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
        self.assertIn("requires an INITIALIZED run", str(caught.exception))


class OrchestrateCliTests(OrchestratorHarness):
    def _invoke(self, agents: dict[str, ScriptedAgent], check: str) -> tuple[int, str]:
        run, _ = self.make_run()
        stdout = StringIO()
        stderr = StringIO()
        with patch(
            "mailman.cli._make_agent",
            side_effect=lambda name, *, model, max_turns, executable=None: agents[name],
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
                        check,
                    ]
                )
        self.assertEqual(stderr.getvalue(), "")
        return exit_code, stdout.getvalue()

    def test_cli_reports_a_ready_run_with_exit_code_zero(self) -> None:
        agents = {
            "codex": ScriptedAgent("codex", [{"report": "candidate ready\n"}]),
            "claude": ScriptedAgent(
                "claude", [{"report": "MAILMAN-VERDICT: APPROVE\n"}]
            ),
        }
        exit_code, output = self._invoke(agents, PASSING_CHECK)

        self.assertEqual(exit_code, 0, output)
        self.assertIn('"final_status": "READY_FOR_HUMAN_REVIEW"', output)
        self.assertIn('"ready_for_human_review": true', output)

    def test_cli_reports_a_blocked_run_with_exit_code_one(self) -> None:
        agents = {
            "codex": ScriptedAgent("codex", [{"report": "candidate ready\n"}]),
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
