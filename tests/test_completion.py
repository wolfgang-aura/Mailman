import copy
import json
import sys
from datetime import UTC, datetime, timedelta

from mailman.artifacts import load_run
from mailman.artifacts import write_run
from mailman.completion import candidate_digest, finalize_review
from mailman.handoff import build_handoff, check_handoff
from mailman.identity import Identity, save_identity
from mailman.orchestrator import orchestrate
from mailman.review_decision import DecisionError
from tests.test_orchestrator import (
    APPROVED,
    PASSING_CHECK,
    OrchestratorHarness,
    ScriptedAgent,
    git,
)
from tests.test_review_decision import VALID


class CompletionTests(OrchestratorHarness):
    def completed(self):
        _, directory, _, _ = self.orchestrate(
            primary_script=[{"report": "fixed", "touch": ("fix.txt", "fixed\n")}],
            reviewer_script=[{"report": APPROVED}],
        )
        decision = copy.deepcopy(VALID)
        decision["questions"] = []
        (directory / "decision.json").write_text(json.dumps(decision), encoding="utf-8")
        return directory

    def test_finalize_requires_a_decision_and_does_not_rerun_the_agents(self):
        directory = self.completed()
        (directory / "decision.json").unlink()
        with self.assertRaises(DecisionError):
            finalize_review(directory)
        self.assertEqual(str(load_run(directory.name, directory.parent)[0].status), "ENGINEERING_COMPLETE")

    def test_finalize_accepts_decision_and_refuses_later_edits(self):
        directory = self.completed()
        finalize_review(directory)
        self.assertEqual(str(load_run(directory.name, directory.parent)[0].status), "READY_FOR_HUMAN_REVIEW")
        (self.workspace / "fix.txt").write_text("edited after verification", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "candidate-changed"):
            finalize_review(directory)

    def test_candidate_digest_survives_staging_and_commit(self):
        directory = self.completed()
        before = candidate_digest(self.workspace, self.base_commit)
        git(self.workspace, "add", "--", "fix.txt")
        self.assertEqual(candidate_digest(self.workspace, self.base_commit), before)
        git(self.workspace, "commit", "-m", "fix")
        self.assertEqual(candidate_digest(self.workspace, self.base_commit), before)
        finalize_review(directory)

    def authorship_fixture(self):
        run, directory = self.make_run()
        exported = directory / "export"
        exported.mkdir()
        (exported / "export.json").write_text(json.dumps({"workspace": str(self.workspace)}), encoding="utf-8")
        save_identity(self.data_root, Identity("Fixture", "fixture@users.noreply.github.com"))
        (self.workspace / "fix.txt").write_text("fix", encoding="utf-8")
        git(self.workspace, "add", "--", "fix.txt")
        git(self.workspace, "commit", "-m", "fix")
        return run, directory

    def test_unapproved_author_and_committer_refuse_at_handoff(self):
        run, directory = self.authorship_fixture()
        body = directory / "body.md"
        body.write_text("Fix the fixture.", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "author-identity"):
            build_handoff(run_id=run.run_id, run_directory=directory, body_path=body,
                          kind="pull-request", repository="example/project", title="Fix fixture",
                          head="fixture:main", base="main", owner_type_lookup=lambda _: "User")
        self.assertFalse((directory / "handoff.json").exists())

    def test_submission_checks_authors_in_exported_workspace(self):
        from mailman.submission import prepare_submission
        from tests.test_submission import SOURCE_DIFF, _policy
        run, directory = self.authorship_fixture()
        record = prepare_submission(run, directory, diff=SOURCE_DIFF, policy=_policy(),
                                    destination=directory / "submission", branch="main", title="Fix fixture")
        self.assertIn("author-identity", record["blocking_codes"])

    def test_handoff_rechecks_branch_identity_after_preview(self):
        run, directory = self.authorship_fixture()
        git(self.workspace, "config", "user.email", "fixture@users.noreply.github.com")
        git(self.workspace, "commit", "--amend", "--no-edit", "--reset-author")
        body = directory / "body.md"
        body.write_text("Fix the fixture.", encoding="utf-8")
        build_handoff(run_id=run.run_id, run_directory=directory, body_path=body,
                      kind="pull-request", repository="example/project", title="Fix fixture",
                      head="fixture:main", base="main", owner_type_lookup=lambda _: "User")
        git(self.workspace, "config", "user.email", "private@example.invalid")
        git(self.workspace, "commit", "--amend", "--no-edit", "--reset-author")
        result = check_handoff(directory)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "author-identity")

    def test_review_budget_is_spent_across_resume_calls(self):
        """https://github.com/wolfgang-aura/Mailman/issues/65

        The first orchestration already spent one review cycle. A resume that
        carries its own `--max-review-cycles 1` must not buy a second one: the
        budget belongs to the run, which is how a one-revision run reached five
        reviewer passes on the shared allowance.
        """
        directory = self.completed()
        run, _ = load_run(directory.name, directory.parent)
        self.assertEqual(run.review_cycles, 1)
        result = orchestrate(
            run=run, run_directory=directory, workspace=self.workspace,
            primary_prompt=self.primary_prompt, reviewer_prompt=self.reviewer_prompt,
            verification_command=[sys.executable, "-c", PASSING_CHECK],
            agent_factory=lambda name, model: self.fail(f"{name} ran on a spent budget"),
            max_review_cycles=1,
            resume_review=True,
        )
        self.assertEqual(str(result.status), "BLOCKED")
        self.assertEqual(result.review_cycles, 1)
        blocked = [step for step in result.steps if not step.ok][-1]
        self.assertIn("review budget spent", blocked.detail)

    def test_a_raised_review_budget_lets_one_more_cycle_run(self):
        directory = self.completed()
        run, _ = load_run(directory.name, directory.parent)
        reviewer = ScriptedAgent("claude", [{"report": APPROVED}])
        result = orchestrate(
            run=run, run_directory=directory, workspace=self.workspace,
            primary_prompt=self.primary_prompt, reviewer_prompt=self.reviewer_prompt,
            verification_command=[sys.executable, "-c", PASSING_CHECK],
            agent_factory=lambda name, model: reviewer,
            max_review_cycles=2,
            resume_review=True,
        )
        self.assertEqual(str(result.status), "ENGINEERING_COMPLETE")
        self.assertEqual(result.review_cycles, 2)

    def test_an_expired_run_budget_blocks_before_another_agent_runs(self):
        directory = self.completed()
        run, _ = load_run(directory.name, directory.parent)
        run.created_at = (datetime.now(UTC) - timedelta(hours=3)).isoformat()
        write_run(run, directory)
        result = orchestrate(
            run=run, run_directory=directory, workspace=self.workspace,
            primary_prompt=self.primary_prompt, reviewer_prompt=self.reviewer_prompt,
            verification_command=[sys.executable, "-c", PASSING_CHECK],
            agent_factory=lambda name, model: self.fail(f"{name} ran after the deadline"),
            resume_review=True,
        )
        self.assertEqual(str(result.status), "BLOCKED")
        self.assertEqual(result.time_budget_seconds, 7200)
        blocked = [step for step in result.steps if not step.ok][-1]
        self.assertIn("run time budget spent", blocked.detail)

    def test_a_run_budget_above_two_hours_requires_a_recorded_reason(self):
        run, directory = self.make_run()
        with self.assertRaisesRegex(ValueError, "override reason"):
            orchestrate(
                run=run, run_directory=directory, workspace=self.workspace,
                primary_prompt=self.primary_prompt, reviewer_prompt=self.reviewer_prompt,
                verification_command=[sys.executable, "-c", PASSING_CHECK],
                agent_factory=lambda name, model: self.fail("agent should not run"),
                run_time_budget_seconds=7201,
            )

    def test_resume_review_preserves_candidate_and_primary_evidence(self):
        directory = self.completed()
        run, _ = load_run(directory.name, directory.parent)
        reviewer = ScriptedAgent("claude", [{"report": APPROVED}])
        result = orchestrate(
            run=run, run_directory=directory, workspace=self.workspace,
            primary_prompt=self.primary_prompt, reviewer_prompt=self.reviewer_prompt,
            verification_command=[sys.executable, "-c", PASSING_CHECK],
            agent_factory=lambda name, model: reviewer if name == "claude" else self.fail("primary rerun"),
            resume_review=True,
        )
        self.assertEqual(str(result.status), "ENGINEERING_COMPLETE")
        self.assertTrue(list((directory / "orchestration-history").glob("*.json")))
        self.assertEqual((self.workspace / "fix.txt").read_text(encoding="utf-8"), "fixed\n")

    def test_each_role_resumes_its_own_codex_session(self):
        outcome, _, primary, reviewer = self.orchestrate(
            primary_script=[
                {"report": "first", "touch": ("fix.txt", "first\n"), "session_id": "p-1"},
                {"report": "revised", "touch": ("fix.txt", "revised\n"), "session_id": "p-1"},
            ],
            reviewer_script=[
                {"report": "fix it\nMAILMAN-VERDICT: REVISE\n", "session_id": "r-1"},
                {"report": APPROVED, "session_id": "r-1"},
            ],
        )
        self.assertEqual(str(outcome.status), "ENGINEERING_COMPLETE")
        self.assertEqual(primary.session_ids, [None, "p-1"])
        self.assertEqual(reviewer.session_ids, [None, "r-1"])
        self.assertIn("Continued primary task", primary.calls[1][1])
        self.assertNotIn("fix the fixture", primary.calls[1][1])
