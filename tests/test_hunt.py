import copy
import json
import sys
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path

from mailman.artifacts import load_run, write_run
from mailman.cli import main
from mailman.completion import finalize_review
from mailman.export import export_patch
from mailman.claims import read_claims
from mailman.handoff import build_handoff, load_handoff, load_offer_handoff
from mailman.hunt import (
    abandon,
    add_run,
    acquire_lease,
    candidate_repositories,
    require_lease,
    compact,
    create_hunt,
    deadline,
    effective_status,
    finish,
    holding_hunt,
    hunt_path,
    load_hunt,
    next_action,
    open_pull_request_repositories,
    record_filing,
    record_prescreen,
    refresh,
    restore_run,
    save,
    status,
    stop,
    target_claims,
    workable_targets,
    stale_screen_warning,
)
from mailman.identity import Identity, save_identity
from mailman.models import AgentConfig
from mailman.provenance import ProvenanceError, unrecorded_submissions
from mailman.screen import screen_path
from mailman.submission import prepare_submission
from mailman.targeting import OPEN_PULL_REQUEST, assess_target
from tests.test_orchestrator import APPROVED, OrchestratorHarness, git
from tests.test_review_decision import VALID
from tests.test_submission import _policy


class HuntTests(OrchestratorHarness):
    def new_hunt(self, count=1):
        return create_hunt(self.data_root, count, primary="codex", primary_model="fixture-primary",
                           reviewer="claude", reviewer_model="fixture-reviewer")

    def ready_run(self):
        _, directory, _, _ = self.orchestrate(
            primary_script=[{"report": "Fixed synthetic fixture", "touch": ("tests/test_fix.py", "assert True\n")}],
            reviewer_script=[{"report": APPROVED}],
        )
        run, _ = load_run(directory.name, self.data_root)
        run.primary = AgentConfig("codex", "fixture-primary")
        run.reviewer = AgentConfig("claude", "fixture-reviewer")
        write_run(run, directory)
        identity = Identity("Fixture", "fixture@users.noreply.github.com")
        save_identity(self.data_root, identity)
        git(self.workspace, "config", "user.email", identity.email)
        now = datetime.now(UTC).isoformat()
        for name, payload in {
            "issue.json": {"success": True, "reference": {"owner": "example", "repository": "project", "number": 1}, "title": "Fixture defect"},
            "environment.json": {"success": True, "workspace_path": str(self.workspace)},
            "workspace.json": {"success": True, "path": str(self.workspace), "head": self.base_commit},
            "prompts.json": {"verification_command": [sys.executable, "-c", "pass"]},
            "duplicate-search.json": {"success": True, "complete": True, "searched_at": now, "repository": "example/project", "matches": []},
        }.items():
            (directory / name).write_text(json.dumps(payload), encoding="utf-8")
        claims = json.loads((directory / "claims.json").read_text(encoding="utf-8"))
        claims["collected_at"] = now
        (directory / "claims.json").write_text(json.dumps(claims), encoding="utf-8")
        screen = screen_path(self.data_root, "example/project")
        screen.parent.mkdir(exist_ok=True)
        screen.write_text(json.dumps({"success": True, "verdict": "pass", "screened_at": now}), encoding="utf-8")
        export_patch(run, directory, workspace=self.workspace, destination=directory / "export")
        diff = (directory / "export" / "changes.diff").read_text(encoding="utf-8")
        submission = prepare_submission(run, directory, diff=diff, policy=_policy(),
                                        destination=directory / "submission", branch="main", title="Fix synthetic fixture")
        self.assertTrue(submission["ready"], submission["blocking_codes"])
        decision = copy.deepcopy(VALID)
        decision["questions"] = []
        decision["headline"] = "Synthetic fixture for PRHunt validation. No real filing."
        (directory / "decision.json").write_text(json.dumps(decision), encoding="utf-8")
        finalize_review(directory)
        git(self.workspace, "add", "--", "tests/test_fix.py")
        git(self.workspace, "commit", "-m", "fixture candidate")
        body = directory / "final-body.md"
        body.write_text("Synthetic fixture. No upstream filing is intended.\n", encoding="utf-8")
        build_handoff(run_id=run.run_id, run_directory=directory, body_path=body,
                      kind="pull-request", repository="example/project", title="Fix synthetic fixture",
                      head="fixture:main", base="main", owner_type_lookup=lambda _: "User")
        return directory

    def setUp(self):
        super().setUp()
        (self.workspace / "tests").mkdir()

    def test_full_fixture_reaches_one_approval_packet_without_publishing(self):
        hunt = self.new_hunt()
        directory = self.ready_run()
        add_run(self.data_root, hunt, directory.name)
        result = finish(self.data_root, hunt)
        self.assertTrue(result["complete"], result)
        self.assertFalse(result["published"])
        self.assertEqual(load_hunt(self.data_root, hunt["hunt_id"])["status"], "AWAITING_FILING_APPROVAL")

    def test_a_ready_candidate_is_published_before_the_quota_is_met(self):
        """https://github.com/wolfgang-aura/Mailman/issues/64

        Two finished candidates stayed invisible for hours because the packet
        waits for the whole quota. A checkpoint page shows what is ready now;
        `hunt finish` still holds the freshness gate.
        """
        hunt = self.new_hunt(3)
        directory = self.ready_run()
        add_run(self.data_root, hunt, directory.name)
        result = status(self.data_root, hunt)
        self.assertEqual(result["ready"], 1)
        self.assertEqual(result["remaining"], 2)
        checkpoint = Path(result["checkpoint"])
        self.assertTrue(checkpoint.is_file())
        self.assertIn("1 of 3 candidates ready", checkpoint.read_text(encoding="utf-8"))
        self.assertFalse(finish(self.data_root, hunt)["complete"])

    def ask_ready_run(self, *, keep_pr_handoff=False, offer_handoff=True):
        """A verified candidate whose decision is ASK, with its offer handed over."""
        directory = self.ready_run()
        if not keep_pr_handoff:
            (directory / "handoff.json").unlink()
        (directory / "offer-comment.md").write_text(
            f"@maintainer This reproduces on `main` ({self.base_commit[:8]}). "
            "A fix is ready; would you like a PR?\n",
            encoding="utf-8",
        )
        decision = json.loads((directory / "decision.json").read_text(encoding="utf-8"))
        decision["recommendation"] = "ASK"
        decision["offer"] = {"path": "offer-comment.md"}
        (directory / "decision.json").write_text(json.dumps(decision), encoding="utf-8")
        if offer_handoff:
            build_handoff(run_id=directory.name, run_directory=directory,
                          body_path=directory / "offer-comment.md", kind="issue-comment",
                          repository="example/project", issue_number=1, offer=True)
        return directory

    def test_an_ask_first_candidate_needs_its_offer_handoff(self):
        """https://github.com/wolfgang-aura/Mailman/issues/138"""
        hunt = self.new_hunt()
        directory = self.ask_ready_run(offer_handoff=False)
        add_run(self.data_root, hunt, directory.name)

        row = status(self.data_root, hunt)["runs"][0]

        self.assertEqual(row["stage"], "handoff")
        self.assertIn("--offer --kind issue-comment --issue 1", row["action"])
        self.assertNotEqual(row["disposition"], "READY_TO_ASK")

    def test_the_offer_handoff_and_the_pr_handoff_coexist(self):
        """https://github.com/wolfgang-aura/Mailman/issues/138

        The offer used to overwrite `handoff.json`, so a run carried the offer
        or the pull request, never both.
        """
        hunt = self.new_hunt()
        directory = self.ask_ready_run(keep_pr_handoff=True)
        add_run(self.data_root, hunt, directory.name)

        self.assertEqual(load_handoff(directory)["kind"], "pull-request")
        self.assertEqual(load_offer_handoff(directory)["kind"], "issue-comment")
        result = status(self.data_root, hunt)
        self.assertEqual(result["ready_to_ask"], 1)
        self.assertEqual(result["ready"], 0)

    def test_a_maintainer_reply_to_the_offer_moves_the_run_to_send(self):
        """https://github.com/wolfgang-aura/Mailman/issues/138"""
        from tests.test_claims import _FakeGh, _comment

        hunt = self.new_hunt()
        directory = self.ask_ready_run()
        add_run(self.data_root, hunt, directory.name)
        prepared = datetime.fromisoformat(load_offer_handoff(directory)["prepared_at"])
        later = (prepared + timedelta(hours=3)).isoformat()
        reply = {**_comment("Yes, a PR would be welcome.", association="COLLABORATOR",
                            login="keeper"), "created_at": later}
        read_claims(directory, executable="gh", execute=_FakeGh(
            {"number": 1, "assignees": [], "state": "open", "closed_at": None,
             "author_association": "NONE"}, [reply]))

        result = status(self.data_root, hunt)

        row = result["runs"][0]
        self.assertEqual(result["ready_to_ask"], 0)
        self.assertEqual(row["stage"], "decision")
        self.assertEqual(row["action"], "maintainer replied to offer; switch decision to SEND")
        self.assertIn("keeper (COLLABORATOR)", row["detail"])

    def test_an_ask_first_candidate_is_counted_apart_and_never_as_a_pr(self):
        """https://github.com/wolfgang-aura/Mailman/issues/138

        An answered ask-first question used to read as coordinator work, so a
        complete candidate could never be ready. It is READY_TO_ASK now: listed
        and packaged with its offer, but the PR quota does not move.
        """
        hunt = self.new_hunt()
        directory = self.ask_ready_run()
        add_run(self.data_root, hunt, directory.name)

        result = status(self.data_root, hunt)

        self.assertEqual(result["ready"], 0)
        self.assertEqual(result["remaining"], 1)
        self.assertEqual(result["ready_to_ask"], 1)
        row = result["runs"][0]
        self.assertFalse(row["ready"])
        self.assertEqual(row["disposition"], "READY_TO_ASK")
        self.assertIn("would you like a PR?",
                      Path(result["checkpoint"]).read_text(encoding="utf-8"))
        finished = finish(self.data_root, hunt)
        self.assertFalse(finished["complete"])
        self.assertEqual(finished["ready_to_ask"], 1)

    def test_an_offer_on_an_own_words_target_is_ready_to_ask_for_the_rewrite(self):
        """Mailman #454: the withheld offer command is the operator's rewrite, not a repair."""
        hunt = self.new_hunt()
        directory = self.ask_ready_run(offer_handoff=False)
        path = directory / "submission" / "submission.json"
        submission = json.loads(path.read_text(encoding="utf-8"))
        submission.update(ready=False, blocking_codes=["policy-requires-own-words"])
        path.write_text(json.dumps(submission), encoding="utf-8")
        build_handoff(run_id=directory.name, run_directory=directory,
                      body_path=directory / "offer-comment.md", kind="issue-comment",
                      repository="example/project", issue_number=1, offer=True)
        add_run(self.data_root, hunt, directory.name)

        row = status(self.data_root, hunt)["runs"][0]

        self.assertEqual(row["disposition"], "READY_TO_ASK", row)
        self.assertTrue(row["human_required"])
        self.assertIn("own words", row["action"])

    def test_an_assignment_block_does_not_stop_the_ask_that_clears_it(self):
        """Mailman #399: semantica merges only assigned work, so the offer asks for it."""
        hunt = self.new_hunt()
        directory = self.ask_ready_run()
        path = directory / "submission" / "submission.json"
        submission = json.loads(path.read_text(encoding="utf-8"))
        submission.update(ready=False, blocking_codes=["needs-maintainer-assignment"])
        path.write_text(json.dumps(submission), encoding="utf-8")
        add_run(self.data_root, hunt, directory.name)

        row = status(self.data_root, hunt)["runs"][0]

        self.assertEqual(row["disposition"], "READY_TO_ASK")

    def test_no_checkpoint_is_written_when_nothing_is_ready(self):
        hunt = self.new_hunt(2)
        run, _ = self.make_run()
        run.primary = AgentConfig("codex", "fixture-primary")
        run.reviewer = AgentConfig("claude", "fixture-reviewer")
        write_run(run, self.data_root / run.run_id)
        add_run(self.data_root, hunt, run.run_id)
        self.assertNotIn("checkpoint", status(self.data_root, hunt))

    def test_refresh_renews_every_ready_candidate_in_one_batch(self):
        """https://github.com/wolfgang-aura/Mailman/issues/69

        Aging evidence is what turned two ready candidates into a reported
        zero. `hunt refresh` renews them together; `hunt finish` still decides.
        """
        from datetime import timedelta

        import mailman.hunt as hunt_module

        hunt = self.new_hunt()
        directory = self.ready_run()
        add_run(self.data_root, hunt, directory.name)
        self.assertEqual(status(self.data_root, hunt)["ready"], 1)

        stale = (datetime.now(UTC) - timedelta(hours=3)).isoformat()
        for name in ("duplicate-search.json", "claims.json"):
            payload = json.loads((directory / name).read_text(encoding="utf-8"))
            payload["searched_at" if "duplicate" in name else "collected_at"] = stale
            if "duplicate" in name:
                payload["query"] = "fixture defect"
            (directory / name).write_text(json.dumps(payload), encoding="utf-8")
        self.assertEqual(status(self.data_root, hunt)["ready"], 0)
        self.assertFalse(finish(self.data_root, hunt)["complete"])

        now = datetime.now(UTC).isoformat()
        calls = []

        def fake_duplicate_search(run_directory, *, repository, query, issue_number=None,
                                  symbols=(), **_):
            calls.append(("duplicate-search", query))
            record = {"success": True, "complete": True, "searched_at": now,
                      "repository": "example/project", "query": query, "matches": [],
                      "match_count": 0, "decided_by": "broad", "symbols": list(symbols)}
            (run_directory / "duplicate-search.json").write_text(
                json.dumps(record), encoding="utf-8")
            return record

        def fake_read_claims(run_directory, **_):
            calls.append(("claims", run_directory.name))
            record = json.loads((run_directory / "claims.json").read_text(encoding="utf-8"))
            record.update(collected_at=now, success=True)
            (run_directory / "claims.json").write_text(json.dumps(record), encoding="utf-8")
            return record

        import mailman.claims
        import mailman.submission
        original_search = mailman.submission.record_duplicate_search
        original_claims = mailman.claims.read_claims
        mailman.submission.record_duplicate_search = fake_duplicate_search
        mailman.claims.read_claims = fake_read_claims
        try:
            result = refresh(self.data_root, hunt)
        finally:
            mailman.submission.record_duplicate_search = original_search
            mailman.claims.read_claims = original_claims

        self.assertEqual(result["ready_before_refresh"], 0)
        self.assertEqual(result["ready"], 1)
        self.assertEqual(len(result["refreshed"]), 1)
        self.assertEqual([kind for kind, _ in calls], ["duplicate-search", "claims"])
        self.assertTrue(finish(self.data_root, hunt)["complete"])

    def test_refresh_leaves_a_ready_candidate_alone(self):
        hunt = self.new_hunt()
        directory = self.ready_run()
        add_run(self.data_root, hunt, directory.name)
        result = refresh(self.data_root, hunt)
        self.assertEqual(result["ready"], 1)
        self.assertEqual(result["refreshed"], [])

    def test_a_second_coordinator_cannot_change_a_leased_hunt(self):
        """https://github.com/wolfgang-aura/Mailman/issues/63"""
        hunt = self.new_hunt()
        owner = hunt["lease"]["owner"]
        with self.assertRaisesRegex(ValueError, "owned by"):
            require_lease(hunt, "another-coordinator")
        require_lease(hunt, owner)
        acquire_lease(self.data_root, hunt, owner="another-coordinator",
                      takeover_reason="first coordinator hit its usage limit")
        require_lease(hunt, "another-coordinator")
        self.assertEqual(load_hunt(self.data_root, hunt["hunt_id"])["lease"]["owner"],
                         "another-coordinator")

    def test_requested_three_does_not_count_one_as_completion(self):
        hunt = self.new_hunt(3)
        directory = self.ready_run()
        add_run(self.data_root, hunt, directory.name)
        add_run(self.data_root, hunt, directory.name)
        result = finish(self.data_root, hunt)
        self.assertFalse(result["complete"])
        self.assertEqual(result["remaining"], 2)
        self.assertEqual(len(hunt["runs"]), 1)

    def test_a_hunt_records_one_deadline_for_every_candidate(self):
        hunt = create_hunt(
            self.data_root, 1, primary="codex", primary_model="fixture-primary",
            reviewer="claude", reviewer_model="fixture-reviewer",
            time_budget_seconds=7200,
        )
        created = datetime.fromisoformat(hunt["created_at"])
        deadline = datetime.fromisoformat(hunt["deadline_at"])

        self.assertEqual(hunt["time_budget_seconds"], 7200)
        self.assertEqual(deadline - created, timedelta(hours=2))

    def test_an_expired_hunt_refuses_a_new_candidate(self):
        hunt = self.new_hunt()
        hunt["deadline_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        save(hunt_path(self.data_root, hunt["hunt_id"]), hunt)
        run, directory = self.make_run()
        run.primary = AgentConfig("codex", "fixture-primary")
        run.reviewer = AgentConfig("claude", "fixture-reviewer")
        write_run(run, directory)

        with self.assertRaisesRegex(ValueError, "fixed deadline"):
            add_run(self.data_root, hunt, run.run_id)

    def test_a_dropped_target_cannot_return_as_a_fresh_run(self):
        hunt = self.new_hunt()
        first, first_directory = self.make_run()
        first.primary = AgentConfig("codex", "fixture-primary")
        first.reviewer = AgentConfig("claude", "fixture-reviewer")
        write_run(first, first_directory)
        add_run(self.data_root, hunt, first.run_id)
        hunt["runs"][0].update(
            dropped=True,
            reason="verification failed",
            evidence="the recorded command exited 1",
        )
        save(hunt_path(self.data_root, hunt["hunt_id"]), hunt)

        second, second_directory = self.make_run()
        second.primary = AgentConfig("codex", "fixture-primary")
        second.reviewer = AgentConfig("claude", "fixture-reviewer")
        write_run(second, second_directory)

        with self.assertRaisesRegex(ValueError, "already used target"):
            add_run(self.data_root, hunt, second.run_id)

    def test_model_substitution_is_refused(self):
        hunt = self.new_hunt()
        run, _ = self.make_run()
        with self.assertRaisesRegex(ValueError, "model"):
            add_run(self.data_root, hunt, run.run_id)

    def test_a_dropped_run_can_be_restored_with_audit_evidence(self):
        hunt = self.new_hunt()
        directory = self.ready_run()
        add_run(self.data_root, hunt, directory.name)
        hunt["runs"][0].update(dropped=True, reason="review gap", evidence="old evidence")

        restore_run(
            self.data_root,
            hunt,
            directory.name,
            reason="review gap repaired",
            evidence="fresh reviewer approval and verification",
        )

        row = load_hunt(self.data_root, hunt["hunt_id"])["runs"][0]
        self.assertNotIn("dropped", row)
        self.assertEqual(row["restored"]["reason"], "review gap repaired")
        self.assertIn("fresh reviewer approval", row["restored"]["evidence"])

    def test_a_body_edit_invalidates_completion(self):
        directory = self.ready_run()
        (directory / "final-body.md").write_text("Changed after preview", encoding="utf-8")
        result = next_action(directory)
        self.assertFalse(result["ready"])
        self.assertEqual(result["stage"], "handoff")
        self.assertFalse(result["human_required"])

    def own_words_run(self, codes):
        """A ready run whose submission is held only by the named codes."""
        directory = self.ready_run()
        path = directory / "submission" / "submission.json"
        submission = json.loads(path.read_text(encoding="utf-8"))
        submission.update(ready=False, blocking_codes=codes)
        path.write_text(json.dumps(submission), encoding="utf-8")
        return directory

    def test_a_run_held_only_for_the_own_words_rewrite_is_ready_for_the_human(self):
        """https://github.com/wolfgang-aura/Mailman/issues/181"""
        result = next_action(self.own_words_run(["policy-requires-own-words"]))
        self.assertTrue(result["ready"], result)
        self.assertEqual(result["stage"], "filing-approval")
        self.assertTrue(result["human_required"])
        self.assertIn("own words", result["action"])

    def test_an_own_words_run_still_fails_any_other_handoff_refusal(self):
        # The own-words refusal is the only one a ready own-words run may
        # carry; a body edited after the handoff still sends it back. #181.
        directory = self.own_words_run(["policy-requires-own-words"])
        (directory / "final-body.md").write_text("Changed after preview", encoding="utf-8")
        result = next_action(directory)
        self.assertFalse(result["ready"])
        self.assertEqual(result["stage"], "handoff")

    def test_another_blocking_code_beside_own_words_still_blocks(self):
        result = next_action(self.own_words_run(["policy-requires-own-words", "lint-failed"]))
        self.assertFalse(result["ready"])
        self.assertEqual(result["stage"], "submission")

    def own_words_questions(self, gates):
        directory = self.own_words_run(["policy-requires-own-words"])
        path = directory / "decision.json"
        decision = json.loads(path.read_text(encoding="utf-8"))
        decision["questions"] = [
            {
                "question": f"Question for gate {gate}?",
                "blocking": True,
                **({"gate": gate} if gate else {}),
                "options": [
                    {"label": "A", "text": "File.", "cost": "None."},
                    {"label": "B", "text": "Drop.", "cost": "The run."},
                ],
                "recommendation": "A.",
            }
            for gate in gates
        ]
        path.write_text(json.dumps(decision), encoding="utf-8")
        return directory

    def test_the_own_words_question_does_not_hold_an_own_words_run(self):
        result = next_action(self.own_words_questions(["own-words"]))
        self.assertTrue(result["ready"], result)
        self.assertTrue(result["human_required"])

    def test_the_own_words_question_is_answered_once_the_rewrite_is_confirmed(self):
        # The confirmed rewrite clears the submission's own-words block, and
        # the question about it must not then hold the run. Mailman #434.
        directory = self.own_words_questions(["own-words"])
        path = directory / "submission" / "submission.json"
        submission = json.loads(path.read_text(encoding="utf-8"))
        submission.update(ready=True, blocking_codes=[])
        path.write_text(json.dumps(submission), encoding="utf-8")
        result = next_action(directory)
        self.assertTrue(result["ready"], result)
        self.assertFalse(result["human_required"])

    def assert_held_at_decision(self, gates):
        result = next_action(self.own_words_questions(gates))
        self.assertFalse(result["ready"])
        self.assertEqual(result["stage"], "decision")

    def test_the_triage_question_still_holds_an_own_words_run(self):
        # Before, any blocking question passed once own-words applied, so an
        # untriaged run with the policy counted ready. Mailman #181.
        self.assert_held_at_decision(["untriaged-issue"])

    def test_an_ungated_question_beside_the_own_words_one_still_holds(self):
        self.assert_held_at_decision(["own-words", None])

    def cla_run(self, gates):
        """A ready run whose decision carries one blocking question per gate."""
        directory = self.ready_run()
        path = directory / "decision.json"
        decision = json.loads(path.read_text(encoding="utf-8"))
        decision["questions"] = [
            {
                "question": "Has the author signed the project's CLA?",
                "blocking": True,
                **({"gate": gate} if gate else {}),
                "options": [
                    {"label": "A", "text": "Signed; file.", "cost": "None."},
                    {"label": "B", "text": "Drop.", "cost": "The run."},
                ],
                "recommendation": "A.",
            }
            for gate in gates
        ]
        path.write_text(json.dumps(decision), encoding="utf-8")
        return directory

    def test_a_run_held_only_for_the_cla_is_ready_for_the_human(self):
        # cloud-init: SEND, packaged, and held at REPAIR by the CLA question
        # while escalate refused a CLA as a reason. Mailman #290.
        result = next_action(self.cla_run(["cla"]))
        self.assertTrue(result["ready"], result)
        self.assertEqual(result["stage"], "filing-approval")
        self.assertTrue(result["human_required"])
        self.assertIn("CLA", result["action"])

    def test_another_blocking_question_beside_the_cla_still_blocks(self):
        result = next_action(self.cla_run(["cla", None]))
        self.assertFalse(result["ready"])
        self.assertEqual(result["stage"], "decision")

    def test_a_run_held_only_for_personal_review_is_ready_for_the_human(self):
        # Python-Markdown 1643: SEND and packaged, held at REPAIR because the
        # policy wants the submitter to answer review personally. Mailman #381.
        result = next_action(self.cla_run(["personal-review"]))
        self.assertTrue(result["ready"], result)
        self.assertTrue(result["human_required"])
        self.assertIn("review comments", result["action"])

    def test_another_blocking_question_beside_personal_review_still_blocks(self):
        result = next_action(self.cla_run(["personal-review", "untriaged-issue"]))
        self.assertFalse(result["ready"])
        self.assertEqual(result["stage"], "decision")

    def test_acknowledged_closed_attempts_remain_ready_after_orchestration(self):
        directory = self.ready_run()
        (directory / "prior-art.json").write_text(
            json.dumps({
                "attempts": [{
                    "number": 17,
                    "outcome": "closed unmerged",
                    "title": "Earlier attempt",
                    "url": "https://github.com/example/project/pull/17",
                }],
            }),
            encoding="utf-8",
        )
        (directory / "target-assessment.json").write_text(
            json.dumps({
                "may_start": True,
                "warnings": ["unacknowledged-prior-attempts"],
                "closed_attempts": [{"number": 17}],
            }),
            encoding="utf-8",
        )

        result = next_action(directory)

        self.assertTrue(result["ready"], result)
        self.assertEqual(result["stage"], "filing-approval")

    def test_new_duplicate_replaces_a_previously_ready_candidate(self):
        directory = self.ready_run()
        path = directory / "duplicate-search.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["matches"] = [{"number": 9, "state": "open", "pull_request": True,
                               "title": "Same fix", "references_issue": True}]
        path.write_text(json.dumps(payload), encoding="utf-8")
        result = next_action(directory)
        self.assertEqual(result["disposition"], "REPLACE", result)
        self.assertFalse(result["human_required"])

    def test_a_refusal_under_old_windows_is_re_read_not_replaced(self):
        # 294 cached screens were read with a 14-day freshness window and a
        # 90-day issue window; copier-org/copier failed only on the first.
        directory = self.ready_run()
        screen_path(self.data_root, "example/project").write_text(json.dumps({
            "success": True, "verdict": "fail", "failed_gates": ["freshness"],
            "window_days": 14, "issue_window_days": 90, "responsiveness_days": 90,
        }), encoding="utf-8")
        result = next_action(directory)
        self.assertEqual(result["stage"], "screen", result)
        self.assertEqual(result["disposition"], "REPAIR", result)
        self.assertIn("--refresh", result["action"])

    def test_a_refusal_under_current_windows_is_replaced(self):
        from mailman.screen import FRESHNESS_WINDOW_DAYS
        directory = self.ready_run()
        screen_path(self.data_root, "example/project").write_text(json.dumps({
            "success": True, "verdict": "fail", "failed_gates": ["freshness"],
            "window_days": FRESHNESS_WINDOW_DAYS, "issue_window_days": 730, "responsiveness_days": 90,
        }), encoding="utf-8")
        result = next_action(directory)
        self.assertEqual(result["disposition"], "REPLACE", result)

    def test_missing_screen_is_coordinator_work(self):
        _, directory = self.make_run()
        result = next_action(directory)
        self.assertEqual(result["stage"], "screen")
        self.assertFalse(result["human_required"])

    def test_routine_failure_cannot_be_recorded_as_a_user_blocker(self):
        hunt = self.new_hunt()
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            code = main(["hunt", "escalate", hunt["hunt_id"], "--reason", "missing-environment",
                         "--data-root", str(self.data_root)])
        self.assertEqual(code, 2)
        self.assertEqual(load_hunt(self.data_root, hunt["hunt_id"])["escalations"], [])


