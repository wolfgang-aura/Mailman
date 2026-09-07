import copy
import json
import sys
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime
from io import StringIO

from mailman.artifacts import load_run, write_run
from mailman.cli import main
from mailman.completion import finalize_review
from mailman.export import export_patch
from mailman.handoff import build_handoff
from mailman.hunt import add_run, create_hunt, finish, load_hunt, next_action
from mailman.identity import Identity, save_identity
from mailman.models import AgentConfig
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

    def test_requested_three_does_not_count_one_as_completion(self):
        hunt = self.new_hunt(3)
        directory = self.ready_run()
        add_run(self.data_root, hunt, directory.name)
        add_run(self.data_root, hunt, directory.name)
        result = finish(self.data_root, hunt)
        self.assertFalse(result["complete"])
        self.assertEqual(result["remaining"], 2)
        self.assertEqual(len(hunt["runs"]), 1)

    def test_model_substitution_is_refused(self):
        hunt = self.new_hunt()
        run, _ = self.make_run()
        with self.assertRaisesRegex(ValueError, "model"):
            add_run(self.data_root, hunt, run.run_id)

    def test_a_body_edit_invalidates_completion(self):
        directory = self.ready_run()
        (directory / "final-body.md").write_text("Changed after preview", encoding="utf-8")
        result = next_action(directory)
        self.assertFalse(result["ready"])
        self.assertEqual(result["stage"], "handoff")
        self.assertFalse(result["human_required"])

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
