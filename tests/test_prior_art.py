from __future__ import annotations

import unittest

from mailman.models import AgentConfig, RunRecord
from mailman.prior_art import render_prior_art, summarize_pull_request
from mailman.prompts import build_primary_prompt, build_reviewer_prompt


CLOSED_PULL_REQUEST = {
    "number": 14502,
    "title": "Handle group-only RaisesGroup checks safely",
    "state": "CLOSED",
    "url": "https://github.com/pytest-dev/pytest/pull/14502",
    "body": "## Summary\nCloses #14324",
    "author": {"login": "GChaucer"},
    "createdAt": "2026-05-22T00:00:00Z",
    "closedAt": "2026-05-23T00:00:00Z",
    "mergedAt": None,
    "files": [{"path": "src/_pytest/raises.py"}, {"path": "testing/python/raises_group.py"}],
    "comments": [
        {
            "author": {"login": "RonnyPfannschmidt"},
            "authorAssociation": "MEMBER",
            "body": "Closing as unattended undisclosed ai",
        },
        {
            "author": {"login": "passerby"},
            "authorAssociation": "NONE",
            "body": "+1 please merge",
        },
    ],
    "reviews": [],
}

MERGED_PULL_REQUEST = {
    "number": 13192,
    "title": "add RaisesGroup & Matcher",
    "state": "MERGED",
    "url": "https://github.com/pytest-dev/pytest/pull/13192",
    "body": "the accepted implementation, in detail",
    "author": {"login": "maintainer"},
    "createdAt": "2026-01-01T00:00:00Z",
    "closedAt": "2026-01-05T00:00:00Z",
    "mergedAt": "2026-01-05T00:00:00Z",
    "files": [{"path": "src/_pytest/raises.py"}],
    "comments": [{"author": {"login": "x"}, "authorAssociation": "MEMBER", "body": "lgtm"}],
    "reviews": [],
}

OPEN_PULL_REQUEST = {
    "number": 14668,
    "title": "Handle RaisesGroup check errors during suggestions",
    "state": "OPEN",
    "url": "https://github.com/pytest-dev/pytest/pull/14668",
    "body": "## Summary\nprevent the speculative check",
    "author": {"login": "someone"},
    "createdAt": "2026-07-01T00:00:00Z",
    "closedAt": None,
    "mergedAt": None,
    "files": [{"path": "src/_pytest/raises.py"}],
    "comments": [],
    "reviews": [],
}


def _run() -> RunRecord:
    return RunRecord(
        run_id="20260902T000000Z-abcdef",
        repository="https://github.com/pytest-dev/pytest.git",
        issue="https://github.com/pytest-dev/pytest/issues/14324",
        base_commit="a" * 40,
        primary=AgentConfig(agent="codex"),
        reviewer=AgentConfig(agent="claude"),
    )


class SummarizeTests(unittest.TestCase):
    def test_a_closed_attempt_keeps_its_body_and_maintainer_comment(self) -> None:
        summary = summarize_pull_request(CLOSED_PULL_REQUEST)
        self.assertEqual(summary["outcome"], "closed unmerged")
        self.assertFalse(summary["withheld"])
        self.assertIn("Closes #14324", summary["body"])
        maintainers = [row for row in summary["comments"] if row["maintainer"]]
        self.assertEqual(len(maintainers), 1)
        self.assertEqual(maintainers[0]["author"], "RonnyPfannschmidt")

    def test_a_merged_pull_request_is_withheld(self) -> None:
        # Handing an agent the accepted fix measures nothing. Same rule that
        # keeps comments out of the captured issue.
        summary = summarize_pull_request(MERGED_PULL_REQUEST)
        self.assertEqual(summary["outcome"], "merged")
        self.assertTrue(summary["withheld"])
        self.assertIsNone(summary["body"])
        self.assertEqual(summary["changed_files"], [])
        self.assertEqual(summary["comments"], [])

    def test_an_open_pull_request_is_reported_as_open(self) -> None:
        self.assertEqual(summarize_pull_request(OPEN_PULL_REQUEST)["outcome"], "open")


class RenderTests(unittest.TestCase):
    def _record(self, *payloads: dict) -> dict:
        return {
            "collected_at": "2026-09-02T00:00:00+00:00",
            "repository": "pytest-dev/pytest",
            "attempts": [summarize_pull_request(payload) for payload in payloads],
        }

    def test_a_maintainer_rejection_is_quoted(self) -> None:
        rendered = render_prior_art(self._record(CLOSED_PULL_REQUEST))
        self.assertIn("Closing as unattended undisclosed ai", rendered)
        self.assertIn("RonnyPfannschmidt (member)", rendered)

    def test_non_maintainer_comments_are_counted_not_quoted(self) -> None:
        rendered = render_prior_art(self._record(CLOSED_PULL_REQUEST))
        self.assertNotIn("+1 please merge", rendered)
        self.assertIn("1 other comment(s) from non-maintainers", rendered)

    def test_a_merged_body_never_reaches_the_page(self) -> None:
        rendered = render_prior_art(self._record(MERGED_PULL_REQUEST))
        self.assertNotIn("the accepted implementation", rendered)
        self.assertIn("deliberately withheld", rendered)

    def test_an_open_attempt_leads_with_a_warning(self) -> None:
        rendered = render_prior_art(self._record(OPEN_PULL_REQUEST))
        self.assertIn("already claims this issue", rendered)

    def test_no_attempts_says_so_without_reassurance(self) -> None:
        rendered = render_prior_art(self._record())
        self.assertIn("No earlier pull request was found", rendered)
        self.assertIn("reason for care", rendered)


class PromptIntegrationTests(unittest.TestCase):
    def test_both_prompts_carry_prior_art_when_present(self) -> None:
        prior_art = render_prior_art(
            {
                "collected_at": "2026-09-02T00:00:00+00:00",
                "repository": "pytest-dev/pytest",
                "attempts": [summarize_pull_request(CLOSED_PULL_REQUEST)],
            }
        )
        primary = build_primary_prompt(
            _run(), "the issue", verification_command=None, prior_art=prior_art
        )
        reviewer = build_reviewer_prompt(
            _run(), "the issue", verification_command=None, prior_art=prior_art
        )
        self.assertIn("Earlier attempts at this issue", primary)
        self.assertIn("Closing as unattended undisclosed ai", primary)
        self.assertIn("say in your report why yours is different", primary)
        self.assertIn("repeats a rejected approach", reviewer)

    def test_prompts_are_unchanged_when_no_prior_art_exists(self) -> None:
        primary = build_primary_prompt(_run(), "the issue", verification_command=None)
        self.assertNotIn("Earlier attempts", primary)


if __name__ == "__main__":
    unittest.main()