class TimeBudgetTests(HuntTests):
    """The hunt's clock is set once, at creation.

    https://github.com/wolfgang-aura/Mailman/issues/106
    """

    def test_a_hunt_has_no_deadline_unless_asked(self):
        """Each role's ten-minute limit already bounds a runaway model.

        https://github.com/wolfgang-aura/Mailman/issues/166
        """
        record = self.new_hunt()

        self.assertIsNone(record["time_budget_seconds"])
        self.assertIsNone(record["deadline_at"])
        self.assertIsNone(deadline(record))

    def test_a_hunt_without_a_deadline_takes_a_candidate_hours_later(self):
        hunt = self.new_hunt()
        hunt["created_at"] = (datetime.now(UTC) - timedelta(hours=5)).isoformat()
        save(hunt_path(self.data_root, hunt["hunt_id"]), hunt)
        run, directory = self.make_run()
        run.primary = AgentConfig("codex", "fixture-primary")
        run.reviewer = AgentConfig("claude", "fixture-reviewer")
        write_run(run, directory)

        add_run(self.data_root, hunt, run.run_id)
        result = status(self.data_root, hunt)

        self.assertEqual([row["run_id"] for row in hunt["runs"]], [run.run_id])
        self.assertIsNone(result["deadline_at"])
        self.assertFalse(result["deadline_expired"])

    def test_a_record_from_before_the_change_keeps_its_two_hours(self):
        record = self.new_hunt()
        del record["deadline_at"]
        record["time_budget_seconds"] = 7200

        self.assertEqual(
            deadline(record),
            datetime.fromisoformat(record["created_at"]) + timedelta(hours=2),
        )

    def test_six_hours_moves_the_deadline_six_hours_out(self):
        record = create_hunt(
            self.data_root, 1, primary="codex", primary_model="fixture-primary",
            reviewer="claude", reviewer_model="fixture-reviewer",
            time_budget_seconds=6 * 60 * 60,
        )

        self.assertEqual(record["time_budget_seconds"], 6 * 60 * 60)
        self.assertEqual(
            deadline(record),
            datetime.fromisoformat(record["created_at"]) + timedelta(hours=6),
        )

    def test_the_cli_flag_sets_it(self):
        printed = StringIO()
        with redirect_stdout(printed):
            code = main([
                "hunt", "init", "2",
                "--primary", "codex", "--primary-model", "fixture-primary",
                "--reviewer", "claude", "--reviewer-model", "fixture-reviewer",
                "--time-budget-hours", "6",
                "--data-root", str(self.data_root),
            ])
        record = json.loads(printed.getvalue())

        self.assertEqual(code, 0)
        self.assertEqual(record["time_budget_seconds"], 6 * 60 * 60)
        self.assertEqual(
            deadline(record),
            datetime.fromisoformat(record["created_at"]) + timedelta(hours=6),
        )

    def test_a_budget_of_nothing_is_refused(self):
        with self.assertRaises(ValueError):
            create_hunt(
                self.data_root, 1, primary="codex",
                primary_model="fixture-primary", reviewer="claude",
                reviewer_model="fixture-reviewer", time_budget_seconds=0,
            )


