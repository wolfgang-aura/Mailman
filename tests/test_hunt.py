import copy
import json
import sys
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path

from mailman.artifacts import load_run, write_run
from mailman.cli import main
from mailman.completion import finalize_review
from mailman.export import export_patch
from mailman.handoff import build_handoff
from mailman.hunt import (
    abandon,
    add_run,
    acquire_lease,
    require_lease,
    compact,
    create_hunt,
    effective_status,
    finish,
    holding_hunt,
    hunt_path,
    load_hunt,
    next_action,
    record_filing,
    refresh,
    restore_run,
    save,
    status,
    target_claims,
)
from mailman.identity import Identity, save_identity
from mailman.models import AgentConfig
from mailman.provenance import ProvenanceError, unrecorded_submissions
from mailman.screen import screen_path
from mailman.submission import prepare_submission
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
        hunt = self.new_hunt()
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

    def test_a_partly_filed_hunt_is_not_terminal(self):
        record = self.new_hunt(2)
        directory = self.ready_run()
        add_run(self.data_root, record, directory.name)
        record_filing(self.data_root, record, directory.name,
                      pr_url="https://github.com/example/project/pull/7")
        self.assertEqual(record["status"], "RUNNING")

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
