"""A maintainer asked for changes, and the run has to move.

https://github.com/wolfgang-aura/Mailman/issues/60
"""
import json
import tempfile
import unittest
from pathlib import Path

from mailman.maintainer_review import (
    REVIEW_MARKDOWN,
    check_revision,
    fetch_review,
    init_response,
    load_review,
    parse_pull_request,
    render,
)
from mailman.models import ALLOWED_TRANSITIONS, RunStatus
from mailman.prompts import build_primary_prompt, build_reviewer_prompt
from tests.test_issue import write_stub_github_cli


PULL_REQUEST = "https://github.com/example/project/pull/42"
REVIEWS = json.dumps({
    "title": "Fix the thing",
    "state": "OPEN",
    "reviews": [
        {"id": "R1", "author": {"login": "maintainer"},
         "state": "CHANGES_REQUESTED", "submittedAt": "2026-09-07T14:01:40Z",
         "body": "Keep the original file until the replacement is committed."},
        {"id": "R2", "author": {"login": "passerby"},
         "state": "APPROVED", "submittedAt": "2026-09-07T15:00:00Z",
         "body": "Looks fine to me."},
    ],
})


def make_run(root: Path):
    from mailman.artifacts import create_run
    return create_run(
        repository="https://github.com/example/project.git",
        issue="https://github.com/example/project/issues/7",
        base_commit="a" * 40, primary="codex", reviewer="claude",
        primary_model="m", reviewer_model="m", data_root=root,
    )


class PullRequestUrlTests(unittest.TestCase):
    def test_reads_owner_repository_and_number(self) -> None:
        self.assertEqual(parse_pull_request(PULL_REQUEST), ("example/project", 42))

    def test_refuses_an_issue_url(self) -> None:
        with self.assertRaises(ValueError):
            parse_pull_request("https://github.com/example/project/issues/42")


class StateTests(unittest.TestCase):
    def test_a_filed_run_can_reach_the_revision_state(self) -> None:
        self.assertIn(
            RunStatus.MAINTAINER_CHANGES_REQUESTED,
            ALLOWED_TRANSITIONS[RunStatus.READY_FOR_HUMAN_REVIEW],
        )

    def test_the_revision_hands_the_run_back_to_the_orchestrator(self) -> None:
        self.assertIn(
            RunStatus.BLOCKED,
            ALLOWED_TRANSITIONS[RunStatus.MAINTAINER_CHANGES_REQUESTED],
        )


class FetchReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "runs"
        self.root.mkdir(parents=True)
        _, self.directory = make_run(self.root)

    def stub(self, payload: str) -> str:
        directory = Path(self.temporary.name) / "bin"
        directory.mkdir(exist_ok=True)
        return str(write_stub_github_cli(directory, payload))

    def test_a_changes_requested_review_becomes_a_requested_change(self) -> None:
        record = fetch_review(self.directory, pull_request=PULL_REQUEST,
                              executable=self.stub(REVIEWS))
        self.assertTrue(record["success"])
        # The stub answers the inline-comment call with the same payload, which
        # is not a list, so only the review body survives. That is the point:
        # an approval is not a requested change.
        ids = [item["id"] for item in record["requested_changes"]]
        self.assertEqual(ids, ["review:R1"])
        self.assertFalse(record["inline_comments_read"])

    def test_the_maintainers_words_reach_the_markdown(self) -> None:
        fetch_review(self.directory, pull_request=PULL_REQUEST,
                     executable=self.stub(REVIEWS))
        text = (self.directory / REVIEW_MARKDOWN).read_text(encoding="utf-8")
        self.assertIn("Keep the original file until the replacement is committed.", text)
        self.assertIn("Inline comments could not be read", text)

    def test_an_unreadable_pull_request_fails_without_a_record_of_success(self) -> None:
        record = fetch_review(self.directory, pull_request=PULL_REQUEST,
                              executable=self.stub("not json"))
        self.assertFalse(record["success"])
        self.assertEqual(load_review(self.directory)["success"], False)


class RevisionGateTests(FetchReviewTests):
    def prepare(self) -> None:
        fetch_review(self.directory, pull_request=PULL_REQUEST,
                     executable=self.stub(REVIEWS))

    def test_no_review_is_not_a_pass(self) -> None:
        checked = check_revision(self.directory)
        self.assertFalse(checked["ok"])
        self.assertEqual(checked["reason"], "no-review")

    def test_a_requested_change_with_no_response_is_refused(self) -> None:
        self.prepare()
        checked = check_revision(self.directory)
        self.assertFalse(checked["ok"])
        self.assertEqual(checked["reason"], "no-response")

    def test_an_unanswered_slot_is_named(self) -> None:
        self.prepare()
        init_response(self.directory)
        checked = check_revision(self.directory)
        self.assertEqual(checked["missing"], ["review:R1"])

    def test_declining_without_a_reason_is_refused(self) -> None:
        self.prepare()
        init_response(self.directory)
        path = self.directory / "revision-response.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        record["answers"][0].update(answer="declined", note="")
        path.write_text(json.dumps(record), encoding="utf-8")
        checked = check_revision(self.directory)
        self.assertFalse(checked["ok"])
        self.assertIn("no note", checked["detail"])

    def test_answering_every_request_passes(self) -> None:
        self.prepare()
        init_response(self.directory)
        path = self.directory / "revision-response.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        record["answers"][0].update(answer="answered", note="original kept until mv")
        path.write_text(json.dumps(record), encoding="utf-8")
        checked = check_revision(self.directory)
        self.assertTrue(checked["ok"], checked)
        self.assertEqual(checked["answered"], ["review:R1"])

    def test_declining_with_a_reason_passes_and_is_reported(self) -> None:
        self.prepare()
        init_response(self.directory)
        path = self.directory / "revision-response.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        record["answers"][0].update(answer="declined", note="breaks Windows case folding")
        path.write_text(json.dumps(record), encoding="utf-8")
        checked = check_revision(self.directory)
        self.assertTrue(checked["ok"], checked)
        self.assertEqual(checked["declined"], ["review:R1"])


class PromptTests(unittest.TestCase):
    def run_record(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name) / "runs"
        root.mkdir(parents=True)
        run, _ = make_run(root)
        return run

    def test_both_agents_are_given_the_maintainers_words(self) -> None:
        run = self.run_record()
        review = render({
            "repository": "example/project", "number": 42,
            "reviews": [{"id": "review:R1", "author": "maintainer",
                         "state": "CHANGES_REQUESTED",
                         "body": "Keep the original until the replacement lands."}],
            "comments": [], "inline_comments_read": True,
        })
        primary = build_primary_prompt(run, "issue", verification_command=None,
                                       maintainer_review=review)
        reviewer = build_reviewer_prompt(run, "issue", verification_command=None,
                                         maintainer_review=review)
        for text in (primary, reviewer):
            self.assertIn("Keep the original until the replacement lands.", text)
            self.assertIn("maintainer's review of the filed pull request", text)
        self.assertIn("not yours to negotiate away", primary)
        self.assertIn("leaves one of them unanswered is not", reviewer)

    def test_a_run_with_no_upstream_review_is_unchanged(self) -> None:
        run = self.run_record()
        text = build_primary_prompt(run, "issue", verification_command=None)
        self.assertNotIn("maintainer's review", text)


if __name__ == "__main__":
    unittest.main()