class FilingRecordTests(HuntTests):
    """A filed hunt has to say so, or the next session offers it again.

    https://github.com/wolfgang-aura/Mailman/issues/71
    """

    def file_one(self, count=1, url="https://github.com/example/project/pull/42"):
        record = self.new_hunt(count)
        directory = self.ready_run()
        add_run(self.data_root, record, directory.name)
        filed = record_filing(self.data_root, record, directory.name, pr_url=url)
        return record, directory, filed

    def test_recording_a_filing_closes_the_hunt_and_keeps_the_pull_request(self):
        record, directory, filed = self.file_one()
        self.assertEqual(filed["pr_number"], 42)
        self.assertEqual(filed["target"], "example/project#1")
        self.assertEqual(record["status"], "FILED")
        stored = json.loads(hunt_path(self.data_root, record["hunt_id"]).read_text(encoding="utf-8"))
        self.assertEqual(stored["runs"][0]["filed"]["pr_url"], filed["pr_url"])

    def test_filing_writes_the_ledger_entry(self):
        """https://github.com/wolfgang-aura/Mailman/issues/84

        Nothing after `handoff` required `mailman provenance`, so a filed pull
        request could be absent from `mailman contributions` entirely.
        """
        _, directory, filed = self.file_one()
        record = json.loads(
            (directory / "submission" / "provenance.json").read_text(encoding="utf-8")
        )
        self.assertEqual(record["pull_request"], filed["pr_number"])
        self.assertEqual(record["repository"], "example/project")
        self.assertEqual(unrecorded_submissions(self.data_root), [])

    def test_a_filing_whose_provenance_fails_is_not_recorded(self):
        """https://github.com/wolfgang-aura/Mailman/issues/84"""
        record = self.new_hunt()
        directory = self.ready_run()
        add_run(self.data_root, record, directory.name)

        def refuse(**_):
            raise ProvenanceError("the branch was force-pushed")

        with self.assertRaisesRegex(ProvenanceError, "force-pushed"):
            record_filing(self.data_root, record, directory.name,
                          pr_url="https://github.com/example/project/pull/42",
                          provenance_recorder=refuse)

        stored = load_hunt(self.data_root, record["hunt_id"])
        self.assertNotIn("filed", stored["runs"][0])
        self.assertEqual(stored["status"], "RUNNING")

    def test_the_packet_offers_only_the_slots_still_open(self):
        # Two requested, one filed: the packet offered two more runs, so the
        # operator was asked to approve three PRs for a hunt of two. #351.
        record = self.new_hunt(2)
        result = {"remaining": 0, "filed": 1, "runs": [
            {"run_id": "a", "ready": True, "filed": "https://github.com/example/project/pull/7"},
            {"run_id": "b", "ready": True},
            {"run_id": "c", "ready": True},
        ]}

        def packet(directories, path, **_):
            path.write_text("packet", encoding="utf-8")

        with (
            mock.patch("mailman.hunt.status", return_value=result),
            mock.patch("mailman.review_page.write_run_page"),
            mock.patch("mailman.review_packet.write_packet_page",
                       side_effect=packet) as written,
        ):
            finish(self.data_root, record)

        self.assertEqual(written.call_args.args[0], [self.data_root / "b"])

    def test_a_partly_filed_hunt_is_not_terminal(self):
        record = self.new_hunt(2)
        directory = self.ready_run()
        add_run(self.data_root, record, directory.name)
        record_filing(self.data_root, record, directory.name,
                      pr_url="https://github.com/example/project/pull/7")
        self.assertEqual(record["status"], "RUNNING")

    # --- Rolling hunts: no count, filed from a second session while running.

    def test_two_sessions_writing_one_hunt_keep_each_others_changes(self):
        # The filing session records a pull request while the coordinator
        # adds a candidate and renews its lease. Whole-record writes dropped
        # whichever change landed first.
        record = self.new_hunt()
        record["runs"].append({"run_id": "a", "target": "acme/one#1"})
        save(hunt_path(self.data_root, record["hunt_id"]), record)
        filer = load_hunt(self.data_root, record["hunt_id"])
        coordinator = load_hunt(self.data_root, record["hunt_id"])

        filer["runs"][0]["filed"] = {"pr_url": "https://github.com/acme/one/pull/9"}
        save(hunt_path(self.data_root, record["hunt_id"]), filer)
        coordinator["runs"].append({"run_id": "b", "target": "acme/two#2"})
        acquire_lease(self.data_root, coordinator, owner=record["lease"]["owner"])
        filer["last_check"] = {"ready": 1}
        save(hunt_path(self.data_root, record["hunt_id"]), filer)

        stored = json.loads(hunt_path(self.data_root, record["hunt_id"]).read_text(encoding="utf-8"))
        self.assertEqual([row["run_id"] for row in stored["runs"]], ["a", "b"])
        self.assertIn("filed", stored["runs"][0])
        self.assertEqual(stored["lease"]["expires_at"], coordinator["lease"]["expires_at"])

    def test_a_rolling_hunt_packages_each_ready_run_and_keeps_running(self):
        record = self.new_hunt(None)
        self.assertIsNone(record["requested"])
        self.assertEqual(status(self.data_root, record)["next"], "Find the next target.")
        directory = self.ready_run()
        add_run(self.data_root, record, directory.name)

        result = finish(self.data_root, record, run_id=directory.name)

        self.assertTrue(result["complete"], result["runs"])
        self.assertIsNone(result["remaining"])
        self.assertTrue(Path(result["packet"]).is_file())
        self.assertEqual(record["status"], "RUNNING")
        self.assertIn("example/project", candidate_repositories(self.data_root))

    def test_a_rolling_hunt_is_filed_from_a_second_session_and_stopped(self):
        record = self.new_hunt(None)
        directory = self.ready_run()
        add_run(self.data_root, record, directory.name)
        status(self.data_root, record)
        with redirect_stdout(StringIO()):
            code = main(["hunt", "file", record["hunt_id"], directory.name,
                         "--pr-url", "https://github.com/example/project/pull/42",
                         "--data-root", str(self.data_root)])
        self.assertEqual(code, 0)
        record = load_hunt(self.data_root, record["hunt_id"])
        self.assertEqual(record["status"], "RUNNING")

        stopped = stop(self.data_root, record, owner=record["lease"]["owner"], reason="done")

        self.assertEqual(stopped["status"], "FILED")
        self.assertNotIn("lease", record)

    def test_a_rolling_hunt_stopped_with_a_ready_run_waits_for_filing(self):
        record = self.new_hunt(None)
        directory = self.ready_run()
        add_run(self.data_root, record, directory.name)

        stopped = stop(self.data_root, record, owner=record["lease"]["owner"], reason=None)

        self.assertEqual(stopped["status"], "AWAITING_FILING_APPROVAL")
        self.assertEqual(stopped["awaiting_filing"], [directory.name])
        record_filing(self.data_root, record, directory.name,
                      pr_url="https://github.com/example/project/pull/42")
        self.assertEqual(record["status"], "FILED")

    def test_stop_closes_an_empty_rolling_hunt_and_refuses_a_counted_one(self):
        with self.assertRaisesRegex(ValueError, "finish it"):
            counted = self.new_hunt(2)
            stop(self.data_root, counted, owner=counted["lease"]["owner"], reason="x")
        record = self.new_hunt(None)
        self.assertEqual(stop(self.data_root, record, owner=record["lease"]["owner"],
                              reason="pool dry")["status"], "ABANDONED")

    def test_hunt_init_rolling_takes_no_count(self):
        printed = StringIO()
        with redirect_stdout(printed):
            code = main(["hunt", "init", "--rolling",
                         "--primary", "codex", "--primary-model", "fixture-primary",
                         "--reviewer", "claude", "--reviewer-model", "fixture-reviewer",
                         "--data-root", str(self.data_root)])
        self.assertEqual(code, 0)
        self.assertIsNone(json.loads(printed.getvalue())["requested"])


    def test_a_filing_recorded_without_its_commit_can_add_it(self):
        # prefect's PR was recorded without --commit, then "already recorded"
        # refused the commit for good. Mailman #250.
        record = self.new_hunt(2)
        directory = self.ready_run()
        add_run(self.data_root, record, directory.name)
        url = "https://github.com/example/project/pull/7"
        record_filing(self.data_root, record, directory.name, pr_url=url)
        filed = record_filing(self.data_root, record, directory.name,
                              pr_url=url, commit="b09c315")
        self.assertEqual(filed["commit"], "b09c315")
        stored = load_hunt(self.data_root, record["hunt_id"])
        self.assertEqual(stored["runs"][0]["filed"]["commit"], "b09c315")
        with self.assertRaises(ValueError):
            record_filing(self.data_root, record, directory.name,
                          pr_url=url, commit="another")

    def test_a_pull_request_on_another_repository_is_refused(self):
        record = self.new_hunt()
        directory = self.ready_run()
        add_run(self.data_root, record, directory.name)
        with self.assertRaises(ValueError) as caught:
            record_filing(self.data_root, record, directory.name,
                          pr_url="https://github.com/other/thing/pull/42")
        self.assertIn("the run targets", str(caught.exception))

    def test_a_filed_target_cannot_join_a_new_hunt(self):
        _, directory, _ = self.file_one()
        second = self.new_hunt()
        with self.assertRaises(ValueError) as caught:
            add_run(self.data_root, second, directory.name)
        self.assertIn("already filed", str(caught.exception))

    def test_status_reports_the_filing_and_leaves_the_record_alone(self):
        record, directory, filed = self.file_one()
        stored = json.loads(hunt_path(self.data_root, record["hunt_id"]).read_text(encoding="utf-8"))
        result = status(self.data_root, record)
        self.assertEqual(result["filed"], 1)
        self.assertIs(result["persisted"], False)
        after = json.loads(hunt_path(self.data_root, record["hunt_id"]).read_text(encoding="utf-8"))
        self.assertEqual(stored, after)

    def test_finish_refuses_to_rewrite_a_filed_hunt(self):
        record, _, _ = self.file_one()
        with self.assertRaises(ValueError) as caught:
            finish(self.data_root, record)
        self.assertIn("already open", str(caught.exception))

    def partly_filed(self):
        """A hunt of two: one candidate filed, one live and ready.

        The filed candidate is a bare run on a second target. What matters is
        that `status` never asks it what to do next, and nothing about it is
        ready enough to answer.
        """
        record = self.new_hunt(2)
        live = self.ready_run()
        add_run(self.data_root, record, live.name)
        run, filed_directory = self.make_run()
        run.primary = AgentConfig("codex", "fixture-primary")
        run.reviewer = AgentConfig("claude", "fixture-reviewer")
        run.issue = "https://github.com/example/project/issues/2"
        write_run(run, filed_directory)
        add_run(self.data_root, record, run.run_id)
        record_filing(self.data_root, record, run.run_id,
                      pr_url="https://github.com/example/project/pull/42",
                      provenance_recorder=lambda **_: None)
        return record, filed_directory, live

    def test_a_filed_candidate_counts_and_is_never_rechecked(self):
        """https://github.com/wolfgang-aura/Mailman/issues/97

        Rechecking a filed run finds its own pull request and reports the
        candidate as replaceable, so a two-PR hunt with one filed read
        `ready 0, remaining 2`.
        """
        record, filed_directory, live = self.partly_filed()
        import mailman.hunt as hunt_module
        asked = []
        original = hunt_module.next_action

        def spy(directory):
            asked.append(directory.name)
            return original(directory)

        with mock.patch.object(hunt_module, "next_action", spy):
            result = status(self.data_root, record)

        self.assertEqual(asked, [live.name])
        self.assertEqual(result["ready"], 2)
        self.assertEqual(result["remaining"], 0)
        row = next(row for row in result["runs"]
                   if row["run_id"] == filed_directory.name)
        self.assertTrue(row["ready"])
        self.assertEqual(row["disposition"], "FILED")
        self.assertEqual(row["stage"], "filed")
        self.assertEqual(row["filed"], "https://github.com/example/project/pull/42")

    def test_the_packet_holds_only_the_candidate_still_to_be_filed(self):
        """https://github.com/wolfgang-aura/Mailman/issues/97"""
        record, filed_directory, live = self.partly_filed()

        result = finish(self.data_root, record)

        self.assertTrue(result["complete"], result)
        page = Path(result["packet"]).read_text(encoding="utf-8")
        self.assertIn(live.name, page)
        self.assertNotIn(filed_directory.name, page)

    def test_a_run_does_not_match_its_own_filed_pull_request(self):
        """https://github.com/wolfgang-aura/Mailman/issues/97"""
        _, directory, filed = self.file_one()
        path = directory / "duplicate-search.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["matches"] = [{"number": filed["pr_number"], "state": "open",
                               "pull_request": True, "title": "Fix synthetic fixture",
                               "references_issue": True}]
        path.write_text(json.dumps(payload), encoding="utf-8")

        assessment = assess_target(directory)

        self.assertEqual(assessment.open_attempts, [])
        self.assertNotIn(OPEN_PULL_REQUEST, assessment.blocking)
        self.assertTrue(next_action(directory)["ready"])

    def test_readiness_checks_accumulate_instead_of_replacing_each_other(self):
        record = self.new_hunt()
        status(self.data_root, record)
        status(self.data_root, record)
        stored = json.loads(hunt_path(self.data_root, record["hunt_id"]).read_text(encoding="utf-8"))
        self.assertEqual(len(stored["checks"]), 2)
        self.assertEqual(stored["last_check"]["checked_at"], stored["checks"][-1]["checked_at"])


