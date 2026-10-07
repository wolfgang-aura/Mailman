"""`mailman hunt ship`: one command files every run at filing approval.

Every git and gh call goes to a fake; nothing here reaches a remote.
https://github.com/wolfgang-aura/Mailman/issues/362
"""

import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from mailman.cli import main
from mailman.hunt import OWN_WORDS_ACTION, PERSONAL_REVIEW_ACTION, add_run, load_hunt
from mailman.ship import Completed, ShipFailure, ship
from tests import test_hunt
from tests.test_orchestrator import OrchestratorHarness, git

WRITES = (("gh", "repo", "fork"), ("gh", "pr", "create"))


class FakeGitHub:
    """Answers the git and gh calls `ship` makes, and records them."""

    def __init__(self, *, login="fixture", sha="", fork=False, remote="", prs=None):
        self.login = login
        self.sha = sha
        self.fork = fork
        self.remote = remote
        self.prs = list(prs or [])
        self.fail: dict[str, Completed] = {}
        self.calls: list[list[str]] = []

    def __call__(self, command, cwd=None):
        self.calls.append(list(command))
        for word, result in self.fail.items():
            if word in command:
                return result
        if command[:3] == ["gh", "api", "user"]:
            return Completed(0, self.login + "\n")
        if command[:2] == ["gh", "api"]:
            if not self.fork:
                return Completed(1, "", "gh: Not Found (HTTP 404)")
            return Completed(0, json.dumps(
                {"fork": True, "parent": {"full_name": "example/project"}}))
        if command[:3] == ["gh", "repo", "fork"]:
            self.fork = True
            return Completed(0, "")
        if command[0] == "git" and "rev-parse" in command:
            return Completed(0, self.sha + "\n")
        if command[:2] == ["git", "ls-remote"]:
            return Completed(0, f"{self.remote}\trefs/heads/main\n" if self.remote else "")
        if command[0] == "git" and "push" in command:
            self.remote = self.sha
            return Completed(0, "")
        if command[:3] == ["gh", "pr", "list"]:
            return Completed(0, json.dumps(self.prs))
        if command[:3] == ["gh", "pr", "create"]:
            url = f"https://github.com/example/project/pull/{40 + len(self.prs)}"
            self.prs.append({"url": url, "state": "OPEN",
                             "headRepositoryOwner": {"login": self.login}})
            return Completed(0, f"Creating pull request\n{url}\n")
        raise AssertionError(f"unexpected command {command}")

    def wrote(self):
        writes = [call for call in self.calls if tuple(call[:3]) in WRITES]
        return writes + [call for call in self.calls if call[0] == "git" and "push" in call]


