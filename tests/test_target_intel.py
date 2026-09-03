from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from mailman.target_intel import (
    classify_claims,
    enforcement_markers,
    is_outside_human,
    referenced_issues,
    render_target_intel,
    repository_slug,
)
from mailman.targeting import NO_TARGET_INTEL, assess_target


def _pull(**overrides: object) -> dict:
    row = {
        "number": 1,
        "state": "closed",
        "title": "fix(core): something",
        "body": "",
        "head": {"ref": "fix/thing"},
        "merged_at": None,
        "closed_at": "2026-09-01T00:00:00Z",
        "author_association": "CONTRIBUTOR",
        "user": {"login": "someone", "type": "User"},
    }
    row.update(overrides)
    return row


class OutsideHumanTests(unittest.TestCase):
    """Dependabot merges as a CONTRIBUTOR, which is the whole problem.

    `PyCQA/bandit` scored one outside merge in a fortnight on that basis and had
    merged no human outside pull request since May 2026.
    """

    def test_a_bot_account_is_not_an_outside_human(self) -> None:
        row = _pull(user={"login": "dependabot[bot]", "type": "Bot"})
        self.assertFalse(is_outside_human(row))

    def test_a_bot_typed_as_a_user_is_still_a_bot(self) -> None:
        row = _pull(user={"login": "pre-commit-ci[bot]", "type": "User"})
        self.assertFalse(is_outside_human(row))

    def test_a_maintainer_is_not_an_outside_contributor(self) -> None:
        self.assertFalse(is_outside_human(_pull(author_association="MEMBER")))

    def test_an_outside_person_counts(self) -> None:
        self.assertTrue(is_outside_human(_pull()))


class ReferencedIssueTests(unittest.TestCase):
    def test_references_are_read_from_title_body_and_branch(self) -> None:
        row = _pull(
            title="fix: repair #101",
            body="Fixes #102 and see github.com/o/r/issues/103",
            head={"ref": "fix/issue-104"},
        )
        self.assertEqual(referenced_issues(row), {"101", "102", "103", "104"})


class ClaimClassificationTests(unittest.TestCase):
    """A closed unmerged pull request is not a claim.

    On a bot-policed target it is the record of someone being refused. Counting
    it as a claim read `langchain-ai/langchain` as fully saturated: 3 unassigned
    open bugs by that rule against 53 by this one.
    """

    def test_open_and_merged_rows_claim_and_closed_rows_do_not(self) -> None:
        rows = [
            _pull(number=1, state="open", body="Fixes #900"),
            _pull(number=2, merged_at="2026-09-01T00:00:00Z", body="Fixes #901"),
            _pull(number=3, body="Fixes #902"),
        ]
        claims = classify_claims(rows)
        self.assertEqual(claims["claiming"], {"900", "901"})
        self.assertEqual(claims["abandoned"], {"902"})


class EnforcementMarkerTests(unittest.TestCase):
    """The bot says what it enforces. Reading it beats guessing."""

    def test_markers_are_counted_and_quoted_from_bot_comments_only(self) -> None:
        comments = [
            {
                "user": {"login": "github-actions[bot]", "type": "Bot"},
                "body": "<!-- require-issue-link -->\nClosed because you are not "
                "assigned to the linked issue.",
                "_pull_request": 38641,
            },
            {
                "user": {"login": "github-actions[bot]", "type": "Bot"},
                "body": "<!-- require-issue-link -->\nSame rule again.",
                "_pull_request": 38647,
            },
            {
                "user": {"login": "github-actions[bot]", "type": "Bot"},
                "body": "<!-- block-fork-main -->\nHead branch is master on a fork.",
                "_pull_request": 38640,
            },
            {
                "user": {"login": "a-human", "type": "User"},
                "body": "<!-- not-a-rule -->\njust a person talking",
                "_pull_request": 1,
            },
        ]
        markers = enforcement_markers(comments)
        names = [entry["marker"] for entry in markers]
        self.assertEqual(names, ["require-issue-link", "block-fork-main"])
        self.assertEqual(markers[0]["count"], 2)
        self.assertEqual(markers[0]["seen_on"], [38641, 38647])
        self.assertIn("not assigned to the linked issue", markers[0]["quote"])
        self.assertNotIn("not-a-rule", names)


class SlugTests(unittest.TestCase):
    def test_a_clone_url_reduces_to_owner_and_name(self) -> None:
        for given in (
            "https://github.com/encode/starlette",
            "https://github.com/encode/starlette.git",
            "git@github.com:encode/starlette.git",
            "encode/starlette",
        ):
            self.assertEqual(repository_slug(given), "encode/starlette")