class TargetClaimTests(HuntTests):
    """One data root, two hunts, one candidate pool.

    https://github.com/wolfgang-aura/Mailman/issues/73
    """

    def test_a_live_hunt_holds_its_target_against_a_sibling(self):
        first = self.new_hunt()
        directory = self.ready_run()
        add_run(self.data_root, first, directory.name)
        second = self.new_hunt()
        with self.assertRaises(ValueError) as caught:
            add_run(self.data_root, second, directory.name)
        self.assertIn(first["hunt_id"], str(caught.exception))

    def test_targets_lists_what_every_hunt_in_the_root_is_working_on(self):
        record = self.new_hunt()
        directory = self.ready_run()
        add_run(self.data_root, record, directory.name)
        claims = target_claims(self.data_root)
        self.assertEqual([claim["target"] for claim in claims], ["example/project#1"])
        self.assertTrue(claims[0]["live"])

    def test_an_expired_lease_does_not_read_as_running(self):
        record = self.new_hunt()
        directory = self.ready_run()
        add_run(self.data_root, record, directory.name)
        acquire_lease(self.data_root, record, owner=record["lease"]["owner"], minutes=-1)
        self.assertEqual(effective_status(record), "ABANDONED")
        self.assertIsNone(holding_hunt(self.data_root, "example/project#1"))

    def test_a_ready_candidate_keeps_its_claim_while_it_waits_for_filing(self):
        """https://github.com/wolfgang-aura/Mailman/issues/209

        py-shiny#2497 was READY at 10:05 and waited for the operator to file
        it. The lease lapsed at 11:36, the hunt read ABANDONED, and its target
        went back to the pool while the pull request was being opened.
        """
        record = self.new_hunt()
        directory = self.ready_run()
        add_run(self.data_root, record, directory.name)
        self.assertEqual(status(self.data_root, record)["ready"], 1)
        acquire_lease(self.data_root, record, owner=record["lease"]["owner"], minutes=-1)
        stored = load_hunt(self.data_root, record["hunt_id"])
        self.assertEqual(effective_status(stored), "AWAITING_FILING")
        claim = holding_hunt(self.data_root, "example/project#1")
        self.assertIsNotNone(claim)
        self.assertEqual(claim["hunt_id"], record["hunt_id"])
        # Waiting on a person is not a coordinator: nothing may restart its runs.
        self.assertNotEqual(effective_status(stored), "RUNNING")
        # Once it is filed, the owner records it without renewing the lease.
        record_filing(self.data_root, stored, directory.name,
                      pr_url="https://github.com/example/project/pull/9",
                      provenance_recorder=lambda **_: None)
        self.assertEqual(
            load_hunt(self.data_root, record["hunt_id"])["runs"][0]["filed"]["pr_number"], 9
        )

    def test_an_expired_lease_is_not_free_to_adopt(self):
        """https://github.com/wolfgang-aura/Mailman/issues/76

        An abandoned hunt used to be picked up silently, which made continuing
        somebody else's candidates the default and left the operator as the
        only gate on whether that was the right hunt at all.
        """
        record = self.new_hunt()
        first = record["lease"]["owner"]
        acquire_lease(self.data_root, record, owner=first, minutes=-1)
        self.assertEqual(effective_status(record), "ABANDONED")
        with self.assertRaisesRegex(ValueError, "decision, not a default"):
            acquire_lease(self.data_root, record, owner="second-coordinator")
        with self.assertRaisesRegex(ValueError, "was abandoned by"):
            require_lease(record, "second-coordinator")
        # The coordinator that owns it may still resume its own hunt.
        acquire_lease(self.data_root, record, owner=first)
        require_lease(record, first)
        # Anyone else has to say out loud that they are adopting it.
        acquire_lease(self.data_root, record, owner=first, minutes=-1)
        acquire_lease(self.data_root, record, owner="second-coordinator",
                      takeover_reason="its coordinator was asked for other targets")
        self.assertEqual(load_hunt(self.data_root, record["hunt_id"])["lease"]["owner"],
                         "second-coordinator")

    def test_an_abandoned_hunt_closes_and_stays_closed(self):
        """https://github.com/wolfgang-aura/Mailman/issues/77

        A hunt that is over and was never filed stored RUNNING for ever, so
        `hunt list` only grew and every later session re-judged the same
        wreckage.
        """
        record = self.new_hunt()
        directory = self.ready_run()
        add_run(self.data_root, record, directory.name)
        with self.assertRaisesRegex(ValueError, "--reason"):
            abandon(self.data_root, record, reason="")
        # A live hunt is somebody's work in progress.
        with self.assertRaisesRegex(ValueError, "owned by"):
            abandon(self.data_root, record, reason="not mine", owner="second-coordinator")
        closed = abandon(self.data_root, record, reason="its targets were not finance",
                         owner=record["lease"]["owner"])
        self.assertEqual(closed["reason"], "its targets were not finance")
        stored = load_hunt(self.data_root, record["hunt_id"])
        self.assertEqual(stored["status"], "ABANDONED")
        self.assertEqual(effective_status(stored), "ABANDONED")
        self.assertNotIn("lease", stored)
        # Its targets go back to the pool, and its record stops accepting work.
        self.assertIsNone(holding_hunt(self.data_root, "example/project#1"))
        with self.assertRaisesRegex(ValueError, "ABANDONED"):
            add_run(self.data_root, stored, directory.name)
        with self.assertRaisesRegex(ValueError, "ABANDONED"):
            finish(self.data_root, stored)
        with self.assertRaisesRegex(ValueError, "ABANDONED"):
            record_filing(self.data_root, stored, directory.name,
                          pr_url="https://github.com/example/project/pull/1")

    def test_a_hunt_pinned_to_an_old_procedure_can_still_be_closed(self):
        """https://github.com/wolfgang-aura/Mailman/issues/77

        The hunts most in need of closing are the oldest, and those are the
        ones pinned to a superseded procedure. Demanding a refresh before they
        could be closed is how the wreckage stayed in `hunt list`.
        """
        record = self.new_hunt()
        record["procedure_sha256"] = "0" * 64
        save(hunt_path(self.data_root, record["hunt_id"]), record)
        with self.assertRaisesRegex(ValueError, "procedure changed"):
            load_hunt(self.data_root, record["hunt_id"])
        stale = load_hunt(self.data_root, record["hunt_id"], require_procedure=False)
        abandon(self.data_root, stale, reason="superseded", owner=stale["lease"]["owner"])
        self.assertEqual(load_hunt(self.data_root, record["hunt_id"])["status"], "ABANDONED")

    def test_a_dropped_target_is_released(self):
        record = self.new_hunt()
        directory = self.ready_run()
        add_run(self.data_root, record, directory.name)
        record["runs"][0].update(dropped=True, reason="issue-assigned", evidence="assignee")
        save(hunt_path(self.data_root, record["hunt_id"]), record)
        self.assertIsNone(holding_hunt(self.data_root, "example/project#1"))