class ShipReadyRunTests(OrchestratorHarness):
    # The hunt fixture's helpers, without inheriting its tests.
    new_hunt = test_hunt.HuntTests.new_hunt
    ready_run = test_hunt.HuntTests.ready_run

    def setUp(self):
        super().setUp()
        (self.workspace / "tests").mkdir()
        self.record = self.new_hunt()
        self.directory = self.ready_run()
        add_run(self.data_root, self.record, self.directory.name)
        self.sha = git(self.workspace, "rev-parse", "HEAD")

    def ship(self, github, **options):
        options.setdefault("refresher", lambda *_, **__: None)
        return ship(self.data_root, self.record, run=github, progress=lambda _: None,
                    provenance_recorder=lambda **_: None, sleep=lambda _: None, **options)

    def test_a_ready_run_is_forked_pushed_opened_and_recorded(self):
        github = FakeGitHub(sha=self.sha)
        result = self.ship(github)
        row = result["runs"][0]
        self.assertIsNone(result["failure"], result)
        self.assertEqual(row["outcome"], "filed")
        self.assertEqual([step["stage"] for step in row["steps"]],
                         ["package", "fork", "push", "pull-request", "hunt-file"])
        create = next(call for call in github.calls if call[:3] == ["gh", "pr", "create"])
        self.assertEqual(create[create.index("--head") + 1], "fixture:main")
        self.assertEqual(create[create.index("--title") + 1], "Fix synthetic fixture")
        self.assertTrue(create[create.index("--body-file") + 1].endswith("final-body.md"))
        push = next(call for call in github.calls if "push" in call)
        self.assertIn("https://github.com/fixture/project.git", push)
        stored = load_hunt(self.data_root, self.record["hunt_id"])
        self.assertEqual(stored["status"], "FILED")
        self.assertEqual(stored["runs"][0]["filed"]["pr_url"], row["pr_url"])
        self.assertEqual(stored["runs"][0]["filed"]["commit"], self.sha)

    def test_a_rerun_reuses_the_fork_branch_and_open_pull_request(self):
        url = "https://github.com/example/project/pull/9"
        github = FakeGitHub(sha=self.sha, fork=True, remote=self.sha, prs=[
            {"url": url, "state": "OPEN", "headRepositoryOwner": {"login": "fixture"}}])
        result = self.ship(github)
        self.assertEqual(github.wrote(), [])
        self.assertEqual(result["runs"][0]["outcome"], "filed")
        self.assertEqual(load_hunt(self.data_root, self.record["hunt_id"])["runs"][0]
                         ["filed"]["pr_url"], url)

    def test_a_dry_run_writes_nothing(self):
        refreshed = []
        github = FakeGitHub(sha=self.sha)
        result = self.ship(github, dry_run=True,
                           refresher=lambda *_, **__: refreshed.append(True))
        self.assertEqual(github.wrote(), [])
        self.assertEqual(refreshed, [])
        self.assertEqual(result["runs"][0]["outcome"], "would-file")
        self.assertNotIn("filed", load_hunt(self.data_root, self.record["hunt_id"])["runs"][0])

    def test_a_failed_push_stops_and_names_the_resume_command(self):
        github = FakeGitHub(sha=self.sha)
        github.fail["push"] = Completed(1, "", "remote: Permission denied")
        result = self.ship(github, lease_owner="token123")
        row = result["runs"][0]
        self.assertEqual(row["outcome"], "failed")
        self.assertEqual(row["stage"], "push")
        self.assertIn("Permission denied", row["detail"])
        self.assertEqual([step["stage"] for step in row["steps"]], ["package", "fork"])
        self.assertEqual(result["resume"],
                         f"mailman hunt ship {self.record['hunt_id']} --owner token123")
        self.assertFalse(any(call[:3] == ["gh", "pr", "create"] for call in github.calls))
        self.assertNotIn("filed", load_hunt(self.data_root, self.record["hunt_id"])["runs"][0])

    def test_a_branch_someone_else_moved_is_not_overwritten(self):
        github = FakeGitHub(sha=self.sha, fork=True, remote="f" * 40)
        result = self.ship(github)
        self.assertEqual(result["failure"]["stage"], "push")
        self.assertEqual(github.wrote(), [])

    def test_a_closed_pull_request_for_the_branch_is_not_duplicated(self):
        github = FakeGitHub(sha=self.sha, fork=True, remote=self.sha, prs=[
            {"url": "https://github.com/example/project/pull/3", "state": "CLOSED",
             "headRepositoryOwner": {"login": "fixture"}}])
        result = self.ship(github)
        self.assertEqual(result["failure"]["stage"], "pull-request")
        self.assertEqual(github.wrote(), [])

    def test_another_signed_in_account_is_skipped_rather_than_forked_into(self):
        github = FakeGitHub(sha=self.sha, login="someone-else")
        result = self.ship(github)
        self.assertEqual(result["runs"][0]["outcome"], "skipped")
        self.assertIn("gh auth switch --user fixture", result["runs"][0]["reason"])
        self.assertEqual(github.wrote(), [])

    def test_the_cli_dry_run_prints_the_plan_and_exits_zero(self):
        github = FakeGitHub(sha=self.sha)
        output = StringIO()
        with mock.patch("mailman.ship.run_command", github), \
                redirect_stdout(output), redirect_stderr(StringIO()):
            code = main(["hunt", "ship", self.record["hunt_id"], "--dry-run",
                         "--owner", self.record["lease"]["owner"],
                         "--data-root", str(self.data_root)])
        self.assertEqual(code, 0, output.getvalue())
        self.assertIn("would-file", output.getvalue())
        self.assertIn("would open on example/project", output.getvalue())
        self.assertEqual(github.wrote(), [])