class RenderTests(unittest.TestCase):
    def test_the_page_leads_with_the_assignment_rule_when_there_is_one(self) -> None:
        record = {
            "success": True,
            "repository": "langchain-ai/langchain",
            "window_days": 14,
            "stars": 145555,
            "freshness": {
                "human_outside_merges": 15,
                "bot_merges_excluded": 4,
                "outside_pull_requests_closed_unmerged": 129,
            },
            "saturation": {
                "unclaimed_counting_only_open_or_merged": 53,
                "unclaimed_counting_any_pull_request": 3,
            },
            "enforcement": [
                {
                    "marker": "require-issue-link",
                    "count": 5,
                    "quote": "closed because you are not assigned",
                    "seen_on": [38641],
                }
            ],
            "assessment": {
                "merge_path_rows_read": 4,
                "merges_whose_author_held_the_assignment": 4,
                "assignment_looks_required": True,
                "assignment_seen_on_some_merges": True,
                "assignment_reaches_non_reporters": True,
            },
            "merge_path": [
                {
                    "pull_request": 38355,
                    "author": "Sourav1331",
                    "title": "fix(langchain): routing",
                    "issues": [
                        {
                            "number": 38351,
                            "reporter": "Fr3ya",
                            "assignees": ["Sourav1331"],
                        }
                    ],
                    "thread_before_the_pull_request": [
                        {
                            "author": "gitbalaji",
                            "association": "CONTRIBUTOR",
                            "won": False,
                            "body": "Hi, I'd like to work on this.",
                        },
                        {
                            "author": "Sourav1331",
                            "association": "CONTRIBUTOR",
                            "won": True,
                            "body": "I reproduced the routing problem and prepared "
                            "a minimal fix.",
                        },
                    ],
                }
            ],
        }
        page = render_target_intel(record)
        self.assertIn("4 of the 4 outside merges", page)
        self.assertIn("assigned to the pull request's author", page)
        self.assertIn("closed unread", page)
        self.assertIn("somebody other than the issue's reporter", page)
        self.assertIn("require-issue-link", page)
        self.assertIn("`WON` **Sourav1331**", page)
        self.assertIn("Linked issue #38351", page)

    def test_a_partial_assignment_rate_is_reported_as_a_ratio_not_a_no(self) -> None:
        """langchain assigns its code fixes and merges docs without assignment.

        A bare `False` on the strict boolean reads as "no gate here", which is
        the opposite of what the rows show.
        """
        page = render_target_intel(
            {
                "success": True,
                "repository": "langchain-ai/langchain",
                "window_days": 14,
                "freshness": {},
                "saturation": {},
                "assessment": {
                    "merge_path_rows_read": 3,
                    "merges_whose_author_held_the_assignment": 2,
                    "assignment_looks_required": False,
                    "assignment_seen_on_some_merges": True,
                },
                "merge_path": [],
            }
        )
        self.assertIn("2 of the 3 outside merges", page)
        self.assertIn("some of them, not all", page)

    def test_a_failed_read_says_so_instead_of_rendering_a_verdict(self) -> None:
        page = render_target_intel({"success": False, "detail": "rate limited"})
        self.assertIn("rate limited", page)


class TargetIntelGateTests(unittest.TestCase):
    """A run cannot start against a target nobody has read.

    Skipping this screen is what cost the 2026-09-03 session a full hunt that
    was never recorded. See
    https://github.com/wolfgang-aura/Mailman/issues/35.
    """

    def _searched(self, root: Path) -> Path:
        (root / "duplicate-search.json").write_text(
            json.dumps({"success": True, "matches": []}), encoding="utf-8"
        )
        (root / "prior-art.json").write_text(
            json.dumps({"success": True, "attempts": []}), encoding="utf-8"
        )
        return root

    def test_a_missing_target_intel_record_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            assessment = assess_target(self._searched(Path(temporary)))
        self.assertIn(NO_TARGET_INTEL, assessment.blocking)
        self.assertFalse(assessment.may_start)
        self.assertIn("mailman target-intel", assessment.summary())

    def test_acknowledging_prior_attempts_does_not_clear_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            assessment = assess_target(
                self._searched(Path(temporary)), acknowledged=True
            )
        self.assertIn(NO_TARGET_INTEL, assessment.blocking)

    def test_a_failed_read_is_not_a_read(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._searched(Path(temporary))
            (root / "target-intel.json").write_text(
                json.dumps({"success": False, "detail": "rate limited"}),
                encoding="utf-8",
            )
            assessment = assess_target(root)
        self.assertIn(NO_TARGET_INTEL, assessment.blocking)

    def test_a_recorded_read_lets_the_run_start_and_states_the_rule(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._searched(Path(temporary))
            (root / "target-intel.json").write_text(
                json.dumps(
                    {
                        "success": True,
                        "repository": "langchain-ai/langchain",
                        "window_days": 14,
                        "freshness": {
                            "human_outside_merges": 15,
                            "outside_pull_requests_closed_unmerged": 129,
                        },
                        "assessment": {
                            "passes_freshness_bar": True,
                            "merge_path_rows_read": 4,
                            "merges_whose_author_held_the_assignment": 4,
                            "assignment_looks_required": True,
                            "automated_enforcement": ["require-issue-link"],
                        },
                    }
                ),
                encoding="utf-8",
            )
            assessment = assess_target(root)
        self.assertEqual(assessment.blocking, [])
        summary = assessment.summary()
        self.assertIn("15 human outside merge(s)", summary)
        self.assertIn("4 of 4 outside merge(s)", summary)
        self.assertIn("require-issue-link", summary)

    def test_a_stale_target_warns_without_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._searched(Path(temporary))
            (root / "target-intel.json").write_text(
                json.dumps(
                    {
                        "success": True,
                        "repository": "PyCQA/bandit",
                        "window_days": 14,
                        "freshness": {"human_outside_merges": 0},
                        "assessment": {"passes_freshness_bar": False},
                    }
                ),
                encoding="utf-8",
            )
            assessment = assess_target(root)
        self.assertEqual(assessment.blocking, [])
        self.assertIn("fails-freshness-bar", assessment.warnings)