class CompactViewTests(HuntTests):
    """What a coordinator reads, it re-sends on every later turn."""

    def test_replaced_candidates_collapse_to_a_count(self):
        record = self.new_hunt()
        directory = self.ready_run()
        add_run(self.data_root, record, directory.name)
        record["runs"][0].update(dropped=True, reason="issue-assigned",
                                 evidence="x" * 4000)
        save(hunt_path(self.data_root, record["hunt_id"]), record)
        result = status(self.data_root, record)
        view = compact(result)
        self.assertEqual(view["replaced"], {
            "count": 1, "reasons": {"issue-assigned": 1},
            "detail": "in the hunt record; pass --full to print it"})
        self.assertEqual(view["runs"], [])
        self.assertLess(len(json.dumps(view)), len(json.dumps(result)) / 4)
        self.assertIn("x" * 4000, json.dumps(result))

    def test_a_live_row_keeps_its_action_and_truncates_long_detail(self):
        record = self.new_hunt()
        result = {"runs": [{"run_id": "r", "ready": False, "stage": "screen",
                            "action": "mailman screen-target a/b",
                            "detail": "y" * 500, "disposition": "REPAIR"}]}
        row = compact(result)["runs"][0]
        self.assertEqual(row["action"], "mailman screen-target a/b")
        self.assertTrue(row["detail"].endswith("..."))
        self.assertEqual(len(row["detail"]), 203)


class PreFilingRefreshTests(HuntTests):
    """The candidates about to be pushed are the ones refresh used to skip.

    https://github.com/wolfgang-aura/Mailman/issues/41
    """

    def test_refresh_skips_a_ready_candidate_mid_hunt(self):
        record = self.new_hunt()
        directory = self.ready_run()
        add_run(self.data_root, record, directory.name)
        self.assertEqual(status(self.data_root, record)["ready"], 1)
        result = refresh(self.data_root, record)
        self.assertEqual(result["refreshed"], [])

    def test_include_ready_refreshes_the_candidate_about_to_be_filed(self):
        record = self.new_hunt()
        directory = self.ready_run()
        add_run(self.data_root, record, directory.name)
        self.assertEqual(status(self.data_root, record)["ready"], 1)
        result = refresh(self.data_root, record, include_ready=True)
        self.assertEqual([row["run_id"] for row in result["refreshed"]], [directory.name])

    def test_a_filed_run_is_not_refreshed(self):
        # finish's pre-filing pass reran a filed run's search, which then found
        # our own pull request and overwrote the evidence it was filed on.
        # Mailman #343.
        record = self.new_hunt(2)
        directory = self.ready_run()
        add_run(self.data_root, record, directory.name)
        record_filing(self.data_root, record, directory.name,
                      pr_url="https://github.com/example/project/pull/7")
        before = (directory / "duplicate-search.json").read_bytes()
        result = refresh(self.data_root, record, include_ready=True)
        self.assertEqual(result["refreshed"], [])
        self.assertEqual((directory / "duplicate-search.json").read_bytes(), before)

    def test_the_repeated_search_keeps_its_limit_and_issue_symbols(self):
        # The pre-filing repeat dropped the symbols read out of the issue
        # body and the recorded limit, so it searched less. Mailman #355.
        record = self.new_hunt()
        directory = self.ready_run()
        add_run(self.data_root, record, directory.name)
        path = directory / "duplicate-search.json"
        search = json.loads(path.read_text(encoding="utf-8"))
        search.update(query="fixture defect", issue_number=1, symbols=["fix"],
                      issue_symbols=["_handle_upserts"], limit=80)
        path.write_text(json.dumps(search), encoding="utf-8")
        fresh = {"success": True, "complete": True, "match_count": 0, "decided_by": "broad"}
        with mock.patch("mailman.submission.record_duplicate_search",
                        return_value=fresh) as searched:
            refresh(self.data_root, record, include_ready=True)

        self.assertEqual(searched.call_args.kwargs["issue_symbols"], ["_handle_upserts"])
        self.assertEqual(searched.call_args.kwargs["limit"], 80)


