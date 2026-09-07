"""Lifecycle regressions behind issues 58 and 59."""
import sys

from mailman.orchestrator import orchestrate
from tests.test_orchestrator import (
    APPROVED,
    PASSING_CHECK,
    OrchestratorHarness,
    ScriptedAgent,
)


class ProcedureRegressions(OrchestratorHarness):
    def test_reviewer_rewriting_an_already_changed_file_is_refused(self):
        outcome, _, _, _ = self.orchestrate(
            primary_script=[{"report": "fixed", "touch": ("fix.txt", "primary")}],
            reviewer_script=[{"report": APPROVED, "touch": ("fix.txt", "reviewer")}],
        )
        self.assertEqual(str(outcome.status), "BLOCKED")

    def test_agents_finishing_without_a_decision_is_not_human_ready(self):
        outcome, _, _, _ = self.orchestrate(
            primary_script=[{"report": "fixed", "touch": ("fix.txt", "fixed")}],
            reviewer_script=[{"report": APPROVED}],
        )
        self.assertFalse(outcome.ready)
        self.assertEqual(str(outcome.status), "ENGINEERING_COMPLETE")

    def test_same_interpreter_different_tests_are_refused_before_agent_launch(self):
        import json
        run, directory = self.make_run()
        (directory / "prompts.json").write_text(json.dumps({
            "verification_command": [sys.executable, "-m", "pytest", "wrong_test.py"],
        }), encoding="utf-8")
        agents = {"codex": ScriptedAgent("codex", []), "claude": ScriptedAgent("claude", [])}
        with self.assertRaisesRegex(ValueError, "verification"):
            orchestrate(
                run=run, run_directory=directory, workspace=self.workspace,
                primary_prompt=self.primary_prompt, reviewer_prompt=self.reviewer_prompt,
                verification_command=[sys.executable, "-c", PASSING_CHECK],
                agent_factory=lambda name, model: agents[name],
            )
