from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from mailman.executor import CommandResult
from mailman.models import AgentConfig, RunRecord
from mailman.prior_art import collect_prior_art, render_prior_art, summarize_pull_request
from mailman.targeting import is_stale_attempt
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
    "mergeCommit": {"oid": "f3d1a4c5b6e7889900aabbccddeeff0011223344"},
    "files": [{"path": "src/_pytest/raises.py"}],
    "comments": [{"author": {"login": "x"}, "authorAssociation": "MEMBER", "body": "lgtm"}],
    "reviews": [],
}

#: Written relative to today, because whether an open attempt still claims the
#: issue is a question about its age. See targeting.STALE_ATTEMPT_DAYS.
_RECENTLY = (datetime.now(UTC) - timedelta(days=3)).isoformat()
_LONG_AGO = (datetime.now(UTC) - timedelta(days=400)).isoformat()

OPEN_PULL_REQUEST = {
    "number": 14668,
    "title": "Handle RaisesGroup check errors during suggestions",
    "state": "OPEN",
    "url": "https://github.com/pytest-dev/pytest/pull/14668",
    "body": "## Summary\nprevent the speculative check",
    "author": {"login": "someone"},
    "createdAt": "2026-07-01T00:00:00Z",
    "updatedAt": _RECENTLY,
    "closedAt": None,
    "mergedAt": None,
    "files": [{"path": "src/_pytest/raises.py"}],
    "comments": [],
    "reviews": [],
}

#: The same attempt, untouched for over a year.
DORMANT_PULL_REQUEST = {**OPEN_PULL_REQUEST, "updatedAt": _LONG_AGO}