class PrescreenRecordTests(OrchestratorHarness):
    """A hunt keeps its prescreens, and the next target comes from screens.

    Hunt 20260916T165859Z-0d3481 ran 20 prescreens and kept none of them.
    https://github.com/wolfgang-aura/Mailman/issues/102
    """

    new_hunt = HuntTests.new_hunt

    def _prescreen(self, slug, number, verdict, blocking=()):
        return {"repository": slug, "issue_number": number, "verdict": verdict,
                "blocking": list(blocking), "screened_at": "2026-09-28T01:00:00+00:00"}

    def _screen(self, slug, numbers, *, verdict="pass", days_old=0, flags=None,
                window_days=None, policy=None):
        from mailman.screen import (FRESHNESS_WINDOW_DAYS, ISSUE_WINDOW_DAYS,
                                    RESPONSIVENESS_WINDOW_DAYS)
        path = screen_path(self.data_root, slug)
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = [{"number": n, "title": f"bug {n}", "age_days": 3, "score": 1,
                 "reasons": ["no-linked-pr"], **((flags or {}).get(n) or {})}
                for n in numbers]
        path.write_text(json.dumps({
            "repository": slug, "success": True, "verdict": verdict,
            "screened_at": (datetime.now(UTC) - timedelta(days=days_old)).isoformat(),
            "window_days": FRESHNESS_WINDOW_DAYS if window_days is None else window_days,
            "issue_window_days": ISSUE_WINDOW_DAYS,
            "responsiveness_days": RESPONSIVENESS_WINDOW_DAYS,
            "gates": [{"name": "saturation", "data": {"shortlist": rows}},
                      {"name": "policy", "data": policy if policy is not None else
                       {"requires_cla": False, "cla_checks_read": True}}],
        }), encoding="utf-8")

    def test_a_prescreen_is_appended_to_the_hunt_and_counted_in_status(self):
        record = self.new_hunt()
        record_prescreen(self.data_root, record,
                         self._prescreen("acme/widgets", 7, "reject", ["open-pull-request"]))
        record_prescreen(self.data_root, record, self._prescreen("acme/widgets", 9, "pass"))
        record_prescreen(self.data_root, record, self._prescreen("acme/widgets", 7, "pass"))

        saved = load_hunt(self.data_root, record["hunt_id"])
        self.assertEqual(
            saved["prescreens"],
            [
                {"target": "acme/widgets#9", "verdict": "pass", "rejects": [],
                 "screened_at": "2026-09-28T01:00:00+00:00"},
                {"target": "acme/widgets#7", "verdict": "pass", "rejects": [],
                 "screened_at": "2026-09-28T01:00:00+00:00"},
            ],
        )
        self.assertEqual(status(self.data_root, saved)["prescreens"],
                         {"screened": 2, "passed": 2})

    def test_prescreen_with_hunt_writes_the_verdict_to_that_hunt(self):
        record = self.new_hunt()
        verdict = self._prescreen("acme/widgets", 7, "reject", ["open-pull-request"])
        with mock.patch("mailman.prescreen.prescreen_issue", return_value=verdict), \
                redirect_stdout(StringIO()):
            code = main(["prescreen", "acme/widgets#7", "--data-root", str(self.data_root),
                         "--hunt", record["hunt_id"]])

        self.assertEqual(code, 1)
        saved = load_hunt(self.data_root, record["hunt_id"])
        self.assertEqual([row["target"] for row in saved["prescreens"]], ["acme/widgets#7"])

    def test_a_reject_keeps_its_reasons(self):
        record = self.new_hunt()
        row = record_prescreen(self.data_root, record, self._prescreen(
            "acme/widgets", 7, "reject", ["open-pull-request"]))

        self.assertEqual(row["rejects"], ["open-pull-request"])
        self.assertEqual(status(self.data_root, record)["prescreens"],
                         {"screened": 1, "passed": 0})

    def test_workable_targets_skip_prescreened_stale_failed_and_held_repositories(self):
        from mailman.prescreen import prescreen_path
        self._screen("acme/widgets", [1, 2])
        self._screen("acme/stale", [3], days_old=30)
        self._screen("acme/failed", [4], verdict="fail")
        self._screen("acme/held", [5])
        done = prescreen_path(self.data_root, "acme/widgets", 2)
        done.parent.mkdir(parents=True, exist_ok=True)
        done.write_text("{}", encoding="utf-8")

        rows = workable_targets(self.data_root, held_repositories={"acme/held"})

        self.assertEqual([row["target"] for row in rows], ["acme/widgets#1"])
        self.assertEqual(rows[0]["title"], "bug 1")

    def _engagement_screens(self):
        # acme/old predates f94d449: its rows carry no flags at all.
        self._screen("acme/old", [1], days_old=1)
        self._screen("acme/new", [2, 3, 4, 5], flags={
            2: {"maintainer_filed": False, "maintainer_replied": False},
            3: {"maintainer_filed": False, "maintainer_replied": True,
                "maintainer_disputed": None},
            4: {"maintainer_filed": True, "maintainer_replied": None},
            5: {"maintainer_filed": False, "maintainer_replied": None},
        })

    def test_workable_targets_rank_maintainer_engagement_first(self):
        self._engagement_screens()

        rows = workable_targets(self.data_root, held_repositories=set())

        self.assertEqual(
            [(row["target"], row["engagement"]) for row in rows],
            [("acme/new#3", "engaged"), ("acme/new#4", "engaged"),
             ("acme/new#5", "unknown"), ("acme/old#1", "unknown"),
             ("acme/new#2", "not-engaged")],
        )
        by_target = {row["target"]: row for row in rows}
        self.assertTrue(by_target["acme/new#3"]["maintainer_replied"])
        self.assertTrue(by_target["acme/old#1"]["stale_screen"])
        self.assertFalse(by_target["acme/new#5"]["stale_screen"])

    def test_workable_targets_say_whether_the_repository_needs_a_cla(self):
        # cloud-custodian's screen predated #440 and hid EasyCLA. #442.
        self._screen("acme/cla", [1], policy={"requires_cla": True, "cla_checks_read": True})
        self._screen("acme/free", [2])
        self._screen("acme/old", [3], policy={"requires_cla": False})

        rows = {row["target"]: row["requires_cla"]
                for row in workable_targets(self.data_root, held_repositories=set())}

        self.assertEqual(rows, {"acme/cla#1": True, "acme/free#2": False, "acme/old#3": None})
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), mock.patch("sys.stderr", err):
            main(["hunt", "targets", "--data-root", str(self.data_root)])
        line = next(text for text in err.getvalue().splitlines()
                    if "predate CLA check detection" in text)
        self.assertIn("acme/old --refresh", line)
        self.assertNotIn("acme/free", line)

    def test_workable_targets_engaged_only(self):
        self._engagement_screens()

        rows = workable_targets(self.data_root, held_repositories=set(),
                                engaged_only=True)

        self.assertEqual([row["target"] for row in rows], ["acme/new#3", "acme/new#4"])

    def test_workable_targets_count_a_label_and_drop_a_rival(self):
        # plotly/dash triages by label alone; a cross-referenced open pull
        # request means somebody is already fixing it.
        self._screen("acme/labels", [6, 7, 8], flags={
            6: {"maintainer_filed": False, "maintainer_replied": False,
                "maintainer_labelled": True, "rival_pull_requests": []},
            7: {"maintainer_filed": False, "maintainer_replied": True,
                "maintainer_labelled": False,
                "rival_pull_requests": ["acme/labels#40"]},
            8: {"maintainer_filed": False, "maintainer_replied": False,
                "maintainer_labelled": False, "rival_pull_requests": []},
        })

        rows = workable_targets(self.data_root, held_repositories=set(),
                                engaged_only=True)

        self.assertEqual([row["target"] for row in rows], ["acme/labels#6"])
        self.assertTrue(rows[0]["maintainer_labelled"])

    def test_workable_targets_skip_a_row_whose_timeline_failed(self):
        # Mailman #339: a failed timeline read is no answer about rivals.
        self._screen("acme/unread", [6, 7], flags={
            6: {"maintainer_filed": True, "rival_pull_requests": [],
                "timeline_read": True},
            7: {"maintainer_filed": True, "rival_pull_requests": None,
                "timeline_read": False},
        })

        rows = workable_targets(self.data_root, held_repositories=set())

        self.assertEqual([row["target"] for row in rows], ["acme/unread#6"])

    def test_workable_targets_rank_a_young_bug_label_first_within_a_group(self):
        # certbot's 581-day-old chore led the engaged list on 2026-09-29 while
        # two-day-old bug-labelled issues sat far below. Mailman #189.
        engaged = {"maintainer_filed": False, "maintainer_replied": True,
                   "maintainer_labelled": True, "rival_pull_requests": []}
        self._screen("acme/rank", [1, 2, 3, 4], flags={
            1: {**engaged, "age_days": 581},
            2: {**engaged, "age_days": 40, "labels": ["bug"]},
            3: {**engaged, "age_days": 2, "labels": [{"name": "type: regression"}]},
            4: {**engaged, "age_days": 9},
        })

        rows = workable_targets(self.data_root, held_repositories=set())

        self.assertEqual([row["target"] for row in rows],
                         ["acme/rank#3", "acme/rank#2", "acme/rank#4", "acme/rank#1"])
        self.assertTrue(rows[0]["bug_labelled"])
        self.assertFalse(rows[2]["bug_labelled"])

    def test_workable_targets_skip_requests_a_stored_screen_still_holds(self):
        # Shortlists recorded before eb317af kept RFCs, feature requests and
        # release trackers, and `hunt targets` served them. Mailman #188.
        self._screen("acme/requests", [1, 2, 3, 4, 5, 6], flags={
            1: {"title": "RFC: Retrieval Diagnostics API"},
            2: {"title": "[RFC, I can do a PR] Make lora_alpha a float"},
            3: {"title": "Track ty readiness as the primary type checker"},
            4: {"title": "2.8.0 release"},
            5: {"title": "Crash on empty input", "labels": ["type:feature"]},
            6: {"title": "Feature flags crash on empty input"},
        })

        rows = workable_targets(self.data_root, held_repositories=set())

        self.assertEqual([row["target"] for row in rows], ["acme/requests#6"])

    def test_workable_targets_skip_rows_a_label_declines_or_leaves_undecided(self):
        # pymc-marketing#2659 carried `bug` and `wontfix` and was offered as
        # an engaged, bug-labelled target. Mailman #371.
        self._screen("acme/declined", [1, 2, 3, 4, 5], flags={
            1: {"title": "Plot crashes", "labels": ["bug", "wontfix"]},
            2: {"title": "Crash on load", "labels": [{"name": "duplicate"}]},
            3: {"title": "Crash on save", "labels": ["bug", "needs discussion"]},
            4: {"title": "Crash on exit", "labels": ["cannot reproduce"]},
            5: {"title": "Crash on empty input", "labels": ["bug"]},
        })

        rows = workable_targets(self.data_root, held_repositories=set())

        self.assertEqual([row["target"] for row in rows], ["acme/declined#5"])

    def test_workable_targets_skip_issues_a_staff_team_reserves(self):
        # zenml's shortlist was sixteen `core-team` and `planned`/`gtm-team`
        # roadmap items that outside PRs do not take. Mailman #264.
        self._screen("acme/staff", [1, 2, 3, 4, 5, 6], flags={
            1: {"title": "Improve stopping behavior", "labels": ["core-team"]},
            2: {"title": "Decrease CLI warnings", "labels": ["planned", "gtm-team"]},
            3: {"title": "Crash on stop", "labels": [{"name": "team: platform"}]},
            4: {"title": "Crash on start", "labels": ["roadmap"]},
            5: {"title": "Crash on resume", "labels": ["internal"]},
            6: {"title": "Crash on empty input", "labels": ["bug", "teamwork"]},
        })

        rows = workable_targets(self.data_root, held_repositories=set())

        self.assertEqual([row["target"] for row in rows], ["acme/staff#6"])

    def test_workable_targets_rank_a_disputed_reply_last_and_out_of_engaged(self):
        # copier#2436: the maintainer's reply was "could not reproduce", and
        # --engaged-only offered it as triaged. Mailman #150.
        self._screen("acme/disputes", [9, 10], flags={
            9: {"maintainer_filed": False, "maintainer_replied": True,
                "maintainer_disputed": "I could not reproduce this on main."},
            10: {"maintainer_filed": False, "maintainer_replied": False},
        })

        every = workable_targets(self.data_root, held_repositories=set())
        engaged = workable_targets(self.data_root, held_repositories=set(),
                                   engaged_only=True)

        self.assertEqual(
            [(row["target"], row["engagement"]) for row in every],
            [("acme/disputes#10", "not-engaged"), ("acme/disputes#9", "disputed")],
        )
        self.assertEqual(engaged, [])

    def test_a_reply_from_a_screen_without_the_dispute_flag_is_unknown(self):
        # jedi#2077's owner "couldn't reproduce", and a screen from before
        # #150 offered it as engaged. Mailman #192.
        self._screen("acme/prior", [11, 12], flags={
            11: {"maintainer_filed": False, "maintainer_replied": True},
            12: {"maintainer_filed": False, "maintainer_replied": True,
                 "maintainer_disputed": None},
        })

        rows = workable_targets(self.data_root, held_repositories=set())

        self.assertEqual(
            [(row["target"], row["engagement"]) for row in rows],
            [("acme/prior#12", "engaged"), ("acme/prior#11", "unknown")],
        )

    def test_stale_screen_warning_counts_rows_and_names_the_refresh(self):
        self._engagement_screens()
        rows = workable_targets(self.data_root, held_repositories=set())

        warning = stale_screen_warning(rows)

        self.assertIn("1 workable row(s)", warning)
        self.assertIn("mailman screen-target acme/old --refresh", warning)
        self.assertNotIn("acme/new", warning)
        self.assertIsNone(stale_screen_warning(
            [row for row in rows if not row["stale_screen"]]))

    def test_hunt_targets_cli_filters_and_warns(self):
        self._engagement_screens()
        for flag, expected in (([], 5), (["--engaged-only"], 2)):
            out, err = StringIO(), StringIO()
            with redirect_stdout(out), mock.patch("sys.stderr", err):
                code = main(["hunt", "targets", "--data-root",
                             str(self.data_root), *flag])
            self.assertEqual(code, 0)
            payload = json.loads(out.getvalue())
            self.assertEqual(len(payload["workable"]), expected)
            self.assertEqual(payload["workable"][0]["target"], "acme/new#3")
            # The stale warning still counts rows the filter dropped.
            self.assertEqual(len(payload["warnings"]), 1)
            self.assertIn("acme/old --refresh", err.getvalue())

    def test_a_repository_with_a_filed_pull_request_not_seen_closed_is_held(self):
        record = self.new_hunt()
        record["runs"].append({"run_id": "r1", "filed": {
            "repository": "acme/held", "pr_number": 12, "pr_url":
            "https://github.com/acme/held/pull/12"}})
        save(hunt_path(self.data_root, record["hunt_id"]), record)

        self.assertEqual(open_pull_request_repositories(self.data_root), {"acme/held"})
        watch = self.data_root.parent / "filed-watch.json"
        watch.write_text(json.dumps({"rows": [
            {"repository": "acme/held", "pull_request": 12, "state": "closed"}
        ]}), encoding="utf-8")
        self.assertEqual(open_pull_request_repositories(self.data_root), set())

class _SearchGh:
    """Answers `repos/R/issues` with canned items, one list per repository."""

    def __init__(self, answers, timelines=None):
        self.answers = list(answers)
        self.timelines = timelines or {}
        self.paths = []
        self.sleeps = []
        self.failures = []

    def sleep(self, seconds):
        self.sleeps.append(seconds)

    def json(self, path):
        if "/timeline" in path:
            return self.timelines.get(path.split("?")[0], [])
        self.paths.append(path)
        answer = self.answers.pop(0) if self.answers else []
        if answer is None:
            self.failures.append(path)
        return answer


def _item(slug, number, *, comments=0, created="2026-09-20T00:00:00Z", labels=("bug",)):
    return {"repository_url": f"https://api.github.com/repos/{slug}",
            "number": number, "title": f"bug {number}", "comments": comments,
            "labels": [{"name": name} for name in labels],
            "created_at": created, "author_association": "NONE",
            "user": {"login": "reporter"},
            "html_url": f"https://github.com/{slug}/issues/{number}"}