class ShipGateTests(unittest.TestCase):
    """Runs the readiness gate refuses are listed with its reason, never filed."""

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.record = {"hunt_id": "h", "requested": 3, "status": "RUNNING",
                       "runs": [{"run_id": "a"}, {"run_id": "b"}, {"run_id": "c"}]}

    def ship(self, readiness, **options):
        return ship(self.root, self.record, run=FakeGitHub(), readiness=readiness,
                    refresher=lambda *_, **__: None, progress=lambda _: None, **options)

    def test_human_required_and_failed_gates_are_skipped_with_their_reasons(self):
        answers = {
            "a": {"ready": True, "stage": "filing-approval", "disposition": "READY",
                  "human_required": True, "action": OWN_WORDS_ACTION},
            "b": {"ready": False, "stage": "handoff", "disposition": "REPAIR",
                  "action": "mailman handoff-check b", "detail": "body-changed"},
            "c": {"ready": False, "stage": "offer-approval", "disposition": "READY_TO_ASK"},
        }
        with mock.patch("mailman.ship.ship_run") as shipped:
            result = self.ship(lambda directory: answers[directory.name])
        shipped.assert_not_called()
        reasons = {row["run_id"]: row["reason"] for row in result["runs"]}
        self.assertIn("own words", reasons["a"])
        self.assertIn("body-changed", reasons["b"])
        self.assertIn("ask-first", reasons["c"])
        self.assertTrue(all(row["outcome"] == "skipped" for row in result["runs"]))

    def test_answer_review_files_a_personal_review_run_and_nothing_else(self):
        # Python-Markdown#1643 (#402): the personal-review gate had no answer,
        # so ship skipped the run forever.
        answers = {
            "a": {"ready": True, "stage": "filing-approval", "disposition": "READY",
                  "human_required": True, "action": PERSONAL_REVIEW_ACTION},
            "b": {"ready": True, "stage": "filing-approval", "disposition": "READY",
                  "human_required": True, "action": OWN_WORDS_ACTION},
            "c": {"ready": True, "stage": "filing-approval", "disposition": "READY",
                  "human_required": True, "action": PERSONAL_REVIEW_ACTION},
        }
        self.record["runs"][2]["dropped"] = True
        with mock.patch("mailman.ship.ship_run") as shipped:
            skipped = self.ship(lambda directory: answers[directory.name], dry_run=True)
        self.assertIn("--answer-review", skipped["runs"][0]["reason"])
        with mock.patch("mailman.ship.ship_run") as shipped:
            result = self.ship(lambda directory: answers[directory.name],
                               answer_review=True)
        self.assertEqual([call.args[2]["run_id"] for call in shipped.call_args_list], ["a"])
        self.assertEqual(result["runs"][1]["outcome"], "skipped")

    def test_the_first_failure_stops_the_batch(self):
        ready = {"ready": True, "stage": "filing-approval", "disposition": "READY",
                 "human_required": False}

        def fail(root, record, row, **_):
            raise ShipFailure("fork", "gh repo fork failed")

        with mock.patch("mailman.ship.ship_run", side_effect=fail) as shipped:
            result = self.ship(lambda _: ready, lease_owner="t")
        self.assertEqual(shipped.call_count, 1)
        self.assertEqual([row["outcome"] for row in result["runs"]],
                         ["failed", "not-attempted", "not-attempted"])
        self.assertEqual(result["failure"]["run_id"], "a")
        self.assertIn("mailman hunt ship h --owner t", result["resume"])

    def test_only_the_open_slots_are_shipped(self):
        self.record["requested"] = 2
        self.record["runs"][0]["filed"] = {"pr_url": "https://github.com/x/y/pull/1"}
        ready = {"ready": True, "stage": "filing-approval", "disposition": "READY",
                 "human_required": False}
        with mock.patch("mailman.ship.ship_run") as shipped:
            result = self.ship(lambda _: ready)
        self.assertEqual(shipped.call_count, 1)
        self.assertEqual([row["outcome"] for row in result["runs"]],
                         ["already-filed", "pending", "skipped"])

    def test_a_rolling_hunt_ships_every_ready_run(self):
        self.record["requested"] = None
        self.record["runs"][0]["filed"] = {"pr_url": "https://github.com/x/y/pull/1"}
        ready = {"ready": True, "stage": "filing-approval", "disposition": "READY",
                 "human_required": False}
        with mock.patch("mailman.ship.ship_run") as shipped:
            result = self.ship(lambda _: ready)
        self.assertEqual(shipped.call_count, 2)
        self.assertEqual([row["outcome"] for row in result["runs"]],
                         ["already-filed", "pending", "pending"])


if __name__ == "__main__":
    unittest.main()