#: A maintainer reviewed the dormant attempt and its author answered after:
#: copier-org/copier#2754, Mailman #157. The reply is an empty-bodied review,
#: which is what an inline-thread reply looks like.
_REVIEW = {
    "author": {"login": "maintainer"},
    "authorAssociation": "MEMBER",
    "state": "COMMENTED",
    "body": "I have a conceptual question.",
    "submittedAt": "2025-07-06T08:00:00Z",
}
_REPLY = {
    "author": {"login": "someone"},
    "authorAssociation": "CONTRIBUTOR",
    "state": "COMMENTED",
    "body": "",
    "submittedAt": "2025-07-06T15:00:00Z",
}
AWAITING_PULL_REQUEST = {**DORMANT_PULL_REQUEST, "reviews": [_REVIEW, _REPLY]}


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

    def test_a_maintainers_inline_review_comment_is_kept_with_its_path(self) -> None:
        # typeshed#15497: the review said "Some notes below." and the design
        # was in the inline comment. Mailman #308.
        payload = dict(CLOSED_PULL_REQUEST)
        payload["reviewComments"] = [
            {
                "user": {"login": "srittau"},
                "author_association": "COLLABORATOR",
                "path": "stubs/grpcio/grpc/aio/__init__.pyi",
                "line": 374,
                "body": "Prefix it with an underscore and mark it @type_check_only.",
            },
            {
                "user": {"login": "passerby"},
                "author_association": "NONE",
                "path": "x.py",
                "body": "bystander-remark",
            },
        ]
        summary = summarize_pull_request(payload)
        inline = [row for row in summary["comments"] if row["kind"] == "inline"]
        self.assertEqual([row["author"] for row in inline], ["srittau", "passerby"])
        self.assertTrue(inline[0]["maintainer"])
        self.assertEqual(inline[0]["path"], "stubs/grpcio/grpc/aio/__init__.pyi:374")
        page = render_prior_art(
            {"repository": "python/typeshed", "attempts": [summary]}
        )
        self.assertIn("@type_check_only", page)
        self.assertIn("`stubs/grpcio/grpc/aio/__init__.pyi:374`", page)
        self.assertNotIn("bystander-remark", page)

    def test_collecting_an_unmerged_attempt_reads_its_inline_comments(self) -> None:
        payload = dict(CLOSED_PULL_REQUEST, state="OPEN", closedAt=None)
        inline = [
            {
                "user": {"login": "srittau"},
                "author_association": "COLLABORATOR",
                "path": "a.pyi",
                "line": 3,
                "body": "Name it _AsyncRpcMethodHandler.",
            }
        ]
        calls: list[list[str]] = []

        def fake_execute(command, **_):
            calls.append(command)
            stdout = json.dumps(inline if command[1] == "api" else payload)
            return CommandResult(command, ".", "", 0.0, 0, stdout, "", False, 60, {})

        with TemporaryDirectory() as directory, patch(
            "mailman.prior_art.execute", fake_execute
        ):
            record = collect_prior_art(
                Path(directory), repository="python/typeshed", numbers=[14502],
                executable="gh",
            )
            page = (Path(directory) / "prior-art.md").read_text(encoding="utf-8")
        self.assertIn(
            ["gh", "api", "repos/python/typeshed/pulls/14502/comments?per_page=100"],
            calls,
        )
        self.assertTrue(record["success"])
        self.assertIn("_AsyncRpcMethodHandler", page)

    def test_a_merged_pull_request_is_withheld(self) -> None:
        # Handing an agent the accepted fix measures nothing. Same rule that
        # keeps comments out of the captured issue.
        summary = summarize_pull_request(MERGED_PULL_REQUEST)
        self.assertEqual(summary["outcome"], "merged")
        self.assertTrue(summary["withheld"])
        self.assertIsNone(summary["body"])
        self.assertEqual(summary["changed_files"], [])
        self.assertEqual(summary["comments"], [])
        # The merge commit is a name, not the fix, and `check-target` needs it
        # to ask whether this merge is already in the run's base commit. See
        # https://github.com/wolfgang-aura/Mailman/issues/46.
        self.assertEqual(
            summary["merge_commit"], "f3d1a4c5b6e7889900aabbccddeeff0011223344"
        )

    def test_a_merged_pull_request_with_no_merge_commit_records_none(self) -> None:
        payload = dict(MERGED_PULL_REQUEST)
        payload.pop("mergeCommit")
        self.assertIsNone(summarize_pull_request(payload)["merge_commit"])

    def test_a_closed_pull_request_records_no_merge_commit(self) -> None:
        self.assertNotIn("merge_commit", summarize_pull_request(CLOSED_PULL_REQUEST))

    def test_an_attempt_whose_author_answered_a_maintainer_is_not_stale(self) -> None:
        summary = summarize_pull_request(AWAITING_PULL_REQUEST)
        self.assertTrue(summary["awaiting_maintainer"])
        self.assertFalse(is_stale_attempt(summary))

    def test_an_attempt_the_maintainer_answered_last_is_still_stale(self) -> None:
        payload = {**DORMANT_PULL_REQUEST, "reviews": [_REPLY, {**_REVIEW, "submittedAt": "2025-07-07T00:00:00Z"}]}
        summary = summarize_pull_request(payload)
        self.assertFalse(summary["awaiting_maintainer"])
        self.assertTrue(is_stale_attempt(summary))

    def test_an_author_reply_to_a_bystander_is_still_stale(self) -> None:
        bystander = {**_REVIEW, "authorAssociation": "NONE", "author": {"login": "passerby"}}
        payload = {**DORMANT_PULL_REQUEST, "reviews": [bystander, _REPLY]}
        self.assertTrue(is_stale_attempt(summarize_pull_request(payload)))

    def test_an_untouched_dormant_attempt_is_still_stale(self) -> None:
        self.assertTrue(is_stale_attempt(summarize_pull_request(DORMANT_PULL_REQUEST)))

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

    def test_a_dormant_open_attempt_is_not_announced_as_a_claim(self) -> None:
        # An attempt nobody has touched in over a year does not stop the work.
        # It is the record of what was tried, and the pull request body has to
        # say it supersedes it. See targeting.STALE_ATTEMPT_DAYS.
        rendered = render_prior_art(self._record(DORMANT_PULL_REQUEST))
        self.assertNotIn("already claims this issue", rendered)
        self.assertIn("open but dormant", rendered)
        self.assertIn("supersedes it", rendered)

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

    def test_prompts_bound_context_and_candidate_scope(self) -> None:
        primary = build_primary_prompt(_run(), "the issue", verification_command=None)
        reviewer = build_reviewer_prompt(_run(), "the issue", verification_command=None)
        for prompt in (primary, reviewer):
            self.assertIn("Time and context discipline", prompt)
            self.assertIn("8 files", prompt)
            self.assertIn("500 changed lines", prompt)
            self.assertIn("Do not print whole large files", prompt)


if __name__ == "__main__":
    unittest.main()