class SweepTests(OrchestratorHarness):
    """Fresh issues in passing repositories, read with the core issues endpoint.

    Hunt 20260929T150205Z-08d690 had 83 passing screens and four workable
    rows, because a shortlist is frozen when its screen is written. Mailman #226.
    """

    new_hunt = HuntTests.new_hunt
    _screen = PrescreenRecordTests._screen

    def test_one_paced_core_read_per_passing_repository(self):
        from mailman.hunt import sweep_fresh_issues
        for slug in ("acme/a", "acme/b", "acme/c"):
            self._screen(slug, [])
        self._screen("acme/month", [], days_old=20)
        self._screen("acme/old", [], days_old=40)
        self._screen("acme/failed", [], verdict="fail")
        self._screen("acme/held", [])
        gh = _SearchGh([])

        result = sweep_fresh_issues(
            self.data_root, gh, held_repositories={"acme/held"},
            since_days=60, now=datetime(2026, 9, 30, tzinfo=UTC))

        read = sorted(path.split("/issues?")[0] for path in gh.paths)
        self.assertEqual(read, ["repos/acme/a", "repos/acme/b", "repos/acme/c",
                                "repos/acme/month"])
        for part in ("state=open", "since=2026-08-01T00:00:00Z", "per_page=100"):
            self.assertIn(part, gh.paths[0])
        self.assertEqual(len(gh.sleeps), 3)
        self.assertEqual(result["queries"], 4)
        self.assertEqual(result["repositories"], 4)

    def test_labels_pull_requests_assignees_and_old_issues_are_judged_locally(self):
        # pylint's "Needs PR" was invisible to the search API, and search
        # refused its third call. Mailman #259.
        from mailman.hunt import sweep_fresh_issues
        self._screen("acme/a", [])
        pull = {**_item("acme/a", 5), "pull_request": {"url": "x"}}
        taken = {**_item("acme/a", 6), "assignee": {"login": "someone"}}
        gh = _SearchGh([[
            _item("acme/a", 1, labels=("False Positive 🦟", "Needs PR")),
            _item("acme/a", 2, labels=("Needs PR",)),
            _item("acme/a", 3, labels=("Enhancement ✨", "Needs PR")),
            _item("acme/a", 4, labels=("bug", "Needs decision :lock:")),
            pull, taken,
            _item("acme/a", 7, created="2026-07-01T00:00:00Z"),
            _item("acme/a", 8, labels=()),
            _item("acme/a", 9, labels=("Issue Type: Bug Report",)),
            _item("acme/a", 10, labels=("type:bug", "status:cannot-reproduce")),
            _item("acme/a", 11, labels=("upstream bug", "fix developed")),
            _item("acme/a", 12, labels=("bug", "status:needs-product-approval")),
            _item("acme/a", 13, labels=("upstream bug",)),
            # mlflow's undecided and already-closed rows. Mailman #265.
            _item("acme/a", 14, labels=("bug", "needs design", "has-closing-pr")),
            _item("acme/a", 15, labels=("bug", "needs author feedback")),
            _item("acme/a", 16, labels=("bug", "has-closing-pr")),
        ]])

        result = sweep_fresh_issues(self.data_root, gh, held_repositories=set(),
                                    now=datetime(2026, 9, 30, tzinfo=UTC))

        self.assertEqual(sorted(row["target"] for row in result["rows"]),
                         ["acme/a#1", "acme/a#2", "acme/a#9"])

    def test_a_pass_read_under_a_narrower_freshness_window_is_still_swept(self):
        # #284 widened the window to 60 days and the next sweep read one
        # repository of about 150, saying nothing. Mailman #286.
        from mailman.hunt import sweep_fresh_issues
        from mailman.screen import FRESHNESS_WINDOW_DAYS
        self._screen("acme/narrow", [], window_days=FRESHNESS_WINDOW_DAYS - 15)
        self._screen("acme/wide", [], window_days=FRESHNESS_WINDOW_DAYS + 30)
        self._screen("acme/now", [])
        gh = _SearchGh([])

        result = sweep_fresh_issues(self.data_root, gh, held_repositories=set(),
                                    now=datetime.now(UTC))

        self.assertEqual(result["repositories"], 2)
        self.assertTrue(any("acme/narrow" in path for path in gh.paths))
        self.assertEqual(result["stale_screens"], ["acme/wide"])

    def test_a_repository_a_hunt_dropped_as_closed_is_not_swept(self):
        # streamlit#17030 was dropped as target-closed-to-outside-prs, and the
        # next hunt was offered streamlit again. Mailman #300.
        from mailman.hunt import closed_repositories, save, hunt_path, sweep_fresh_issues
        for slug in ("acme/open", "acme/closed", "acme/banned", "acme/cheap"):
            self._screen(slug, [])
        record = self.new_hunt(3)
        record["runs"] = [
            {"run_id": "r1", "target": "acme/closed#1", "dropped": True,
             "reason": "target-closed-to-outside-prs", "evidence": "CONTRIBUTING.md"},
            {"run_id": "r2", "target": "acme/banned#2", "dropped": True,
             "reason": "maintainers refuse generated code", "evidence": "x",
             "closes_repository": True},
            {"run_id": "r3", "target": "acme/cheap#3", "dropped": True,
             "reason": "already-fixed-upstream", "evidence": "x"},
        ]
        save(hunt_path(self.data_root, record["hunt_id"]), record)

        self.assertEqual(closed_repositories(self.data_root), {"acme/closed", "acme/banned"})
        gh = _SearchGh([])
        sweep_fresh_issues(self.data_root, gh, held_repositories=set(),
                           now=datetime.now(UTC))
        read = sorted(path.split("/issues?")[0] for path in gh.paths)
        self.assertEqual(read, ["repos/acme/cheap", "repos/acme/open"])

    def test_other_invitation_labels_are_admitted(self):
        # bleachbit's maintainer filed #2307 as `status:ready-for-dev`; only a
        # hand-run label pool found it. Mailman #285.
        from mailman.hunt import sweep_labels_admit
        for labels in (("new", "status:ready-for-dev"), ("triaged",),
                       ("PR welcome",), ("contributions welcome",), ("approved",)):
            with self.subTest(labels=labels):
                self.assertTrue(sweep_labels_admit(list(labels)))
        for labels in (("enhancement", "status:ready-for-dev"), ("needs triage",),
                       ("feature", "PR welcome")):
            with self.subTest(labels=labels):
                self.assertFalse(sweep_labels_admit(list(labels)))

    def test_an_issue_missing_the_label_its_template_requires_is_dropped(self):
        # mlflow's template asks contributors to wait for `ready`; three
        # prescreens were spent learning that one row at a time. Mailman #269.
        from mailman.hunt import sweep_fresh_issues
        self._screen("acme/a", [])
        template = "Wait until a maintainer has applied the `ready` label before opening a PR."
        gh = _SearchGh([[
            {**_item("acme/a", 1), "body": template},
            {**_item("acme/a", 2, labels=("bug", "ready")), "body": template},
            {**_item("acme/a", 3), "body": None},
        ]])

        result = sweep_fresh_issues(self.data_root, gh, held_repositories=set(),
                                    now=datetime(2026, 9, 30, tzinfo=UTC))

        self.assertEqual(sorted(row["target"] for row in result["rows"]),
                         ["acme/a#2", "acme/a#3"])

    def test_a_full_page_is_followed_and_the_cap_is_reported(self):
        # One page of 100 is a few weeks of streamlit; a 180-day sweep kept
        # three more rows than a 60-day one. Mailman #262.
        from mailman.hunt import SWEEP_PAGES, sweep_fresh_issues
        self._screen("acme/a", [])
        self._screen("acme/b", [])
        full = [_item("acme/a", 1000 + n, labels=("question",)) for n in range(100)]
        gh = _SearchGh([full, [_item("acme/a", 7)]] + [list(full)] * SWEEP_PAGES)

        result = sweep_fresh_issues(self.data_root, gh, held_repositories=set(),
                                    now=datetime(2026, 9, 30, tzinfo=UTC))

        self.assertEqual(len(gh.paths), 2 + SWEEP_PAGES)
        self.assertIn("&page=2", gh.paths[1])
        self.assertEqual(len(result["truncated"]), 1)
        self.assertEqual(len(result["rows"]), 1)
        self.assertTrue(result["rows"][0]["target"].endswith("#7"))

    def test_rows_skip_prescreened_and_rank_commented_issues_first(self):
        from mailman.hunt import sweep_fresh_issues
        from mailman.prescreen import prescreen_path
        self._screen("acme/a", [])
        done = prescreen_path(self.data_root, "acme/a", 2)
        done.parent.mkdir(parents=True, exist_ok=True)
        done.write_text("{}", encoding="utf-8")
        gh = _SearchGh([[
            _item("acme/a", 1, created="2026-09-28T00:00:00Z"),
            _item("acme/a", 2, comments=4),
            _item("acme/a", 3, comments=2, created="2026-09-10T00:00:00Z"),
        ]])

        result = sweep_fresh_issues(self.data_root, gh, held_repositories=set(),
                                    now=datetime(2026, 9, 30, tzinfo=UTC))

        self.assertEqual([row["target"] for row in result["rows"]],
                         ["acme/a#3", "acme/a#1"])
        self.assertEqual(result["rows"][0]["comments"], 2)
        self.assertEqual(result["rows"][0]["title"], "bug 3")
        self.assertEqual(result["failed"], [])

    def test_a_refused_query_is_reported_not_skipped(self):
        from mailman.hunt import sweep_fresh_issues
        self._screen("acme/a", [])
        self._screen("acme/b", [])
        gh = _SearchGh([None, [_item("acme/b", 5)]])

        result = sweep_fresh_issues(self.data_root, gh, held_repositories=set(),
                                    now=datetime(2026, 9, 30, tzinfo=UTC))

        self.assertEqual(len(result["failed"]), 1)
        [row] = result["rows"]
        self.assertTrue(row["target"].endswith("#5"))
        self.assertNotEqual(row["target"].split("#")[0], result["failed"][0])

    def test_hunt_sweep_exits_non_zero_when_a_query_failed(self):
        record = self.new_hunt()
        answer = {"queries": 2, "repositories": 9, "failed": ["repo:acme/a"],
                  "rows": []}
        with mock.patch("mailman.hunt.sweep_fresh_issues", return_value=answer), \
                redirect_stdout(StringIO()) as out:
            code = main(["hunt", "sweep", record["hunt_id"],
                         "--data-root", str(self.data_root)])

        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out.getvalue())["failed"], ["repo:acme/a"])

    def test_a_cross_referenced_pull_request_claims_the_row(self):
        # moto#10176 and feast#6787 passed -linked:pr with open pull requests
        # that only named them. Mailman #226.
        from mailman.hunt import sweep_fresh_issues
        self._screen("acme/a", [])
        rival = {"event": "cross-referenced", "source": {"issue": {
            "number": 12, "state": "open", "pull_request": {"merged_at": None}}}}
        closed = {"event": "cross-referenced", "source": {"issue": {
            "number": 13, "state": "closed", "pull_request": {"merged_at": None}}}}
        gh = _SearchGh(
            [[_item("acme/a", 1), _item("acme/a", 2)]],
            timelines={"repos/acme/a/issues/1/timeline": [rival],
                       "repos/acme/a/issues/2/timeline": [closed]})

        result = sweep_fresh_issues(self.data_root, gh, held_repositories=set(),
                                    now=datetime(2026, 9, 30, tzinfo=UTC))

        self.assertEqual([row["target"] for row in result["rows"]], ["acme/a#2"])
        self.assertEqual(result["claimed"], [{"target": "acme/a#1", "pull_requests": [12]}])

    def test_a_rival_past_the_first_hundred_events_still_claims(self):
        # One `per_page=100` read missed a cross-reference on page 2.
        from mailman.hunt import sweep_fresh_issues
        self._screen("acme/a", [])
        filler = [{"event": "subscribed"}] * 100
        rival = {"event": "cross-referenced", "source": {"issue": {
            "number": 12, "state": "open", "pull_request": {"merged_at": None}}}}

        class PagedGh(_SearchGh):
            def json(self, path):
                if "/timeline" in path:
                    return [rival] if path.endswith("&page=2") else filler
                return super().json(path)

        gh = PagedGh([[_item("acme/a", 1)]])

        result = sweep_fresh_issues(self.data_root, gh, held_repositories=set(),
                                    now=datetime(2026, 9, 30, tzinfo=UTC))

        self.assertEqual(result["rows"], [])
        self.assertEqual(result["claimed"], [{"target": "acme/a#1", "pull_requests": [12]}])

    def test_a_pull_request_in_another_repository_does_not_claim(self):
        # A downstream workaround PR names the issue from its own repository.
        from mailman.hunt import sweep_fresh_issues
        self._screen("acme/a", [])
        downstream = {"event": "cross-referenced", "source": {"issue": {
            "number": 40, "state": "open", "pull_request": {"merged_at": None},
            "repository_url": "https://api.github.com/repos/other/app"}}}
        gh = _SearchGh(
            [[_item("acme/a", 1)]],
            timelines={"repos/acme/a/issues/1/timeline": [downstream]})

        result = sweep_fresh_issues(self.data_root, gh, held_repositories=set(),
                                    now=datetime(2026, 9, 30, tzinfo=UTC))

        self.assertEqual([row["target"] for row in result["rows"]], ["acme/a#1"])
        self.assertEqual(result["rows"][0]["prior_attempts"], [])
        self.assertEqual(result["claimed"], [])

    def test_a_sibling_repository_pull_request_claims_as_prescreen_counts_it(self):
        # prescreen blocks on an open fix PR under the same owner
        # (jsonschema#1497's is in python-jsonschema/referencing); the sweep
        # dropped it and offered the row. Mailman #340.
        from mailman.hunt import sweep_fresh_issues
        self._screen("acme/a", [])
        sibling = {"event": "cross-referenced", "source": {"issue": {
            "number": 40, "state": "open", "pull_request": {"merged_at": None},
            "repository_url": "https://api.github.com/repos/acme/core"}}}
        gh = _SearchGh(
            [[_item("acme/a", 1)]],
            timelines={"repos/acme/a/issues/1/timeline": [sibling]})

        result = sweep_fresh_issues(self.data_root, gh, held_repositories=set(),
                                    now=datetime(2026, 9, 30, tzinfo=UTC))

        self.assertEqual(result["rows"], [])
        self.assertEqual(result["claimed"],
                         [{"target": "acme/a#1", "pull_requests": ["acme/core#40"]}])

    def test_a_timeline_past_the_page_cap_is_unverified(self):
        # Five full pages were returned as the whole timeline, so a rival on
        # the sixth went unseen and the row was offered. Mailman #340.
        from mailman.hunt import sweep_fresh_issues
        self._screen("acme/a", [])
        filler = [{"event": "subscribed"}] * 100

        class EndlessGh(_SearchGh):
            def json(self, path):
                if "/timeline" in path:
                    return filler
                return super().json(path)

        gh = EndlessGh([[_item("acme/a", 1)]])

        result = sweep_fresh_issues(self.data_root, gh, held_repositories=set(),
                                    now=datetime(2026, 9, 30, tzinfo=UTC))

        self.assertEqual(result["rows"], [])
        self.assertEqual(result["unverified"], ["acme/a#1"])

    def test_a_rival_found_before_a_later_page_fails_still_claims(self):
        # A failed second page threw away the rival read on the first, and
        # the row became unverified instead of claimed. Mailman #340.
        from mailman.hunt import sweep_fresh_issues
        self._screen("acme/a", [])
        rival = {"event": "cross-referenced", "source": {"issue": {
            "number": 12, "state": "open", "pull_request": {"merged_at": None}}}}
        first = [rival] + [{"event": "subscribed"}] * 99

        class FailingGh(_SearchGh):
            def json(self, path):
                if "/timeline" in path:
                    return None if path.endswith("page=2") else first
                return super().json(path)

        gh = FailingGh([[_item("acme/a", 1)]])

        result = sweep_fresh_issues(self.data_root, gh, held_repositories=set(),
                                    now=datetime(2026, 9, 30, tzinfo=UTC))

        self.assertEqual(result["unverified"], [])
        self.assertEqual(result["claimed"], [{"target": "acme/a#1", "pull_requests": [12]}])

    def test_a_dormant_outside_pull_request_is_a_prior_attempt_not_a_claim(self):
        # typeshed#15495 sat behind typeshed#15497, untouched for 195 days by
        # an outside author; prescreen took it as stale. A maintainer's
        # dormant pull request still claims. Mailman #309.
        from mailman.hunt import sweep_fresh_issues
        self._screen("acme/a", [])
        old = "2026-05-01T00:00:00Z"
        outside = {"event": "cross-referenced", "source": {"issue": {
            "number": 12, "state": "open", "updated_at": old,
            "author_association": "NONE", "pull_request": {"merged_at": None}}}}
        member = {"event": "cross-referenced", "source": {"issue": {
            "number": 13, "state": "open", "updated_at": old,
            "author_association": "MEMBER", "pull_request": {"merged_at": None}}}}
        gh = _SearchGh(
            [[_item("acme/a", 1), _item("acme/a", 2)]],
            timelines={"repos/acme/a/issues/1/timeline": [outside],
                       "repos/acme/a/issues/2/timeline": [member]})

        result = sweep_fresh_issues(self.data_root, gh, held_repositories=set(),
                                    since_days=365,
                                    now=datetime(2026, 9, 30, tzinfo=UTC))

        self.assertEqual([row["target"] for row in result["rows"]], ["acme/a#1"])
        self.assertEqual(result["rows"][0]["prior_attempts"], [12])
        self.assertEqual(result["claimed"], [{"target": "acme/a#2", "pull_requests": [13]}])

    def test_the_sweep_reports_progress_per_repository_and_timeline_read(self):
        # A 240-day sweep ran 25 minutes and was killed with no output at
        # all; a slow sweep looked like a hung one. Mailman #313.
        from mailman.hunt import sweep_fresh_issues
        self._screen("acme/a", [])
        gh = _SearchGh([[_item("acme/a", 1), _item("acme/a", 2)]])
        lines: list[str] = []

        sweep_fresh_issues(self.data_root, gh, held_repositories=set(),
                           now=datetime(2026, 9, 30, tzinfo=UTC), progress=lines.append)

        self.assertTrue(any("acme/a" in line and "1/1" in line for line in lines), lines)
        self.assertTrue(any("timeline 2/2" in line for line in lines), lines)

    def test_the_sweep_reads_timelines_concurrently_and_keeps_the_order(self):
        # 284 timeline reads one at a time took about 20 minutes. Each read
        # here waits for a second one to start, which a serial loop never
        # does, so a serial sweep leaves both rows unverified. Mailman #314.
        import threading
        from mailman.hunt import sweep_fresh_issues
        self._screen("acme/a", [])
        barrier = threading.Barrier(2, timeout=5)

        class _Concurrent(_SearchGh):
            def json(self, path):
                if "/timeline" in path:
                    try:
                        barrier.wait()
                    except threading.BrokenBarrierError:
                        return None
                return super().json(path)

        gh = _Concurrent([[_item("acme/a", 1, created="2026-09-21T00:00:00Z"),
                           _item("acme/a", 2)]])

        result = sweep_fresh_issues(self.data_root, gh, held_repositories=set(),
                                    now=datetime(2026, 9, 30, tzinfo=UTC))

        self.assertEqual(result["unverified"], [])
        self.assertEqual([row["target"] for row in result["rows"]], ["acme/a#1", "acme/a#2"])

    def test_a_row_whose_timeline_could_not_be_read_is_unverified(self):
        # 13 rows kept after a rate-limited timeline read all had an open
        # pull request at prescreen. Mailman #283.
        from mailman.hunt import sweep_fresh_issues
        self._screen("acme/a", [])
        gh = _SearchGh(
            [[_item("acme/a", 1), _item("acme/a", 2)]],
            timelines={"repos/acme/a/issues/1/timeline": None})

        result = sweep_fresh_issues(self.data_root, gh, held_repositories=set(),
                                    now=datetime(2026, 9, 30, tzinfo=UTC))

        self.assertEqual([row["target"] for row in result["rows"]], ["acme/a#2"])
        self.assertEqual(result["unverified"], ["acme/a#1"])

    def test_hunt_sweep_exits_non_zero_when_a_row_is_unverified(self):
        record = self.new_hunt()
        answer = {"queries": 1, "repositories": 1, "failed": [],
                  "unverified": ["acme/a#1"], "rows": []}
        with mock.patch("mailman.hunt.sweep_fresh_issues", return_value=answer), \
                redirect_stdout(StringIO()):
            code = main(["hunt", "sweep", record["hunt_id"],
                         "--data-root", str(self.data_root)])

        self.assertEqual(code, 1)

    def test_a_closed_earlier_pull_request_ranks_the_row_after_clean_ones(self):
        # airflow#73311, huggingface_hub#4893 and two marimo rows came back
        # maintainer-closed-attempt at prescreen on 2026-09-30. Mailman #266.
        from mailman.hunt import sweep_fresh_issues
        self._screen("acme/a", [])
        closed = {"event": "cross-referenced", "source": {"issue": {
            "number": 13, "state": "closed", "pull_request": {"merged_at": None}}}}
        gh = _SearchGh(
            [[_item("acme/a", 1, comments=3), _item("acme/a", 2)]],
            timelines={"repos/acme/a/issues/1/timeline": [closed]})

        result = sweep_fresh_issues(self.data_root, gh, held_repositories=set(),
                                    now=datetime(2026, 9, 30, tzinfo=UTC))

        self.assertEqual(
            [(row["target"], row["prior_attempts"]) for row in result["rows"]],
            [("acme/a#2", []), ("acme/a#1", [13])],
        )

    def test_a_maintainer_label_or_reply_ranks_the_row_engaged_first(self):
        from mailman.hunt import sweep_fresh_issues
        self._screen("acme/a", [])
        labelled = {"event": "labeled", "actor": {"login": "maintainer"},
                    "label": {"name": "bug"}}
        self_labelled = {"event": "labeled", "actor": {"login": "reporter"},
                         "label": {"name": "bug"}}
        replied = {"event": "commented", "author_association": "MEMBER",
                   "actor": {"login": "maintainer"}}
        gh = _SearchGh(
            [[_item("acme/a", 1, comments=5), _item("acme/a", 2),
                        _item("acme/a", 3, comments=1)]],
            timelines={"repos/acme/a/issues/1/timeline": [self_labelled],
                       "repos/acme/a/issues/2/timeline": [labelled],
                       "repos/acme/a/issues/3/timeline": [replied]})

        result = sweep_fresh_issues(self.data_root, gh, held_repositories=set(),
                                    now=datetime(2026, 9, 30, tzinfo=UTC))

        self.assertEqual(
            [(row["target"], row["engaged"]) for row in result["rows"]],
            [("acme/a#3", True), ("acme/a#2", True), ("acme/a#1", False)],
        )

    def test_a_label_that_summons_a_triage_bot_is_not_engagement(self):
        # streamlit#17126's only human act was `ai-review`. Mailman #267.
        from mailman.hunt import sweep_fresh_issues
        self._screen("acme/a", [])
        routed = {"event": "labeled", "label": {"name": "ai-review"},
                  "actor": {"login": "maintainer", "type": "User"}}
        gh = _SearchGh([[_item("acme/a", 1)]],
                       timelines={"repos/acme/a/issues/1/timeline": [routed]})

        result = sweep_fresh_issues(self.data_root, gh, held_repositories=set(),
                                    now=datetime(2026, 9, 30, tzinfo=UTC))

        self.assertEqual([row["engaged"] for row in result["rows"]], [False])

    def test_a_bot_label_is_not_triage(self):
        # agentscope's github-actions[bot] labels every new issue
        # triage/confirmed. Mailman #253.
        from mailman.hunt import sweep_fresh_issues
        self._screen("acme/a", [])
        bot = {"event": "labeled", "label": {"name": "triage/confirmed"},
               "actor": {"login": "github-actions[bot]", "type": "Bot"}}
        gh = _SearchGh([[_item("acme/a", 1)]],
                       timelines={"repos/acme/a/issues/1/timeline": [bot]})

        result = sweep_fresh_issues(self.data_root, gh, held_repositories=set(),
                                    now=datetime(2026, 9, 30, tzinfo=UTC))

        self.assertEqual([row["engaged"] for row in result["rows"]], [False])


class RescreenTests(OrchestratorHarness):
    """Failing screens judged under older responsiveness rules. Mailman #227."""

    def _failed(self, slug, *, stars=1000, rules=None, failed=("responsiveness",),
                **numbers):
        data = {"sampled": 50, "responded": 40, "responded_within_days": 30,
                "median_first_response_days": 2.0, "merged": 10,
                "closed_unmerged": 12, **numbers}
        record = {
            "repository": slug, "success": True, "verdict": "fail",
            "screened_at": datetime.now(UTC).isoformat(),
            "failed_gates": list(failed),
            "gates": [
                {"name": "provenance", "passed": True, "data": {"stars": stars}},
                {"name": "responsiveness", "passed": False, "detail": "old", "data": data},
            ],
        }
        if rules is not None:
            record["responsiveness_rules"] = rules
        path = screen_path(self.data_root, slug)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record), encoding="utf-8")

    def test_old_rules_failures_that_could_pass_now_are_offered_by_stars(self):
        from mailman.hunt import rescreen_candidates
        from mailman.screen import RESPONSIVENESS_RULES_VERSION
        self._failed("acme/small", stars=10)
        self._failed("acme/big", stars=9000, rules=RESPONSIVENESS_RULES_VERSION - 1)
        # Judged under today's rules: its verdict stands.
        self._failed("acme/current", rules=RESPONSIVENESS_RULES_VERSION)
        # Also failed another gate: a re-screen cannot rescue it.
        self._failed("acme/also", failed=("freshness", "responsiveness"))
        # Merges 1 in 11 decided: fails today's merge-share rule regardless.
        self._failed("acme/closer", merged=1, closed_unmerged=10)
        # Median wait over 14 days fails whatever the denominator.
        self._failed("acme/slow", median_first_response_days=20.0)
        # Even with every unanswered pull request left out, under half on time.
        self._failed("acme/late", responded=40, responded_within_days=15)

        rows = rescreen_candidates(self.data_root)

        self.assertEqual([row["repository"] for row in rows], ["acme/big", "acme/small"])
        self.assertEqual(rows[0]["stars"], 9000)

    def test_hunt_targets_warns_with_the_refresh_commands(self):
        self._failed("acme/big", stars=9000)
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), mock.patch("sys.stderr", err):
            code = main(["hunt", "targets", "--data-root", str(self.data_root)])
        self.assertEqual(code, 0)
        warnings = json.loads(out.getvalue())["warnings"]
        self.assertEqual(len(warnings), 1)
        self.assertIn("mailman screen-target acme/big --refresh", warnings[0])
        self.assertIn("older responsiveness rules", err.getvalue())

    def test_a_new_screen_records_the_rules_it_was_judged_under(self):
        from mailman.executor import CommandResult
        from mailman.screen import RESPONSIVENESS_RULES_VERSION, screen_repository

        def refused(command, *args, **kwargs):
            return CommandResult(command=list(command), working_directory=".",
                                 started_at="", duration_seconds=0.0, exit_code=1,
                                 stdout="", stderr="Not Found", timed_out=False,
                                 timeout_seconds=1.0, environment={})

        record = screen_repository("acme/gone", data_root=self.data_root,
                                   executable="gh", _execute=refused)

        self.assertEqual(record["responsiveness_rules"], RESPONSIVENESS_RULES_VERSION)
        self.assertGreaterEqual(RESPONSIVENESS_RULES_VERSION, 3)
